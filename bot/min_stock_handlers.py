"""
Telegram-хэндлеры: редактирование минимальных остатков в iiko.

Флоу:
  1. Пользователь нажимает «✏️ Изменить мин. остаток» в меню Отчётов.
  2. Вводит название товара → поиск.
  3. Выбирает товар из inline-кнопок.
  4. Вводит новое значение мин. остатка.
  5. Бот обновляет на всех складах department в iiko и подтверждает.
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

    await state.set_state(EditMinStockStates.search_product)
    await state.update_data(department_id=ctx.department_id)
    await message.answer(
        "🔍 Введите название товара для поиска\n"
        "(или часть названия, например: «молоко»):",
    )


# ══════════════════════════════════════════════════════
#  2. Поиск товара по названию
# ══════════════════════════════════════════════════════

@router.message(EditMinStockStates.search_product)
async def search_product(message: Message, state: FSMContext) -> None:
    """Пользователь ввёл текст → ищем товары."""
    query = (message.text or "").strip()
    logger.info("[edit-min] Поиск «%s» tg:%d", query, message.from_user.id)

    if not query or len(query) < 2:
        await message.answer("⚠️ Введите минимум 2 символа для поиска.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    products = await ems_uc.search_products_for_edit(query, limit=10)
    if not products:
        await message.answer(
            f"😔 Ничего не найдено по запросу «{query}».\n"
            "Попробуйте другое название:"
        )
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
    await message.answer(
        f"📦 Найдено {len(products)} товаров. Выберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ══════════════════════════════════════════════════════
#  3. Выбор товара → запрос нового минимума
# ══════════════════════════════════════════════════════

@router.callback_query(EditMinStockStates.choose_product, F.data.startswith(CB_PROD))
async def select_product(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал товар → запрашиваем новый min."""
    await callback.answer()

    product_id = callback.data[len(CB_PROD):]
    data = await state.get_data()
    products_cache = data.get("_products_cache", {})
    product_info = products_cache.get(product_id, {})
    product_name = product_info.get("name", product_id)

    logger.info(
        "[edit-min] Выбран товар %s tg:%d",
        product_id, callback.from_user.id,
    )

    await state.update_data(product_id=product_id, product_name=product_name)
    await state.set_state(EditMinStockStates.enter_min_level)

    await callback.message.edit_text(
        f"📦 *{_escape_md(product_name)}*\n\n"
        f"Введите новый минимальный остаток (число):\n"
        f"_(0 = убрать минимум)_",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════
#  4. Ввод нового значения → обновление в iiko
# ══════════════════════════════════════════════════════

@router.message(EditMinStockStates.enter_min_level)
async def enter_min_level(message: Message, state: FSMContext) -> None:
    """Пользователь ввёл число → обновляем в iiko."""
    text = (message.text or "").strip().replace(",", ".")
    logger.info(
        "[edit-min] Ввод min=%s tg:%d", text, message.from_user.id
    )

    try:
        await message.delete()
    except Exception:
        pass

    # Валидация числа
    try:
        new_min = float(text)
    except ValueError:
        await message.answer(
            "⚠️ Введите число (например: 5, 10.5, 0).\n"
            "Попробуйте ещё раз:"
        )
        return

    if new_min < 0:
        await message.answer("⚠️ Значение не может быть отрицательным. Попробуйте ещё раз:")
        return

    if new_min > 999999:
        await message.answer("⚠️ Слишком большое значение. Максимум 999 999. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    department_id = data.get("department_id")
    product_name = data.get("product_name", "")

    if not product_id or not department_id:
        await message.answer("❌ Ошибка: данные сессии потеряны. Начните заново.")
        await state.clear()
        return

    # Отправляем в iiko
    await message.answer(
        f"⏳ Обновляю мин. остаток для *{_escape_md(product_name)}*...",
        parse_mode="Markdown",
    )

    result = await ems_uc.update_min_level(
        product_id=product_id,
        department_id=department_id,
        new_min=new_min,
    )

    await message.answer(result, parse_mode="Markdown")
    await state.clear()


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
    await message.answer("⚠️ Нажмите одну из кнопок выше.")


# ══════════════════════════════════════════════════════
#  Утилиты
# ══════════════════════════════════════════════════════

def _escape_md(s: str) -> str:
    """Экранировать спецсимволы Markdown v1."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s
