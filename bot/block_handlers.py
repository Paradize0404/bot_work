"""
Telegram-хэндлеры: блокировка / разблокировка пользователей бота.

Админ может:
  1. Просмотреть список заблокированных
  2. Заблокировать пользователя: выбрать из авторизованных
  3. Разблокировать пользователя

Доступно через «⚙️ Настройки → 🚫 Заблокированные».
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

from use_cases import blocked_users as block_uc
from use_cases import admin as admin_uc
from bot.middleware import auth_required, permission_required
from bot.permission_map import PERM_SETTINGS

logger = logging.getLogger(__name__)

router = Router(name="block_handlers")


# ═══════════════════════════════════════════════════════
#  FSM States
# ═══════════════════════════════════════════════════════


class BlockStates(StatesGroup):
    choosing_user = State()  # Выбор пользователя для блокировки


# ═══════════════════════════════════════════════════════
#  Кнопка «🚫 Заблокированные»
# ═══════════════════════════════════════════════════════


@router.message(F.text == "🚫 Заблокированные")
@auth_required
@permission_required(PERM_SETTINGS)
async def btn_blocked_users(message: Message, state: FSMContext) -> None:
    """Показать список заблокированных и предложить действия."""
    logger.info("[block] Просмотр заблокированных tg:%d", message.from_user.id)
    await _show_blocked(message)


async def _show_blocked(message: Message) -> None:
    """Вспомогательная: отобразить всех заблокированных."""
    blocked = await block_uc.get_all_blocked()

    if not blocked:
        text = "🚫 <b>Заблокированные пользователи</b>\n\nНет заблокированных."
    else:
        lines = ["🚫 <b>Заблокированные пользователи</b>\n"]
        for b in blocked:
            dt = b["blocked_at"].strftime("%d.%m.%Y %H:%M") if b["blocked_at"] else ""
            lines.append(f"  • {b['user_name']}  <i>({dt})</i>")
        text = "\n".join(lines)

    buttons = [
        [
            InlineKeyboardButton(
                text="🔒 Заблокировать",
                callback_data="blk_add",
            )
        ],
    ]

    if blocked:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🔓 Разблокировать",
                    callback_data="blk_unblock_list",
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="blk_close")])

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ═══════════════════════════════════════════════════════
#  Блокировка: выбор пользователя
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "blk_add")
async def blk_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало блокировки — показать список авторизованных пользователей."""
    await callback.answer()
    users = await admin_uc.get_employees_with_telegram()

    # Исключаем уже заблокированных
    blocked = await block_uc.get_all_blocked()
    blocked_ids = {b["telegram_id"] for b in blocked}
    users = [u for u in users if u["telegram_id"] not in blocked_ids]

    # Исключаем самого админа
    users = [u for u in users if u["telegram_id"] != callback.from_user.id]

    if not users:
        try:
            await callback.message.edit_text("ℹ️ Нет пользователей для блокировки.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return

    buttons = []
    for u in users[:50]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=u["name"],
                    callback_data=f"blk_user:{u['telegram_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="blk_close")])

    await state.set_state(BlockStates.choosing_user)
    try:
        await callback.message.edit_text(
            "🔒 Выберите пользователя для блокировки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "🔒 Выберите пользователя для блокировки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


# ═══════════════════════════════════════════════════════
#  Подтверждение блокировки
# ═══════════════════════════════════════════════════════


@router.callback_query(BlockStates.choosing_user, F.data.startswith("blk_user:"))
async def blk_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбран — заблокировать."""
    await callback.answer()
    raw = callback.data.split(":", 1)[1]
    try:
        target_tg_id = int(raw)
    except ValueError:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    # Ищем имя пользователя
    users = await admin_uc.get_employees_with_telegram()
    user_name = None
    for u in users:
        if u["telegram_id"] == target_tg_id:
            user_name = u["name"]
            break
    if not user_name:
        user_name = f"tg:{target_tg_id}"

    created = await block_uc.block_user(
        telegram_id=target_tg_id,
        user_name=user_name,
        blocked_by=callback.from_user.id,
    )

    await state.clear()

    if created:
        text = f"🔒 <b>{user_name}</b> заблокирован(а)."
    else:
        text = f"ℹ️ <b>{user_name}</b> уже заблокирован(а)."

    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  Разблокировка: выбор из заблокированных
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "blk_unblock_list")
async def blk_unblock_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать список заблокированных для разблокировки."""
    await callback.answer()
    blocked = await block_uc.get_all_blocked()

    if not blocked:
        try:
            await callback.message.edit_text("ℹ️ Нет заблокированных.")
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return

    buttons = []
    for b in blocked:
        label = f"🔓 {b['user_name']}"
        cb = f"blk_unblock:{b['telegram_id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="blk_close")])

    try:
        await callback.message.edit_text(
            "🔓 Выберите пользователя для разблокировки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await callback.message.answer(
            "🔓 Выберите пользователя для разблокировки:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data.startswith("blk_unblock:"))
async def blk_confirm_unblock(callback: CallbackQuery, state: FSMContext) -> None:
    """Разблокировать выбранного пользователя."""
    await callback.answer()
    raw = callback.data.split(":", 1)[1]
    try:
        target_tg_id = int(raw)
    except ValueError:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    removed = await block_uc.unblock_user(target_tg_id)

    if removed:
        text = "🔓 Пользователь разблокирован."
    else:
        text = "ℹ️ Пользователь не был заблокирован."

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)


# ═══════════════════════════════════════════════════════
#  Закрытие
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "blk_close")
async def blk_close(callback: CallbackQuery, state: FSMContext) -> None:
    """Закрыть меню блокировки."""
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)
