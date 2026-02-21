"""
Telegram-хэндлеры: отчёт дня (смены).

FSM-флоу:
  1. Сотрудник нажимает «📋 Отчёт дня»
  2. Бот спрашивает плюсы дня
  3. Бот спрашивает минусы дня
  4. Бот загружает данные из iiko (продажи + себестоимость)
  5. Формирует финальный отчёт и отправляет всем админам

Паттерны:
  - Тонкий handler → use_cases/day_report.py
  - prompt → edit (одно окно)
  - set_cancel_kb / restore_menu_kb
  - Удаление текста пользователя
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message

from use_cases import user_context as uctx
from use_cases import permissions as perm_uc
from use_cases import day_report as day_report_uc
from use_cases._helpers import now_kgd
from bot.middleware import (
    auth_required,
    set_cancel_kb,
    restore_menu_kb,
    truncate_input,
    MAX_TEXT_GENERAL,
)
from bot._utils import send_prompt_msg, reports_keyboard

logger = logging.getLogger(__name__)

router = Router(name="day_report_handlers")


# ══════════════════════════════════════════════════════
#  FSM States
# ══════════════════════════════════════════════════════


class DayReportStates(StatesGroup):
    positives = State()  # ввод плюсов
    negatives = State()  # ввод минусов


# ══════════════════════════════════════════════════════
#  Вход в флоу: кнопка «📋 Отчёт дня»
# ══════════════════════════════════════════════════════


@router.message(F.text == "📋 Отчёт дня")
@auth_required
async def btn_day_report_start(message: Message, state: FSMContext) -> None:
    """Начало заполнения отчёта дня."""
    tg_id = message.from_user.id
    logger.info("[day_report] Начало отчёта tg:%d", tg_id)

    try:
        await message.delete()
    except Exception:
        pass

    # Контекст пользователя — имя + подразделение
    ctx = await uctx.get_user_context(tg_id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала выберите подразделение через /start")
        return

    # Сохраняем данные в FSM
    date_str = now_kgd().strftime("%Y-%m-%d")
    await state.update_data(
        employee_name=ctx.employee_name,
        department_name=ctx.department_name,
        department_id=ctx.department_id,
        date=date_str,
    )

    # Показываем cancel-клавиатуру
    await set_cancel_kb(message.bot, message.chat.id, state)

    # Спрашиваем плюсы
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        "✅ Укажи плюсы смены:",
        log_tag="day_report",
    )
    await state.set_state(DayReportStates.positives)


# ══════════════════════════════════════════════════════
#  Шаг 1: Плюсы
# ══════════════════════════════════════════════════════


@router.message(DayReportStates.positives, F.text)
async def step_positives(message: Message, state: FSMContext) -> None:
    """Приём плюсов дня → переход к минусам."""
    tg_id = message.from_user.id
    logger.info("[day_report] Плюсы получены tg:%d", tg_id)

    try:
        await message.delete()
    except Exception:
        pass

    text = truncate_input(message.text, MAX_TEXT_GENERAL)
    await state.update_data(positives=text)

    # Спрашиваем минусы
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        "❌ Что не понравилось или нужно улучшить?",
        log_tag="day_report",
    )
    await state.set_state(DayReportStates.negatives)


# ══════════════════════════════════════════════════════
#  Шаг 2: Минусы → финализация
# ══════════════════════════════════════════════════════


@router.message(DayReportStates.negatives, F.text)
async def step_negatives(message: Message, state: FSMContext) -> None:
    """Приём минусов → сбор данных iiko → отправка отчёта."""
    tg_id = message.from_user.id
    logger.info("[day_report] Минусы получены tg:%d", tg_id)

    try:
        await message.delete()
    except Exception:
        pass

    text = truncate_input(message.text, MAX_TEXT_GENERAL)
    await state.update_data(negatives=text)
    data = await state.get_data()

    # ── Placeholder ──
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        "⏳ Собираю данные из iiko...",
        log_tag="day_report",
    )

    # ── Запрос данных из iiko ──
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    iiko_data = await day_report_uc.fetch_day_report_data()

    # ── Формируем итоговый текст ──
    report_text = day_report_uc.format_day_report(
        employee_name=data["employee_name"],
        date_str=data["date"],
        positives=data["positives"],
        negatives=text,
        iiko_data=iiko_data,
    )

    # ── Показываем результат автору ──
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        f"✅ Отчёт дня отправлен!\n\n{report_text}",
        log_tag="day_report",
    )

    # ── Отправляем отчёт всем админам ──
    admin_ids = await perm_uc.get_users_with_permission("👑 Админ")
    # Исключаем автора чтобы не дублировать
    recipients = [uid for uid in admin_ids if uid != tg_id]

    sent_count = 0
    for uid in recipients:
        try:
            await message.bot.send_message(
                uid,
                f"📋 <b>Отчёт дня</b> ({data.get('department_name', '')})\n\n{report_text}",
                parse_mode="HTML",
            )
            sent_count += 1
        except Exception as exc:
            logger.warning(
                "[day_report] Не удалось отправить отчёт uid:%d: %s", uid, exc
            )

    logger.info(
        "[day_report] Отчёт отправлен %d/%d получателям, tg:%d",
        sent_count,
        len(recipients),
        tg_id,
    )

    # ── Возврат в меню ──
    await state.clear()
    await restore_menu_kb(
        message.bot,
        message.chat.id,
        state,
        "📊 Отчёты:",
        reports_keyboard(),
    )


# ══════════════════════════════════════════════════════
#  Guard: нетекстовый ввод в FSM
# ══════════════════════════════════════════════════════


@router.message(DayReportStates.positives)
async def guard_positives(message: Message, state: FSMContext) -> None:
    """Нетекстовый ввод на шаге плюсов."""
    try:
        await message.delete()
    except Exception:
        pass
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        "⚠️ Ожидается текст. Напишите плюсы смены:",
        log_tag="day_report",
    )


@router.message(DayReportStates.negatives)
async def guard_negatives(message: Message, state: FSMContext) -> None:
    """Нетекстовый ввод на шаге минусов."""
    try:
        await message.delete()
    except Exception:
        pass
    await send_prompt_msg(
        message.bot,
        message.chat.id,
        state,
        "⚠️ Ожидается текст. Напишите минусы смены:",
        log_tag="day_report",
    )
