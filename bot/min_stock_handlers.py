"""
Telegram-хэндлеры: редактирование минимальных остатков.

Флоу:
  1. Пользователь нажимает «✏️ Изменить мин. остаток» в меню Отчётов.
  2. Вводит название товара → поиск.
  3. Выбирает товар из inline-кнопок.
  4. Вводит новое значение мин. остатка.
  5. Бот обновляет Google Таблицу + min_stock_level (БД).
"""

import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot._utils import escape_md as _escape_md, reports_keyboard
from bot.middleware import (
    set_cancel_kb, restore_menu_kb,
    parse_uuid, truncate_input, MAX_TEXT_SEARCH,
)
from use_cases import edit_min_stock as ems_uc
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)

router = Router(name="min_stock_edit_handlers")

# Префиксы callback-данных
CB_PROD = "ems:prod:"          # ems:prod:<product_id>
CB_CANCEL = "ems:cancel"


# ══════════════════════════════════════════════════════
#  FSM States
# ══════════════════════════════════════════════════════

class EditMinStockStates(StatesGroup):
    search_product = State()     # ожидание текста для поиска
    choose_product = State()     # выбор товара (inline)
    enter_min_level = State()    # ввод нового min


# ══════════════════════════════════════════════════════
#  1. Точка входа — кнопка «✏️ Изменить мин. остаток»
# ══════════════════════════════════════════════════════

@router.message(F.text == "✏️ Изменить мин. остаток")
async def btn_edit_min_stock(message: Message, state: FSMContext) -> None:
    """Начало флоу: предлагает ввести название товара."""
    logger.info("[edit-min] Старт tg:%d", message.from_user.id)

    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("❌ Сначала авторизуйтесь и выберите ресторан (/start).")
        return

    await set_cancel_kb(message.bot, message.chat.id, state)

    await state.set_state(EditMinStockStates.search_product)
    await state.update_data(department_id=ctx.department_id)
    try:
        await message.delete()
    except Exception:
        pass
    msg = await message.answer(
        "🔍 Введите название товара для поиска\n"
        "(или часть названия, например: «молоко»):",
    )
    await state.update_data(_prompt_msg_id=msg.message_id)


# ══════════════════════════════════════════════════════
#  2. Поиск товара по названию
# ══════════════════════════════════════════════════════

@router.message(EditMinStockStates.search_product)
async def search_product(message: Message, state: FSMContext) -> None:
    """Пользователь ввёл текст → ищем товары."""
    query = truncate_input((message.text or "").strip(), MAX_TEXT_SEARCH)
    logger.info("[edit-min] Поиск «%s» tg:%d", query, message.from_user.id)

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    prompt_id = data.get("_prompt_msg_id")

    if not query or len(query) < 2:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Введите минимум 2 символа для поиска.",
                    chat_id=message.chat.id, message_id=prompt_id)
            except Exception:
                pass
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await ems_uc.search_products_for_edit(query, limit=10)
    if not products:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    f"😔 Ничего не найдено по запросу «{query}».\n"
                    "Попробуйте другое название:",
                    chat_id=message.chat.id, message_id=prompt_id)
            except Exception:
                pass
        return

    # Формируем inline-кнопки
    buttons = []
    for p in products:
        label = p["name"]
        if len(label) > 55:
            label = label[:52] + "..."
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"{CB_PROD}{p['id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
    ])

    # Сохраним продукты для использования позже
    await state.update_data(
        _products_cache={p["id"]: p for p in products},
    )
    await state.set_state(EditMinStockStates.choose_product)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                f"📦 Найдено {len(products)} товаров. Выберите:",
                chat_id=message.chat.id, message_id=prompt_id,
                reply_markup=kb)
            return
        except Exception:
            pass
    msg = await message.answer(
        f"📦 Найдено {len(products)} товаров. Выберите:",
        reply_markup=kb,
    )
    await state.update_data(_prompt_msg_id=msg.message_id)


# ══════════════════════════════════════════════════════
#  3. Выбор товара → запрос нового минимума
# ══════════════════════════════════════════════════════

@router.callback_query(EditMinStockStates.choose_product, F.data.startswith(CB_PROD))
async def select_product(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал товар → запрашиваем новый min."""
    product_id = callback.data[len(CB_PROD):]
    if parse_uuid(product_id) is None:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        logger.warning("[security] Невалидный UUID product_id=%r tg:%d", product_id, callback.from_user.id)
        return
    await callback.answer()
    data = await state.get_data()
    products_cache = data.get("_products_cache", {})
    product_info = products_cache.get(product_id, {})
    product_name = product_info.get("name", product_id)

    logger.info(
        "[edit-min] Выбран товар %s tg:%d",
        product_id, callback.from_user.id,
    )

    await state.update_data(product_id=product_id, product_name=product_name,
                             _prompt_msg_id=callback.message.message_id)
    await state.set_state(EditMinStockStates.enter_min_level)

    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL)],
    ])
    await callback.message.edit_text(
        f"📦 *{_escape_md(product_name)}*\n\n"
        f"Введите новый минимальный остаток (число):\n"
        f"_(0 = убрать минимум)_",
        parse_mode="Markdown",
        reply_markup=_cancel_kb,
    )


# ══════════════════════════════════════════════════════
#  4. Ввод нового значения → обновление в iiko
# ══════════════════════════════════════════════════════

@router.message(EditMinStockStates.enter_min_level)
async def enter_min_level(message: Message, state: FSMContext) -> None:
    """Пользователь ввёл число → валидация → обновление в iiko."""
    logger.info(
        "[edit-min] Ввод min=%s tg:%d", (message.text or "").strip(), message.from_user.id
    )

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    prompt_id = data.get("_prompt_msg_id")

    # Валидация через use-case
    validated = ems_uc.apply_min_level(message.text or "")
    if isinstance(validated, ems_uc.EditMinResult):
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    validated.text,
                    chat_id=message.chat.id, message_id=prompt_id)
            except Exception:
                pass
        return

    new_min = validated
    product_id = data.get("product_id")
    department_id = data.get("department_id")
    product_name = data.get("product_name", "")

    if not product_id or not department_id:
        await message.answer("❌ Ошибка: данные сессии потеряны. Начните заново.")
        await state.clear()
        return

    # Отправляем в iiko — placeholder → edit
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                f"⏳ Обновляю мин. остаток для *{_escape_md(product_name)}*...",
                chat_id=message.chat.id, message_id=prompt_id,
                parse_mode="Markdown")
        except Exception:
            pass

    result = await ems_uc.update_min_level(
        product_id=product_id,
        department_id=department_id,
        new_min=new_min,
    )

    if prompt_id:
        try:
            await message.bot.edit_message_text(
                result, chat_id=message.chat.id,
                message_id=prompt_id, parse_mode="Markdown")
        except Exception:
            await message.answer(result, parse_mode="Markdown")
    else:
        await message.answer(result, parse_mode="Markdown")
    await state.clear()
    await restore_menu_kb(message.bot, message.chat.id, state,
                          "📊 Отчёты:", reports_keyboard())


# ══════════════════════════════════════════════════════
#  Вспомогательные callback'и
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == CB_CANCEL)
async def cancel_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена на любом этапе."""
    await callback.answer("Отменено")
    logger.info("[edit-min] Отмена tg:%d", callback.from_user.id)
    await callback.message.edit_text("🚫 Редактирование мин. остатка отменено.")
    await state.clear()
    await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                          "📊 Отчёты:", reports_keyboard())


@router.callback_query(F.data == "ems:research")
async def back_to_search(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к поиску другого товара."""
    await callback.answer()
    logger.info("[edit-min] Повторный поиск tg:%d", callback.from_user.id)
    await state.set_state(EditMinStockStates.search_product)
    await callback.message.edit_text(
        "🔍 Введите название товара для поиска:"
    )


# Guard: текст в inline-состояниях
@router.message(EditMinStockStates.choose_product)
async def _guard_inline_states(message: Message) -> None:
    """Текст в состоянии, где ожидаются кнопки."""
    logger.debug("[edit-min] Guard: текст в inline tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass



