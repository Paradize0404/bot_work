"""
Telegram-хэндлеры: шаблоны расходных накладных + создание по шаблону.

Два FSM-потока:

A) Создание шаблона:
  1. 🏬 Выбор склада (бар / кухня)
  2. 🏢 Выбор поставщика из прайс-листа БД (price_supplier_column)
  3. 🔍 Поиск товара по name из price_product → добавление (цена из БД)
  4. ➕ Добавить ещё / ✅ Сохранить шаблон
  5. 📝 Название шаблона → сохранение в invoice_template

B) Создание по шаблону:
  1. 📦 Выбор шаблона из invoice_template (по department_id)
  2. 📋 Показ позиций с ценами
  3. ✏️ Ввод количества для каждой позиции (одним сообщением)
  4. ✅ Подтверждение → отправка расходной накладной в iiko

Оптимизации:
  - callback.answer() ПЕРВЫМ
  - Защита: текст в inline-состояниях, double-click, валидация callback_data
"""

import asyncio
import logging
import re
from uuid import UUID

from aiogram import Bot, Router, F
from aiogram.enums import ChatAction
from bot.middleware import set_cancel_kb, restore_menu_kb
from bot._utils import (
    invoices_keyboard,
    items_inline_kb,
    send_prompt_msg,
    update_summary_msg,
    safe_page,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

from use_cases import outgoing_invoice as inv_uc
from use_cases import user_context as uctx
from use_cases import pdf_invoice as pdf_uc
from use_cases.writeoff import normalize_unit

logger = logging.getLogger(__name__)

router = Router(name="invoice_handlers")

MAX_ITEMS = 50


# ══════════════════════════════════════════════════════
#  FSM States — создание шаблона
# ══════════════════════════════════════════════════════


class InvoiceTemplateStates(StatesGroup):
    store = State()  # выбор склада
    supplier_choose = State()  # выбор поставщика из прайс-листа
    add_items = State()  # поиск и добавление товаров
    template_name = State()  # ввод названия шаблона


# ══════════════════════════════════════════════════════
#  FSM States — создание по шаблону
# ══════════════════════════════════════════════════════


class InvoiceFromTemplateStates(StatesGroup):
    choose_template = State()  # выбор шаблона
    enter_quantities = State()  # ввод количества для позиций
    confirm = State()  # подтверждение и отправка


# ══════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════


def _stores_kb(stores: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(
        stores, prefix="inv_store", cancel_data="inv_cancel", page=page
    )


def _suppliers_kb(suppliers: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(
        suppliers, prefix="inv_sup", cancel_data="inv_cancel", page=page
    )


def _products_kb(products: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(
        products, prefix="inv_prod", cancel_data="inv_cancel", page=page
    )


def _add_more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Сохранить шаблон", callback_data="inv_save"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")],
        ]
    )


def _templates_kb(templates: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    total = len(templates)
    page_size = 10
    start = page * page_size
    end = start + page_size
    page_items = templates[start:end]

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{t['name']} ({t['counteragent_name']}, {t['items_count']} поз.)",
                callback_data=f"inv_tmpl:{t['pk']}",
            )
        ]
        for t in page_items
    ]

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"inv_tmpl_page:{page - 1}"
            )
        )
    if end < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️ Далее", callback_data=f"inv_tmpl_page:{page + 1}"
            )
        )

    if nav:
        total_pages = (total + page_size - 1) // page_size
        nav.insert(
            len(nav) // 2,
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="noop",
            ),
        )
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить накладную", callback_data="inv_confirm_send"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Ввести заново", callback_data="inv_reenter_qty"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")],
        ]
    )


# ══════════════════════════════════════════════════════
#  Summary-сообщение
# ══════════════════════════════════════════════════════


def _build_summary(data: dict) -> str:
    store = data.get("store_name", "—")
    counteragent = data.get("counteragent_name", "—")
    account = data.get("account_name", "—")

    text = (
        f"📋 <b>Шаблон расходной накладной</b>\n"
        f"🏬 <b>Склад:</b> {store}\n"
        f"🏢 <b>Контрагент:</b> {counteragent}\n"
        f"📂 <b>Счёт:</b> {account}"
    )
    items = data.get("items", [])
    if items:
        text += "\n\n<b>Позиции:</b>"
        for i, item in enumerate(items, 1):
            unit = item.get("unit_name", "шт")
            price = item.get("sell_price")
            price_str = f" — {price:.2f}₽/{unit}" if price else ""
            text += f"\n  {i}. {item['name']}{price_str}"
    else:
        text += "\n\n<b>Позиции:</b> (пусто)"
    return text


async def _update_summary(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await update_summary_msg(bot, chat_id, state, _build_summary, log_tag="invoice")


async def _send_prompt(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    await send_prompt_msg(bot, chat_id, state, text, reply_markup, log_tag="invoice")


# ══════════════════════════════════════════════════════
#  Защита: текст в inline-состояниях
# ══════════════════════════════════════════════════════


@router.message(InvoiceTemplateStates.store)
async def _ignore_text_store(message: Message) -> None:
    logger.debug("[invoice] Текст в store-состоянии tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(InvoiceTemplateStates.supplier_choose)
async def _ignore_text_supplier_choose(message: Message) -> None:
    logger.debug("[invoice] Текст в supplier_choose tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(InvoiceFromTemplateStates.choose_template)
async def _ignore_text_choose_template(message: Message) -> None:
    logger.debug("[invoice] Текст в choose_template tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(InvoiceFromTemplateStates.confirm)
async def _ignore_text_confirm(message: Message) -> None:
    logger.debug("[invoice] Текст в confirm-состоянии tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  A) СОЗДАНИЕ ШАБЛОНА — шаги
# ══════════════════════════════════════════════════════

# ── 1. Старт — «📑 Создать шаблон накладной» ──


@router.message(F.text == "📑 Создать шаблон накладной")
async def start_template(message: Message, state: FSMContext) -> None:
    logger.info("[invoice_handlers] start_template tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала авторизуйтесь (/start) и выберите ресторан.")
        return

    await set_cancel_kb(message.bot, message.chat.id, state)

    logger.info(
        "[invoice] Старт создания шаблона tg:%d, dept=%s (%s)",
        message.from_user.id,
        ctx.department_id,
        ctx.department_name,
    )

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    # Параллельно: склады + счёт реализации + поставщики из прайс-листа
    stores, account, price_suppliers = await asyncio.gather(
        inv_uc.get_stores_for_department(ctx.department_id),
        inv_uc.get_revenue_account(),
        inv_uc.get_price_list_suppliers(),
    )
    logger.info(
        "[invoice][template] Загружено: stores=%d, account=%s, price_suppliers=%d",
        len(stores) if stores else 0,
        account.get("name") if account else None,
        len(price_suppliers) if price_suppliers else 0,
    )

    if not stores:
        await message.answer("❌ У вашего подразделения нет складов (бар/кухня).")
        return

    if not account:
        await message.answer("❌ Счёт «реализация на точки» не найден в справочниках.")
        return

    if not price_suppliers:
        await message.answer(
            "❌ В прайс-листе нет поставщиков.\n"
            "Сначала назначьте поставщиков в Google Таблице "
            "и нажмите «💰 Прайс-лист → GSheet»."
        )
        return

    await state.update_data(
        department_id=ctx.department_id,
        account_id=account["id"],
        account_name=account["name"],
        items=[],
        _stores_cache=stores,
        _suppliers_cache=price_suppliers,
    )

    # Summary-сообщение
    summary_msg = await message.answer(
        _build_summary(await state.get_data()),
        parse_mode="HTML",
    )
    await state.update_data(header_msg_id=summary_msg.message_id)

    # Показать склады
    await state.set_state(InvoiceTemplateStates.store)
    msg = await message.answer(
        "🏬 Выберите склад:",
        reply_markup=_stores_kb(stores),
    )
    await state.update_data(prompt_msg_id=msg.message_id)


# ── 2. Выбор склада ──


@router.callback_query(InvoiceTemplateStates.store, F.data.startswith("inv_store:"))
async def choose_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = callback.data.split(":", 1)[1]

    try:
        UUID(store_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    logger.info(
        "[invoice] Выбор склада tg:%d, store_id=%s", callback.from_user.id, store_id
    )
    data = await state.get_data()
    stores = data.get("_stores_cache") or await inv_uc.get_stores_for_department(
        data["department_id"],
    )
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        await callback.answer("❌ Склад не найден", show_alert=True)
        return

    await state.update_data(store_id=store_id, store_name=store["name"])
    await _update_summary(callback.bot, callback.message.chat.id, state)

    # Показать поставщиков из прайс-листа
    suppliers = data.get("_suppliers_cache") or await inv_uc.get_price_list_suppliers()

    await state.set_state(InvoiceTemplateStates.supplier_choose)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        "🏢 Выберите поставщика из прайс-листа:",
        reply_markup=_suppliers_kb(suppliers),
    )


# ── 3. Выбор поставщика ──


@router.callback_query(
    InvoiceTemplateStates.supplier_choose,
    F.data.startswith("inv_sup:"),
)
async def choose_supplier(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    sup_id = callback.data.split(":", 1)[1]

    try:
        UUID(sup_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    logger.info(
        "[invoice][template] Выбор поставщика tg:%d, sup_id=%s",
        callback.from_user.id,
        sup_id,
    )
    data = await state.get_data()
    suppliers = data.get("_suppliers_cache") or await inv_uc.get_price_list_suppliers()
    supplier = next((s for s in suppliers if s["id"] == sup_id), None)
    if not supplier:
        await callback.answer("❌ Поставщик не найден", show_alert=True)
        return

    # Предзагружаем цены поставщика
    supplier_prices = await inv_uc.get_supplier_prices(sup_id)
    logger.info(
        "[invoice][template] Поставщик «%s» выбран, предзагружено цен: %d, tg:%d",
        supplier["name"],
        len(supplier_prices),
        callback.from_user.id,
    )

    await state.update_data(
        counteragent_id=sup_id,
        counteragent_name=supplier["name"],
        _supplier_prices=supplier_prices,
    )
    await _update_summary(callback.bot, callback.message.chat.id, state)

    # Переход к добавлению товаров
    await state.set_state(InvoiceTemplateStates.add_items)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        "🔍 Введите название товара для поиска:",
    )


# ── 4. Поиск и добавление товаров ──


@router.message(InvoiceTemplateStates.add_items)
async def search_product(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    logger.info("[invoice] Поиск товара tg:%d, query='%s'", message.from_user.id, query)
    try:
        await message.delete()
    except Exception:
        pass

    if not query:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Введите название товара для поиска:",
        )
        return

    if len(query) > 200:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Макс. 200 символов. Попробуйте короче:",
        )
        return

    data = await state.get_data()
    items = data.get("items", [])
    if len(items) >= MAX_ITEMS:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            f"⚠️ Максимум {MAX_ITEMS} позиций. Нажмите «✅ Сохранить шаблон».",
            reply_markup=_add_more_kb(),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    # Поиск в прайс-листе БД
    products = await inv_uc.search_price_products(query)

    if not products:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            f"🔍 По запросу «{query}» ничего не найдено в прайс-листе.\n"
            "Введите другое название или нажмите «✅ Сохранить»:",
            reply_markup=_add_more_kb() if items else None,
        )
        return

    await state.update_data(_products_cache=products)
    await _send_prompt(
        message.bot,
        message.chat.id,
        state,
        f"🔍 Найдено {len(products)}. Выберите товар:",
        reply_markup=_products_kb(products, page=0),
    )


@router.callback_query(F.data.startswith("inv_store_page:"))
async def invoice_store_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] invoice_store_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    stores = data.get("_stores_cache", [])
    if not stores:
        await callback.answer("Склады не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=_stores_kb(stores, page=page))


@router.callback_query(F.data.startswith("inv_sup_page:"))
async def invoice_sup_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] invoice_sup_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    suppliers = data.get("_suppliers_cache", [])
    if not suppliers:
        await callback.answer("Контрагенты не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(
        reply_markup=_suppliers_kb(suppliers, page=page)
    )


@router.callback_query(F.data.startswith("inv_prod_page:"))
async def invoice_prod_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] invoice_prod_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    products = data.get("_products_cache", [])
    if not products:
        await callback.answer("Товары не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(
        reply_markup=_products_kb(products, page=page)
    )


@router.callback_query(F.data.startswith("inv_tmpl_page:"))
async def invoice_tmpl_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] invoice_tmpl_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    templates = data.get("_templates_cache", [])
    if not templates:
        await callback.answer("Шаблоны не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(
        reply_markup=_templates_kb(templates, page=page)
    )


# ── 5. Выбор товара → добавление ──


@router.callback_query(InvoiceTemplateStates.add_items, F.data.startswith("inv_prod:"))
async def choose_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    prod_id = callback.data.split(":", 1)[1]

    try:
        UUID(prod_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    logger.info(
        "[invoice] Выбор товара tg:%d, prod_id=%s", callback.from_user.id, prod_id
    )
    data = await state.get_data()
    products = data.get("_products_cache") or []
    product = next((p for p in products if p["id"] == prod_id), None)
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return

    # Проверим дубль
    items = data.get("items", [])
    if any(it["product_id"] == prod_id for it in items):
        await callback.answer("⚠️ Этот товар уже добавлен", show_alert=True)
        return

    # Подтягиваем цену из прайс-листа БД; fallback → себестоимость
    supplier_prices = data.get("_supplier_prices", {})
    sell_price = supplier_prices.get(prod_id, 0.0)
    cost_price = product.get("cost_price", 0.0)
    effective_price = sell_price or cost_price
    used_cost = (not sell_price) and bool(cost_price)

    items.append(
        {
            "product_id": prod_id,
            "name": product["name"],
            "unit_name": product.get("unit_name", "шт"),
            "main_unit": product.get("main_unit"),
            "cost_price": cost_price,
            "sell_price": effective_price,
        }
    )
    await state.update_data(items=items)
    await _update_summary(callback.bot, callback.message.chat.id, state)

    if effective_price:
        price_info = (
            f" (себест.: {effective_price:.2f}₽)"
            if used_cost
            else f" (цена: {effective_price:.2f}₽)"
        )
    else:
        price_info = " (цена не задана)"
    logger.info(
        "[invoice][template] Добавлен товар #%d: «%s» prod_id=%s, "
        "unit=%s, main_unit=%s, cost=%.2f, sell=%.2f, effective=%.2f, used_cost=%s, tg:%d",
        len(items),
        product["name"],
        prod_id,
        product.get("unit_name", "шт"),
        product.get("main_unit"),
        cost_price,
        sell_price,
        effective_price,
        used_cost,
        callback.from_user.id,
    )

    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        f"✅ Добавлено: {product['name']}{price_info}\n"
        f"Всего позиций: {len(items)}\n\n"
        "🔍 Введите название следующего товара или сохраните шаблон:",
        reply_markup=_add_more_kb(),
    )


# ── 6. Сохранить шаблон → ввод названия ──


@router.callback_query(InvoiceTemplateStates.add_items, F.data == "inv_save")
async def ask_template_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    items = data.get("items", [])
    if not items:
        await callback.answer("⚠️ Добавьте хотя бы одну позицию", show_alert=True)
        return

    logger.info(
        "[invoice] Переход к вводу имени шаблона tg:%d, items=%d",
        callback.from_user.id,
        len(items),
    )
    await state.set_state(InvoiceTemplateStates.template_name)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        "📝 Введите название шаблона (макс. 200 символов):",
    )


@router.message(InvoiceTemplateStates.template_name)
async def save_template(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    logger.info(
        "[invoice] Ввод названия шаблона tg:%d, name='%s'", message.from_user.id, name
    )
    try:
        await message.delete()
    except Exception:
        pass

    if not name:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Название не может быть пустым. Введите название шаблона:",
        )
        return

    if len(name) > 200:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Макс. 200 символов. Введите покороче:",
        )
        return

    data = await state.get_data()

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    pk = await inv_uc.save_template(
        name=name,
        created_by=message.from_user.id,
        department_id=data["department_id"],
        counteragent_id=data["counteragent_id"],
        counteragent_name=data["counteragent_name"],
        account_id=data["account_id"],
        account_name=data["account_name"],
        store_id=data["store_id"],
        store_name=data["store_name"],
        items=data.get("items", []),
    )

    await state.update_data(template_name=name)
    await _update_summary(message.bot, message.chat.id, state)

    await _send_prompt(
        message.bot,
        message.chat.id,
        state,
        f"✅ Шаблон «{name}» сохранён! (#{pk}, {len(data.get('items', []))} позиций)",
    )

    logger.info(
        "[invoice] ✅ Шаблон pk=%d «%s» сохранён tg:%d",
        pk,
        name,
        message.from_user.id,
    )
    await state.clear()
    await restore_menu_kb(
        message.bot, message.chat.id, state, "📦 Накладные:", invoices_keyboard()
    )


# ══════════════════════════════════════════════════════
#  B) СОЗДАНИЕ ПО ШАБЛОНУ — шаги
# ══════════════════════════════════════════════════════

# ── 1. Старт — «📦 Создать по шаблону» ──


@router.message(F.text == "📦 Создать по шаблону")
async def start_from_template(message: Message, state: FSMContext) -> None:
    logger.info("[invoice_handlers] start_from_template tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала авторизуйтесь (/start) и выберите ресторан.")
        return

    await set_cancel_kb(message.bot, message.chat.id, state)

    logger.info(
        "[invoice] Старт создания по шаблону tg:%d, dept=%s",
        message.from_user.id,
        ctx.department_id,
    )

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    templates = await inv_uc.get_templates_for_department(ctx.department_id)
    if not templates:
        await message.answer("❌ Нет сохранённых шаблонов.\nСначала создайте шаблон.")
        return

    await state.update_data(
        department_id=ctx.department_id,
        _templates_cache=templates,
    )
    await state.set_state(InvoiceFromTemplateStates.choose_template)
    await message.answer(
        "📦 Выберите шаблон для создания накладной:",
        reply_markup=_templates_kb(templates),
    )


# ── 2. Выбор шаблона ──


@router.callback_query(
    InvoiceFromTemplateStates.choose_template,
    F.data.startswith("inv_tmpl:"),
)
async def choose_template_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pk_str = callback.data.split(":", 1)[1]

    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    logger.info("[invoice] Выбор шаблона tg:%d, pk=%d", callback.from_user.id, pk)
    template = await inv_uc.get_template_by_pk(pk)
    if not template:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return

    items = template.get("items", [])
    if not items:
        await callback.answer("❌ У шаблона нет позиций", show_alert=True)
        return

    # ── Динамическое подтягивание цен (не хардкодим в шаблоне) ──
    product_ids = [it.get("product_id") or it.get("id", "") for it in items]
    supplier_prices = await inv_uc.get_supplier_prices(template["counteragent_id"])
    cost_prices = await inv_uc.get_cost_prices_bulk(product_ids)

    for item in items:
        pid = item.get("product_id") or item.get("id", "")
        sup_price = supplier_prices.get(pid, 0.0)
        cost_price = cost_prices.get(pid, 0.0)
        effective = sup_price or cost_price
        item["sell_price"] = effective
        item["cost_price"] = cost_price

    logger.info(
        "[invoice][from_tpl] Динамические цены: supplier=%d, cost=%d, tg:%d",
        len(supplier_prices),
        len(cost_prices),
        callback.from_user.id,
    )

    # Показываем позиции с ценами и подсказкой единиц ввода
    text = (
        f"📋 <b>Шаблон:</b> {template['name']}\n"
        f"🏬 <b>Склад:</b> {template['store_name']}\n"
        f"🏢 <b>Контрагент:</b> {template['counteragent_name']}\n"
        f"📂 <b>Счёт:</b> {template['account_name']}\n\n"
        f"<b>Позиции ({len(items)} шт.):</b>\n"
    )
    for i, item in enumerate(items, 1):
        price = item.get("sell_price", 0)
        cost = item.get("cost_price", 0)
        unit = item.get("unit_name", "шт")
        norm = normalize_unit(unit)
        if norm == "kg":
            input_hint = "в граммах"
        elif norm == "l":
            input_hint = "в мл"
        else:
            input_hint = f"в {unit}"
        if price and price != cost:
            price_str = f" — 💰 {price:.2f}₽/{unit}"
        elif price:
            price_str = f" — себест. {price:.2f}₽/{unit}"
        else:
            price_str = ""
        text += f"  {i}. {item['name']}{price_str} → <i>{input_hint}</i>\n"

    text += (
        "\n✏️ <b>Введите количество для каждой позиции</b> "
        "(по одному числу на строке, в том же порядке):\n"
        "<i>Например:</i>\n<code>"
    )
    examples = ["500" if i == 0 else "200" for i in range(min(3, len(items)))]
    text += "\n".join(examples)
    text += "</code>"

    await state.update_data(
        _template=template,
        _items=items,
    )
    await state.set_state(InvoiceFromTemplateStates.enter_quantities)

    _cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")],
        ]
    )
    # Новое сообщение (может быть длинным)
    msg = await callback.message.answer(
        text, parse_mode="HTML", reply_markup=_cancel_kb
    )
    await state.update_data(prompt_msg_id=msg.message_id)


# ── 3. Ввод количества ──


@router.message(InvoiceFromTemplateStates.enter_quantities)
async def enter_quantities(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()[:2000]
    logger.info(
        "[invoice][from_tpl] Ввод количества tg:%d, raw='%s'",
        message.from_user.id,
        raw[:100],
    )
    try:
        await message.delete()
    except Exception:
        pass

    if not raw:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Введите количество для каждой позиции (по числу на строке):",
        )
        return

    data = await state.get_data()
    items = data.get("_items", [])

    # Парсим количества (через пробел, запятую или перевод строки)
    parts = re.split(r"[\n,;\s]+", raw.strip())
    quantities: list[float] = []
    for p in parts:
        p = p.strip().replace(",", ".")
        if not p:
            continue
        try:
            q = float(p)
            quantities.append(q)
        except ValueError:
            await _send_prompt(
                message.bot,
                message.chat.id,
                state,
                f"⚠️ Не удалось распознать число: «{p}»\n"
                "Введите количества заново (по числу на строке):",
            )
            return

    if len(quantities) != len(items):
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            f"⚠️ Ожидается {len(items)} чисел (позиций), получено {len(quantities)}.\n"
            "Введите количества заново:",
        )
        return

    # Строим итоги с конвертацией единиц (г→кг, мл→л)
    template = data.get("_template", {})
    items_with_qty: list[dict] = []
    total_sum = 0.0
    text = (
        f"📋 <b>Накладная по шаблону:</b> {template.get('name', '?')}\n"
        f"🏬 {template.get('store_name', '?')}\n"
        f"🏢 {template.get('counteragent_name', '?')}\n\n"
    )
    for i, (item, qty) in enumerate(zip(items, quantities), 1):
        price = item.get("sell_price", 0.0)
        unit = item.get("unit_name", "шт")
        norm = normalize_unit(unit)

        # Конвертация: пользователь вводит г/мл, API принимает кг/л
        if norm in ("kg", "l"):
            converted = qty / 1000
            display_unit = "г" if norm == "kg" else "мл"
            api_unit = "кг" if norm == "kg" else "л"
        else:
            converted = qty
            display_unit = unit
            api_unit = unit

        line_sum = converted * price  # цена за кг/л/шт
        total_sum += line_sum

        text += f"  {i}. {item['name']} × {qty} {display_unit}"
        if norm in ("kg", "l"):
            text += f" ({converted:.3g} {api_unit})"
        if price:
            text += f" × {price:.2f}₽ = {line_sum:.2f}₽"
        text += "\n"
        logger.debug(
            "[invoice][from_tpl] Позиция %d: «%s» qty_input=%.4g %s → converted=%.4g %s, "
            "price=%.2f, line_sum=%.2f, main_unit=%s",
            i,
            item["name"],
            qty,
            display_unit,
            converted,
            api_unit,
            price,
            line_sum,
            item.get("main_unit"),
        )
        items_with_qty.append(
            {
                "product_id": item.get("product_id") or item.get("id"),
                "name": item["name"],
                "amount": converted,
                "price": price,
                "main_unit": item.get("main_unit"),
                "unit_name": unit,
            }
        )

    text += f"\n<b>Итого: {total_sum:.2f}₽</b>"

    logger.info(
        "[invoice][from_tpl] Итоги: items=%d, total_sum=%.2f, tg:%d",
        len(items_with_qty),
        total_sum,
        message.from_user.id,
    )
    await state.update_data(_items_with_qty=items_with_qty, _total_sum=total_sum)
    await state.set_state(InvoiceFromTemplateStates.confirm)

    await _send_prompt(
        message.bot,
        message.chat.id,
        state,
        text,
        reply_markup=_confirm_kb(),
    )


# ── 4. Подтверждение → отправка ──


@router.callback_query(InvoiceFromTemplateStates.confirm, F.data == "inv_confirm_send")
async def confirm_send(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] confirm_send tg:%d", callback.from_user.id)
    await callback.answer("⏳ Отправляю...")
    data = await state.get_data()
    template = data.get("_template", {})
    items_with_qty = data.get("_items_with_qty", [])

    if not items_with_qty:
        await callback.answer("❌ Нет позиций для отправки", show_alert=True)
        return

    logger.info(
        "[invoice][from_tpl] ▶ Отправка расходной tg:%d, шаблон=«%s» (pk=%s), "
        "store=%s (%s), counteragent=%s (%s), account=%s, items=%d",
        callback.from_user.id,
        template.get("name"),
        template.get("id"),
        template.get("store_id"),
        template.get("store_name"),
        template.get("counteragent_id"),
        template.get("counteragent_name"),
        template.get("account_id"),
        len(items_with_qty),
    )
    for idx, it in enumerate(items_with_qty, 1):
        logger.info(
            "[invoice][from_tpl]   item #%d: prod=%s, amount=%.4g, price=%.2f, "
            "sum=%.2f, unit=%s, main_unit=%s",
            idx,
            it.get("product_id"),
            it.get("amount", 0),
            it.get("price", 0),
            round(it.get("amount", 0) * it.get("price", 0), 2),
            it.get("unit_name"),
            it.get("main_unit"),
        )

    ctx = await uctx.get_user_context(callback.from_user.id)
    author_name = ctx.employee_name if ctx else ""

    comment = f"Шаблон: {template.get('name', '')}"
    if author_name:
        comment += f" (Автор: {author_name})"

    # Подтягиваем containerId для каждого продукта из iiko_product.raw_json
    product_ids = [it["product_id"] for it in items_with_qty if it.get("product_id")]
    containers = await inv_uc.get_product_containers(product_ids)

    document = inv_uc.build_outgoing_invoice_document(
        store_id=template["store_id"],
        counteragent_id=template["counteragent_id"],
        account_id=template["account_id"],
        items=items_with_qty,
        containers=containers,
        comment=comment,
    )
    logger.info(
        "[invoice][from_tpl] Документ построен: dateIncoming=%s, status=%s, "
        "store=%s, counteragent=%s, comment='%s', items_count=%d",
        document.get("dateIncoming"),
        document.get("status"),
        document.get("storeId"),
        document.get("counteragentId"),
        document.get("comment", "")[:80],
        len(document.get("items", [])),
    )

    result_text = await inv_uc.send_outgoing_invoice_document(document)

    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        result_text,
    )

    # Генерация и отправка PDF-документа
    if result_text.startswith("✅"):
        try:
            department_name = ""
            if ctx and ctx.department_name:
                department_name = ctx.department_name

            pdf_bytes = await asyncio.to_thread(
                pdf_uc.generate_invoice_pdf,
                items=items_with_qty,
                store_name=template.get("store_name", ""),
                counteragent_name=template.get("counteragent_name", ""),
                account_name=template.get("account_name", ""),
                department_name=department_name,
                author_name=author_name,
                comment=comment,
                total_sum=data.get("_total_sum"),
                doc_title="Расходная накладная",
            )
            filename = pdf_uc.generate_invoice_filename(
                counteragent_name=template.get("counteragent_name", ""),
                store_name=template.get("store_name", ""),
            )
            await callback.bot.send_document(
                callback.message.chat.id,
                BufferedInputFile(pdf_bytes, filename=filename),
                caption="📄 Расходная накладная (2 копии)",
            )
            logger.info(
                "[invoice][from_tpl] PDF отправлен: %s (%.1f КБ)",
                filename,
                len(pdf_bytes) / 1024,
            )
        except Exception:
            logger.exception("[invoice][from_tpl] Ошибка генерации PDF")

    logger.info("[invoice][from_tpl] ◀ Результат отправки: %s", result_text[:100])

    # Удаляем старые сообщения (header с шаблонами и т.д.)
    header_id = data.get("header_msg_id")
    if header_id and header_id != callback.message.message_id:
        try:
            await callback.bot.delete_message(callback.message.chat.id, header_id)
        except Exception:
            pass

    await state.clear()
    await restore_menu_kb(
        callback.bot,
        callback.message.chat.id,
        state,
        "📦 Накладные:",
        invoices_keyboard(),
    )


# ── Ввести заново (кол-во) ──


@router.callback_query(InvoiceFromTemplateStates.confirm, F.data == "inv_reenter_qty")
async def reenter_quantities(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[invoice_handlers] reenter_quantities tg:%d", callback.from_user.id)
    await callback.answer()
    data = await state.get_data()
    items = data.get("_items", [])

    text = "<b>Позиции:</b>\n"
    for i, item in enumerate(items, 1):
        price = item.get("sell_price", 0)
        unit = item.get("unit_name", "шт")
        norm = normalize_unit(unit)
        if norm == "kg":
            input_hint = "в граммах"
        elif norm == "l":
            input_hint = "в мл"
        else:
            input_hint = f"в {unit}"
        price_str = f" — {price:.2f}₽/{unit}" if price else ""
        text += f"  {i}. {item['name']}{price_str} → <i>{input_hint}</i>\n"

    text += "\n✏️ Введите количества заново (по числу на строке):"

    await state.set_state(InvoiceFromTemplateStates.enter_quantities)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        text,
    )


# ══════════════════════════════════════════════════════
#  Отмена (общая для обоих потоков)
# ══════════════════════════════════════════════════════


@router.callback_query(F.data == "inv_cancel")
async def cancel_template(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    logger.info("[invoice] Отмена tg:%d", callback.from_user.id)

    data = await state.get_data()
    # Удаляем header (summary), а prompt (текущее сообщение) редактируем
    header_id = data.get("header_msg_id")
    if header_id and header_id != callback.message.message_id:
        try:
            await callback.bot.delete_message(callback.message.chat.id, header_id)
        except Exception:
            pass

    await state.clear()
    try:
        await callback.message.edit_text("❌ Действие отменено.")
    except Exception:
        pass
    await restore_menu_kb(
        callback.bot,
        callback.message.chat.id,
        state,
        "📦 Накладные:",
        invoices_keyboard(),
    )
