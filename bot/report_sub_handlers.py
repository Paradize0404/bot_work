"""
Telegram-хэндлеры: настройка подписок на отчёты дня.

Новый UX (март 2026):
  1. Кнопка «📬 Подписки на отчёты» → обзор подписок + действия
  2. «➕ Настроить» → выбор ресторана
  3. Экран ресторана: в заголовке — список подписанных,
     ниже — кнопки сотрудников. Нажатие тоглит подписку,
     сообщение обновляется in-place.
  4. Можно сменить ресторан или выйти.

Доступно через «⚙️ Настройки → 📬 Подписки на отчёты».
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from use_cases import report_subscriptions as sub_uc
from use_cases import auth as auth_uc
from bot.middleware import auth_required, permission_required
from bot.permission_map import PERM_SETTINGS

logger = logging.getLogger(__name__)

router = Router(name="report_sub_handlers")


# ═══════════════════════════════════════════════════════
#  FSM States
# ═══════════════════════════════════════════════════════


class ReportSubStates(StatesGroup):
    editing_department = State()  # Экран редактирования подписок ресторана


# ═══════════════════════════════════════════════════════
#  Кнопка «📬 Подписки на отчёты» — главный экран
# ═══════════════════════════════════════════════════════


@router.message(F.text == "📬 Подписки на отчёты")
@auth_required
@permission_required(PERM_SETTINGS)
async def btn_report_subscriptions(message: Message, state: FSMContext) -> None:
    """Показать текущие подписки и предложить действия."""
    logger.info("[report_sub] Просмотр подписок tg:%d", message.from_user.id)
    await state.clear()
    text, kb = await _build_overview()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _build_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Построить обзорное сообщение со всеми подписками."""
    subs = await sub_uc.get_all_subscriptions()

    if not subs:
        text = "📬 <b>Подписки на отчёты дня</b>\n\nНет подписок."
    else:
        lines = ["📬 <b>Подписки на отчёты дня</b>\n"]
        by_dept: dict[str, list[str]] = {}
        for s in subs:
            dept = s["department_name"]
            if dept not in by_dept:
                by_dept[dept] = []
            by_dept[dept].append(s["user_name"])

        for dept, users in sorted(by_dept.items()):
            lines.append(f"\n🏢 <b>{dept}</b>:")
            for u in users:
                lines.append(f"  • {u}")
        text = "\n".join(lines)

    buttons = [
        [
            InlineKeyboardButton(
                text="➕ Настроить",
                callback_data="rsub_pick_dept",
            )
        ],
    ]
    if subs:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить подписку",
                    callback_data="rsub_del_list",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="rsub_close")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════
#  Шаг 1: выбор ресторана
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "rsub_pick_dept")
async def rsub_pick_dept(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать список ресторанов для настройки подписок."""
    await callback.answer()
    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        try:
            await callback.message.edit_text("⚠️ Нет доступных подразделений.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=r["name"],
                callback_data=f"rsub_dept:{r['id']}",
            )
        ]
        for r in restaurants
    ]
    buttons.append(
        [InlineKeyboardButton(text="◀️ Назад", callback_data="rsub_back_overview")]
    )

    try:
        await callback.message.edit_text(
            "🏢 Выберите ресторан для настройки подписок:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "🏢 Выберите ресторан для настройки подписок:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


# ═══════════════════════════════════════════════════════
#  Шаг 2: экран редактирования подписок ресторана
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("rsub_dept:"))
async def rsub_enter_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбран ресторан — показать подписанных + кнопки сотрудников."""
    await callback.answer()
    dept_id = callback.data.split(":", 1)[1]

    await state.set_state(ReportSubStates.editing_department)
    await state.update_data(dept_id=dept_id)

    await _render_department_editor(callback.message, dept_id)


async def _render_department_editor(message, dept_id: str) -> None:
    """Отрисовать экран редактирования подписок для ресторана."""
    # Получаем данные
    restaurants = await auth_uc.get_restaurants()
    dept_name = dept_id
    for r in restaurants:
        if r["id"] == dept_id:
            dept_name = r["name"]
            break

    # Текущие подписчики этого ресторана
    sub_tg_ids = set(await sub_uc.get_subscribers_for_department(dept_id))

    # Все авторизованные пользователи
    all_users = await sub_uc.get_all_authorized_users()

    # Строим текст: заголовок + список подписанных
    lines = [f"🏢 <b>{dept_name}</b>\n"]
    if sub_tg_ids:
        lines.append("📋 <b>Подписаны:</b>")
        for u in all_users:
            if u["telegram_id"] in sub_tg_ids:
                lines.append(f"  ✅ {u['name']}")
    else:
        lines.append("<i>Нет подписчиков</i>")

    lines.append("\n👇 Нажмите на сотрудника чтобы добавить/убрать:")

    text = "\n".join(lines)

    # Кнопки: сотрудники (с маркером подписки)
    buttons = []
    for u in all_users[:50]:
        tg_id = u["telegram_id"]
        is_sub = tg_id in sub_tg_ids
        marker = "✅" if is_sub else "➕"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {u['name']}",
                    callback_data=f"rsub_toggle:{tg_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Сменить ресторан",
                callback_data="rsub_pick_dept",
            ),
            InlineKeyboardButton(
                text="✅ Готово",
                callback_data="rsub_back_overview",
            ),
        ]
    )

    try:
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        # Message not modified — ignore
        logger.debug("suppressed", exc_info=True)


# ═══════════════════════════════════════════════════════
#  Тогл подписки (нажатие на сотрудника)
# ═══════════════════════════════════════════════════════


@router.callback_query(
    ReportSubStates.editing_department, F.data.startswith("rsub_toggle:")
)
async def rsub_toggle_user(callback: CallbackQuery, state: FSMContext) -> None:
    """Тогл подписки: если подписан — удалить, если нет — добавить."""
    await callback.answer()
    raw = callback.data.split(":", 1)[1]
    try:
        target_tg_id = int(raw)
    except ValueError:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    data = await state.get_data()
    dept_id = data.get("dept_id")
    if not dept_id:
        await callback.answer("⚠️ Ресторан не выбран", show_alert=True)
        return

    # Проверяем текущее состояние подписки
    current_subs = set(await sub_uc.get_subscribers_for_department(dept_id))

    if target_tg_id in current_subs:
        await sub_uc.remove_subscription(target_tg_id, dept_id)
        logger.info(
            "[report_sub] Подписка удалена: tg:%d dept=%s by tg:%d",
            target_tg_id,
            dept_id,
            callback.from_user.id,
        )
    else:
        await sub_uc.add_subscription(
            telegram_id=target_tg_id,
            department_id=dept_id,
            created_by=callback.from_user.id,
        )
        logger.info(
            "[report_sub] Подписка добавлена: tg:%d dept=%s by tg:%d",
            target_tg_id,
            dept_id,
            callback.from_user.id,
        )

    # Перерисовываем экран
    await _render_department_editor(callback.message, dept_id)


# ═══════════════════════════════════════════════════════
#  Удаление подписки (из обзорного экрана)
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "rsub_del_list")
async def rsub_del_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать список подписок для удаления."""
    await callback.answer()
    subs = await sub_uc.get_all_subscriptions()

    if not subs:
        try:
            await callback.message.edit_text("Нет подписок для удаления.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return

    buttons = []
    for s in subs:
        label = f"❌ {s['user_name']} → {s['department_name']}"
        cb = f"rsub_del:{s['telegram_id']}:{s['department_id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])

    buttons.append(
        [InlineKeyboardButton(text="◀️ Назад", callback_data="rsub_back_overview")]
    )

    try:
        await callback.message.edit_text(
            "🗑 Выберите подписку для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "🗑 Выберите подписку для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data.startswith("rsub_del:"))
async def rsub_confirm_del(callback: CallbackQuery, state: FSMContext) -> None:
    """Удалить выбранную подписку и вернуться к обзору."""
    await callback.answer()
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    tg_id = int(parts[1])
    dept_id = parts[2]

    removed = await sub_uc.remove_subscription(tg_id, dept_id)
    logger.info(
        "[report_sub] Удаление подписки tg:%d dept=%s → %s",
        tg_id,
        dept_id,
        removed,
    )

    # Возвращаемся к обзору
    text, kb = await _build_overview()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
#  Назад к обзору / Закрытие
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "rsub_back_overview")
async def rsub_back_overview(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к обзорному экрану подписок."""
    await callback.answer()
    await state.clear()
    text, kb = await _build_overview()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "rsub_close")
async def rsub_close(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрыть настройки подписок."""
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)
