"""
Telegram-хэндлеры: настройка подписок на отчёты дня.

Админ может:
  1. Просмотреть текущие подписки
  2. Добавить подписку: выбрать пользователя → выбрать подразделение
  3. Удалить подписку

Доступно через «⚙️ Настройки → 📬 Подписки на отчёты».
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
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
    choosing_user = State()  # Выбор пользователя для подписки
    choosing_department = State()  # Выбор подразделения


# ═══════════════════════════════════════════════════════
#  Кнопка «📬 Подписки на отчёты»
# ═══════════════════════════════════════════════════════


@router.message(F.text == "📬 Подписки на отчёты")
@auth_required
@permission_required(PERM_SETTINGS)
async def btn_report_subscriptions(message: Message, state: FSMContext) -> None:
    """Показать текущие подписки и предложить действия."""
    logger.info("[report_sub] Просмотр подписок tg:%d", message.from_user.id)
    await _show_subscriptions(message)


async def _show_subscriptions(message: Message) -> None:
    """Вспомогательная: отобразить все подписки."""
    subs = await sub_uc.get_all_subscriptions()

    if not subs:
        text = "📬 <b>Подписки на отчёты дня</b>\n\nНет подписок."
    else:
        lines = ["📬 <b>Подписки на отчёты дня</b>\n"]
        # Группируем по подразделению
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
                text="➕ Добавить подписку",
                callback_data="rsub_add",
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

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ═══════════════════════════════════════════════════════
#  Добавление подписки: выбор пользователя
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "rsub_add")
async def rsub_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления подписки — показать список пользователей."""
    await callback.answer()
    users = await sub_uc.get_all_authorized_users()

    if not users:
        try:
            await callback.message.edit_text("⚠️ Нет авторизованных пользователей.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return

    # Пагинация: показываем по 8 пользователей
    buttons = []
    for u in users[:50]:  # лимит
        buttons.append(
            [
                InlineKeyboardButton(
                    text=u["name"],
                    callback_data=f"rsub_user:{u['telegram_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="rsub_close")])

    await state.set_state(ReportSubStates.choosing_user)
    try:
        await callback.message.edit_text(
            "👤 Выберите пользователя для подписки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "👤 Выберите пользователя для подписки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


# ═══════════════════════════════════════════════════════
#  Добавление подписки: выбор подразделения
# ═══════════════════════════════════════════════════════


@router.callback_query(ReportSubStates.choosing_user, F.data.startswith("rsub_user:"))
async def rsub_choose_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбран — показать список подразделений."""
    await callback.answer()
    raw = callback.data.split(":", 1)[1]
    try:
        target_tg_id = int(raw)
    except ValueError:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    await state.update_data(target_tg_id=target_tg_id)

    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        try:
            await callback.message.edit_text("⚠️ Нет доступных подразделений.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        await state.clear()
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
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="rsub_close")])

    await state.set_state(ReportSubStates.choosing_department)
    try:
        await callback.message.edit_text(
            "🏢 Выберите подразделение для подписки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "🏢 Выберите подразделение для подписки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(
    ReportSubStates.choosing_department, F.data.startswith("rsub_dept:")
)
async def rsub_confirm_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Подразделение выбрано — создаём подписку."""
    await callback.answer()
    dept_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    target_tg_id = data.get("target_tg_id")

    if not target_tg_id:
        await callback.answer("⚠️ Ошибка: пользователь не выбран", show_alert=True)
        await state.clear()
        return

    created = await sub_uc.add_subscription(
        telegram_id=target_tg_id,
        department_id=dept_id,
        created_by=callback.from_user.id,
    )

    await state.clear()

    if created:
        text = "✅ Подписка добавлена!"
    else:
        text = "ℹ️ Подписка уже существует."

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)


# ═══════════════════════════════════════════════════════
#  Удаление подписки
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

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="rsub_close")])

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
    """Удалить выбранную подписку."""
    await callback.answer()
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    tg_id = int(parts[1])
    dept_id = parts[2]

    removed = await sub_uc.remove_subscription(tg_id, dept_id)

    if removed:
        text = "✅ Подписка удалена."
    else:
        text = "ℹ️ Подписка не найдена."

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)


# ═══════════════════════════════════════════════════════
#  Закрытие
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "rsub_close")
async def rsub_close(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрыть настройки подписок."""
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)
