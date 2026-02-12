"""
Telegram-хэндлеры: заявки на товары + управление получателями.

Три FSM-потока:

A) Создание заявки (любой авторизованный сотрудник):
  1. 🏬 Выбор склада
  2. 🏢 Выбор поставщика из прайс-листа
  3. 🔍 Поиск товаров по названию → добавление по одному с вводом количества
  4. ✅ Подтверждение → сохранение в БД + уведомление получателям

B) Просмотр / одобрение / редактирование заявки (получатели):
  - «✅ Отправить» → расходная накладная в iiko
  - «✏️ Редактировать» → изменить количества → отправить
  - «❌ Отменить» → заявка cancelled

C) Управление получателями (только админы):
  - Показать текущих | Добавить | Удалить
"""

import asyncio
import logging
import re
from uuid import UUID

from aiogram import Bot, Router, F
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from use_cases import outgoing_invoice as inv_uc
from use_cases import product_request as req_uc
from use_cases import user_context as uctx
from use_cases import admin as admin_uc
from use_cases.writeoff import normalize_unit

logger = logging.getLogger(__name__)

router = Router(name="request_handlers")

MAX_ITEMS = 50


# ══════════════════════════════════════════════════════
#  FSM States
# ══════════════════════════════════════════════════════

class CreateRequestStates(StatesGroup):
    store = State()
    supplier_choose = State()
    add_items = State()          # поиск товаров по названию
    enter_item_qty = State()     # ввод количества для выбранного товара
    confirm = State()


class EditRequestStates(StatesGroup):
    enter_quantities = State()   # получатель вводит новые количества


class ReceiverMgmtStates(StatesGroup):
    menu = State()
    choosing_employee = State()
    confirm_remove = State()


# ══════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════

def _stores_kb(stores: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=s["name"], callback_data=f"req_store:{s['id']}")]
        for s in stores
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _suppliers_kb(suppliers: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=s["name"], callback_data=f"req_sup:{s['id']}")]
        for s in suppliers
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _req_products_kb(products: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура найденных товаров для заявки."""
    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"reqp:{p['id']}")]
        for p in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _req_add_more_kb(items_count: int = 0) -> InlineKeyboardMarkup:
    """Кнопки после добавления товара: отправить / удалить последний / отмена."""
    buttons = []
    if items_count > 0:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Отправить заявку ({items_count} поз.)",
            callback_data="req_send",
        )])
        buttons.append([InlineKeyboardButton(
            text="🗑 Удалить последний товар",
            callback_data="req_remove_last",
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="req_confirm_send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
    ])


def _approve_kb(request_pk: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Отправить накладную в iiko",
            callback_data=f"req_approve:{request_pk}",
        )],
        [InlineKeyboardButton(
            text="✏️ Редактировать количества",
            callback_data=f"req_edit:{request_pk}",
        )],
        [InlineKeyboardButton(
            text="❌ Отменить заявку",
            callback_data=f"req_reject:{request_pk}",
        )],
    ])


def _receiver_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Текущие получатели", callback_data="rcv_list")],
        [InlineKeyboardButton(text="➕ Добавить получателя", callback_data="rcv_add")],
        [InlineKeyboardButton(text="➖ Удалить получателя", callback_data="rcv_remove")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="rcv_close")],
    ])


PAGE_SIZE = 8


def _employees_kb(employees: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    total = len(employees)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = employees[start:end]

    buttons = [
        [InlineKeyboardButton(
            text=f"{e['last_name']} {e['first_name']}",
            callback_data=f"rcv_pick:{e['telegram_id']}",
        )]
        for e in page_items
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rcv_emp_page:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rcv_emp_page:{page + 1}"))
    if nav:
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        nav.insert(len(nav) // 2, InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="rcv_noop"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="rcv_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _receivers_remove_kb(receivers: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {r['employee_name']}",
            callback_data=f"rcv_rm:{r['telegram_id']}",
        )]
        for r in receivers
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="rcv_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ══════════════════════════════════════════════════════
#  Отмена (общая)
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "req_cancel")
async def cancel_request_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    try:
        await callback.message.edit_text("❌ Заявка отменена.")
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  Хелпер: текст сводки добавленных позиций
# ══════════════════════════════════════════════════════

def _items_summary(items: list[dict], store_name: str, sup_name: str) -> str:
    """Формирует текст сводки добавленных товаров."""
    text = (
        f"🏬 <b>{store_name}</b>  ·  🏢 <b>{sup_name}</b>\n\n"
        f"<b>Позиции ({len(items)}):</b>\n"
    )
    total = 0.0
    for i, it in enumerate(items, 1):
        qty_display = it.get("qty_display", "")
        name = it["name"]
        price = it.get("price", 0)
        amount = it.get("amount", 0)
        line_sum = amount * price
        total += line_sum
        price_str = f" × {price:.2f}₽ = {line_sum:.2f}₽" if price else ""
        text += f"  {i}. {name}  ×  {qty_display}{price_str}\n"
    text += f"\n<b>Итого: {total:.2f}₽</b>"
    return text


# ══════════════════════════════════════════════════════
#  A) СОЗДАНИЕ ЗАЯВКИ
# ══════════════════════════════════════════════════════

@router.message(F.text == "📝 Создать заявку")
async def start_create_request(message: Message, state: FSMContext) -> None:
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала авторизуйтесь (/start) и выберите ресторан.")
        return

    logger.info(
        "[request] Старт создания заявки tg:%d, dept=%s (%s)",
        message.from_user.id, ctx.department_id, ctx.department_name,
    )

    stores, account, price_suppliers = await asyncio.gather(
        inv_uc.get_stores_for_department(ctx.department_id),
        inv_uc.get_revenue_account(),
        inv_uc.get_price_list_suppliers(),
    )

    if not stores:
        await message.answer("❌ Нет складов для вашего подразделения.")
        return
    if not account:
        await message.answer("❌ Счёт реализации не найден.")
        return
    if not price_suppliers:
        await message.answer("❌ В прайс-листе нет поставщиков.")
        return

    await state.update_data(
        department_id=ctx.department_id,
        department_name=ctx.department_name,
        requester_name=ctx.employee_name,
        account_id=account["id"],
        account_name=account["name"],
        _stores_cache=stores,
        _suppliers_cache=price_suppliers,
        items=[],
    )

    await state.set_state(CreateRequestStates.store)
    await message.answer("🏬 Выберите склад:", reply_markup=_stores_kb(stores))


# ── 1. Выбор склада ──

@router.callback_query(CreateRequestStates.store, F.data.startswith("req_store:"))
async def choose_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = callback.data.split(":", 1)[1]
    try:
        UUID(store_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    data = await state.get_data()
    stores = data.get("_stores_cache", [])
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        await callback.answer("❌ Склад не найден", show_alert=True)
        return

    logger.info("[request] Выбран склад: %s tg:%d", store["name"], callback.from_user.id)
    await state.update_data(store_id=store_id, store_name=store["name"])

    suppliers = data.get("_suppliers_cache", [])
    await state.set_state(CreateRequestStates.supplier_choose)
    await callback.message.edit_text(
        f"🏬 Склад: <b>{store['name']}</b>\n\n🏢 Выберите поставщика:",
        reply_markup=_suppliers_kb(suppliers),
        parse_mode="HTML",
    )


# ── 2. Выбор поставщика → переход к поиску товаров ──

@router.callback_query(CreateRequestStates.supplier_choose, F.data.startswith("req_sup:"))
async def choose_supplier(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    sup_id = callback.data.split(":", 1)[1]
    try:
        UUID(sup_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    data = await state.get_data()
    suppliers = data.get("_suppliers_cache", [])
    supplier = next((s for s in suppliers if s["id"] == sup_id), None)
    if not supplier:
        await callback.answer("❌ Поставщик не найден", show_alert=True)
        return

    logger.info("[request] Выбран поставщик: %s tg:%d", supplier["name"], callback.from_user.id)

    # Предзагружаем цены поставщика
    supplier_prices = await inv_uc.get_supplier_prices(sup_id)

    await state.update_data(
        counteragent_id=sup_id,
        counteragent_name=supplier["name"],
        _supplier_prices=supplier_prices,
    )

    # Переход к поиску товаров (как при создании шаблона)
    await state.set_state(CreateRequestStates.add_items)
    await callback.message.edit_text(
        f"🏬 <b>{data.get('store_name')}</b>  ·  🏢 <b>{supplier['name']}</b>\n\n"
        "🔍 Введите название товара для поиска:",
        parse_mode="HTML",
    )


# ── 3. Поиск товаров по названию ──

@router.message(CreateRequestStates.add_items)
async def search_request_product(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    logger.info("[request] Поиск товара tg:%d, query='%s'", message.from_user.id, query)
    try:
        await message.delete()
    except Exception:
        pass

    if not query:
        await message.answer("⚠️ Введите название товара для поиска.")
        return

    if len(query) > 200:
        await message.answer("⚠️ Макс. 200 символов. Попробуйте короче.")
        return

    data = await state.get_data()
    items = data.get("items", [])
    if len(items) >= MAX_ITEMS:
        await message.answer(
            f"⚠️ Максимум {MAX_ITEMS} позиций. Нажмите «✅ Отправить заявку».",
            reply_markup=_req_add_more_kb(len(items)),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await inv_uc.search_price_products(query)

    if not products:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не найдено.\n"
            "Введите другое название:",
            reply_markup=_req_add_more_kb(len(items)) if items else None,
        )
        return

    await state.update_data(_products_cache=products)
    await message.answer(
        f"🔍 Найдено {len(products)}. Выберите товар:",
        reply_markup=_req_products_kb(products),
    )


# ── 4. Выбор товара → запрос количества ──

@router.callback_query(CreateRequestStates.add_items, F.data.startswith("reqp:"))
async def choose_request_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    prod_id = callback.data.split(":", 1)[1]

    try:
        UUID(prod_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

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

    unit = product.get("unit_name", "шт")
    norm = normalize_unit(unit)
    if norm == "kg":
        hint = "в граммах"
    elif norm == "l":
        hint = "в мл"
    else:
        hint = f"в {unit}"

    # Сохраняем выбранный товар для следующего шага
    await state.update_data(_selected_product=product)
    await state.set_state(CreateRequestStates.enter_item_qty)

    supplier_prices = data.get("_supplier_prices", {})
    price = supplier_prices.get(prod_id, 0)
    price_str = f"\n💰 Цена: {price:.2f}₽/{unit}" if price else ""

    try:
        await callback.message.edit_text(
            f"📦 <b>{product['name']}</b>{price_str}\n\n"
            f"✏️ Введите количество ({hint}):",
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            f"📦 <b>{product['name']}</b>{price_str}\n\n"
            f"✏️ Введите количество ({hint}):",
            parse_mode="HTML",
        )


# ── 5. Ввод количества для выбранного товара ──

@router.message(CreateRequestStates.enter_item_qty)
async def enter_item_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    logger.info("[request] Ввод кол-ва tg:%d, raw='%s'", message.from_user.id, raw)
    try:
        await message.delete()
    except Exception:
        pass

    if not raw:
        await message.answer("⚠️ Введите число.")
        return

    try:
        qty = float(raw)
    except ValueError:
        await message.answer(f"⚠️ Не удалось распознать число: «{raw}». Введите заново.")
        return

    if qty <= 0:
        await message.answer("⚠️ Количество должно быть > 0.")
        return

    data = await state.get_data()
    product = data.get("_selected_product")
    if not product:
        await message.answer("⚠️ Ошибка: товар не выбран. Начните поиск заново.")
        await state.set_state(CreateRequestStates.add_items)
        return

    supplier_prices = data.get("_supplier_prices", {})
    price = supplier_prices.get(product["id"], product.get("sell_price", 0))
    unit = product.get("unit_name", "шт")
    norm = normalize_unit(unit)

    # Конвертация единиц
    if norm in ("kg", "l"):
        converted = qty / 1000
        display_unit = "г" if norm == "kg" else "мл"
        api_unit = "кг" if norm == "kg" else "л"
        qty_display = f"{qty:.4g} {display_unit} ({converted:.3g} {api_unit})"
    else:
        converted = qty
        display_unit = unit
        api_unit = unit
        qty_display = f"{qty:.4g} {unit}"

    items = data.get("items", [])
    items.append({
        "product_id": product["id"],
        "name": product["name"],
        "amount": converted,
        "price": price,
        "main_unit": product.get("main_unit"),
        "unit_name": unit,
        "sell_price": price,
        "qty_display": qty_display,
        "raw_qty": qty,
    })
    await state.update_data(items=items, _selected_product=None)

    logger.info(
        "[request] Добавлен товар #%d: «%s» qty=%s, price=%.2f, tg:%d",
        len(items), product["name"], qty_display, price, message.from_user.id,
    )

    # Показываем сводку + предлагаем добавить ещё
    store_name = data.get("store_name", "?")
    sup_name = data.get("counteragent_name", "?")
    summary = _items_summary(items, store_name, sup_name)

    await state.set_state(CreateRequestStates.add_items)
    await message.answer(
        f"{summary}\n\n"
        "🔍 Введите название следующего товара или отправьте заявку:",
        parse_mode="HTML",
        reply_markup=_req_add_more_kb(len(items)),
    )


# ── 6. Удалить последний товар ──

@router.callback_query(CreateRequestStates.add_items, F.data == "req_remove_last")
async def remove_last_item(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    items = data.get("items", [])
    if not items:
        await callback.answer("Список пуст", show_alert=True)
        return

    removed = items.pop()
    await state.update_data(items=items)

    store_name = data.get("store_name", "?")
    sup_name = data.get("counteragent_name", "?")

    if items:
        summary = _items_summary(items, store_name, sup_name)
        text = f"🗑 Удалено: {removed['name']}\n\n{summary}\n\n🔍 Введите название товара:"
    else:
        text = (
            f"🗑 Удалено: {removed['name']}\n\n"
            f"🏬 <b>{store_name}</b>  ·  🏢 <b>{sup_name}</b>\n\n"
            "🔍 Введите название товара для поиска:"
        )

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=_req_add_more_kb(len(items)) if items else None,
        )
    except Exception:
        pass


# ── 7. Превью заявки перед отправкой ──

@router.callback_query(CreateRequestStates.add_items, F.data == "req_send")
async def preview_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    items = data.get("items", [])
    if not items:
        await callback.answer("⚠️ Добавьте хотя бы одну позицию", show_alert=True)
        return

    store_name = data.get("store_name", "?")
    sup_name = data.get("counteragent_name", "?")
    summary = _items_summary(items, store_name, sup_name)

    await state.set_state(CreateRequestStates.confirm)
    try:
        await callback.message.edit_text(
            f"📝 <b>Подтверждение заявки</b>\n\n{summary}\n\n"
            "<i>Проверьте и отправьте заявку получателям.</i>",
            parse_mode="HTML",
            reply_markup=_confirm_kb(),
        )
    except Exception:
        pass


# ── 8. Подтверждение → отправка заявки получателям ──

@router.callback_query(CreateRequestStates.confirm, F.data == "req_confirm_send")
async def confirm_send_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("⏳ Отправляю заявку...")
    data = await state.get_data()
    items = data.get("items", [])

    if not items:
        await callback.answer("❌ Нет позиций", show_alert=True)
        return

    # Считаем total_sum
    total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)

    # Сохраняем заявку в БД
    pk = await req_uc.create_request(
        requester_tg=callback.from_user.id,
        requester_name=data.get("requester_name", "?"),
        department_id=data["department_id"],
        department_name=data.get("department_name", "?"),
        store_id=data["store_id"],
        store_name=data.get("store_name", "?"),
        counteragent_id=data["counteragent_id"],
        counteragent_name=data.get("counteragent_name", "?"),
        account_id=data["account_id"],
        account_name=data.get("account_name", "?"),
        items=items,
        total_sum=total_sum,
    )

    # Уведомить получателей
    receiver_ids = await req_uc.get_receiver_ids()
    req_data = await req_uc.get_request_by_pk(pk)

    if not receiver_ids:
        await callback.message.edit_text(
            f"✅ Заявка #{pk} сохранена, но нет назначенных получателей.\n"
            "Попросите администратора добавить получателей заявок."
        )
        await state.clear()
        return

    # Отправляем уведомление каждому получателю
    sent = 0
    text = req_uc.format_request_text(req_data)
    for tg_id in receiver_ids:
        try:
            await callback.bot.send_message(
                tg_id, text,
                parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            sent += 1
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить tg:%d: %s", tg_id, exc)

    logger.info(
        "[request] Заявка #%d отправлена %d/%d получателям",
        pk, sent, len(receiver_ids),
    )

    await callback.message.edit_text(
        f"✅ Заявка #{pk} отправлена получателям ({sent}/{len(receiver_ids)})!\n"
        f"Ожидайте подтверждения."
    )
    await state.clear()


# ══════════════════════════════════════════════════════
#  Защита: текст в inline-состояниях
# ══════════════════════════════════════════════════════

@router.message(CreateRequestStates.store)
@router.message(CreateRequestStates.supplier_choose)
@router.message(CreateRequestStates.confirm)
async def _ignore_text_request(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  B) ОДОБРЕНИЕ / РЕДАКТИРОВАНИЕ / ОТКЛОНЕНИЕ ЗАЯВКИ
# ══════════════════════════════════════════════════════

# ── Одобрить → отправить в iiko ──

@router.callback_query(F.data.startswith("req_approve:"))
async def approve_request(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Создаю накладную...")
    pk_str = callback.data.split(":", 1)[1]
    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    if req_data["status"] != "pending":
        await callback.answer(f"⚠️ Заявка уже {req_data['status']}", show_alert=True)
        return

    logger.info(
        "[request] Одобрение заявки #%d tg:%d, items=%d",
        pk, callback.from_user.id, len(req_data.get("items", [])),
    )

    # Собираем данные для расходной накладной
    items = req_data.get("items", [])
    product_ids = [it["product_id"] for it in items if it.get("product_id")]
    containers = await inv_uc.get_product_containers(product_ids)

    ctx = await uctx.get_user_context(callback.from_user.id)
    author_name = ctx.employee_name if ctx else ""
    requester = req_data.get("requester_name", "?")

    comment = f"Заявка #{pk} от {requester}"
    if author_name:
        comment += f" (Отправил: {author_name})"

    document = inv_uc.build_outgoing_invoice_document(
        store_id=req_data["store_id"],
        counteragent_id=req_data["counteragent_id"],
        account_id=req_data["account_id"],
        items=items,
        containers=containers,
        comment=comment,
    )

    result_text = await inv_uc.send_outgoing_invoice_document(document)

    # Если успех — помечаем заявку approved
    if result_text.startswith("✅"):
        await req_uc.approve_request(pk, callback.from_user.id)

        # Уведомить создателя
        try:
            await callback.bot.send_message(
                req_data["requester_tg"],
                f"✅ Ваша заявка #{pk} одобрена!\n"
                f"Накладная создана в iiko.\n"
                f"Отправил: {author_name or '?'}",
            )
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить создателя tg:%d: %s",
                           req_data["requester_tg"], exc)

    # Обновить сообщение у получателя
    updated_req = await req_uc.get_request_by_pk(pk)
    text = req_uc.format_request_text(updated_req or req_data)
    text += f"\n\n{result_text}"
    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(result_text)


# ── Редактировать количества (получатель) ──

@router.callback_query(F.data.startswith("req_edit:"))
async def start_edit_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pk_str = callback.data.split(":", 1)[1]
    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    if req_data["status"] != "pending":
        await callback.answer(f"⚠️ Заявка уже {req_data['status']}", show_alert=True)
        return

    items = req_data.get("items", [])

    text = f"✏️ <b>Редактирование заявки #{pk}</b>\n\n"
    for i, it in enumerate(items, 1):
        unit = it.get("unit_name", "шт")
        norm = normalize_unit(unit)
        if norm == "kg":
            hint = "граммах"
            current = it.get("amount", 0) * 1000
        elif norm == "l":
            hint = "мл"
            current = it.get("amount", 0) * 1000
        else:
            hint = unit
            current = it.get("amount", 0)
        text += f"  {i}. {it.get('name', '?')} — сейчас: {current:.4g} (в {hint})\n"

    text += (
        "\n✏️ <b>Введите новые количества</b>\n"
        "(по одному числу на строке, 0 = убрать позицию):"
    )

    await state.clear()
    await state.update_data(_edit_pk=pk, _edit_items=items)
    await state.set_state(EditRequestStates.enter_quantities)

    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, parse_mode="HTML")


@router.message(EditRequestStates.enter_quantities)
async def edit_quantities_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    logger.info("[request] Ввод новых кол-в tg:%d, raw='%s'", message.from_user.id, raw[:100])
    try:
        await message.delete()
    except Exception:
        pass

    if not raw:
        await message.answer("⚠️ Введите количества (по числу на строке).")
        return

    data = await state.get_data()
    pk = data.get("_edit_pk")
    items = data.get("_edit_items", [])

    # Парсим числа
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
            await message.answer(f"⚠️ Не удалось распознать: «{p}». Введите заново.")
            return

    if len(quantities) != len(items):
        await message.answer(
            f"⚠️ Ожидается {len(items)} чисел, получено {len(quantities)}.\n"
            "Введите заново:"
        )
        return

    # Собираем обновлённые позиции
    updated_items: list[dict] = []
    total_sum = 0.0
    for it, qty in zip(items, quantities):
        if qty == 0:
            continue

        unit = it.get("unit_name", "шт")
        norm = normalize_unit(unit)
        price = it.get("price", it.get("sell_price", 0))

        if norm in ("kg", "l"):
            converted = qty / 1000
            display_unit = "г" if norm == "kg" else "мл"
            api_unit = "кг" if norm == "kg" else "л"
            qty_display = f"{qty:.4g} {display_unit} ({converted:.3g} {api_unit})"
        else:
            converted = qty
            display_unit = unit
            api_unit = unit
            qty_display = f"{qty:.4g} {unit}"

        line_sum = converted * price
        total_sum += line_sum

        updated_items.append({
            **it,
            "amount": converted,
            "qty_display": qty_display,
            "raw_qty": qty,
        })

    if not updated_items:
        await message.answer("⚠️ Все позиции с количеством 0. Введите заново.")
        return

    # Обновить заявку в БД
    await req_uc.update_request_items(pk, updated_items, total_sum)

    # Показать обновлённую заявку
    req_data = await req_uc.get_request_by_pk(pk)
    text = req_uc.format_request_text(req_data)
    text += "\n\n✅ <i>Количества обновлены.</i>"

    await message.answer(
        text, parse_mode="HTML",
        reply_markup=_approve_kb(pk),
    )
    await state.clear()


# ── Отклонить заявку ──

@router.callback_query(F.data.startswith("req_reject:"))
async def reject_request(callback: CallbackQuery) -> None:
    await callback.answer()
    pk_str = callback.data.split(":", 1)[1]
    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    if req_data["status"] != "pending":
        await callback.answer(f"⚠️ Заявка уже {req_data['status']}", show_alert=True)
        return

    await req_uc.cancel_request(pk, callback.from_user.id)
    logger.info("[request] Заявка #%d отклонена tg:%d", pk, callback.from_user.id)

    # Уведомить создателя
    ctx = await uctx.get_user_context(callback.from_user.id)
    who = ctx.employee_name if ctx else "?"
    try:
        await callback.bot.send_message(
            req_data["requester_tg"],
            f"❌ Ваша заявка #{pk} отклонена.\nОтклонил: {who}",
        )
    except Exception:
        pass

    updated_req = await req_uc.get_request_by_pk(pk)
    text = req_uc.format_request_text(updated_req or req_data)
    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  Просмотр заявок (получатели)
# ══════════════════════════════════════════════════════

@router.message(F.text == "📬 Входящие заявки")
async def view_pending_requests(message: Message) -> None:
    is_rcv = await req_uc.is_receiver(message.from_user.id)
    is_adm = await admin_uc.is_admin(message.from_user.id)
    if not is_rcv and not is_adm:
        await message.answer("⚠️ У вас нет доступа к заявкам.")
        return

    logger.info("[request] Просмотр входящих заявок tg:%d", message.from_user.id)
    pending = await req_uc.get_pending_requests()

    if not pending:
        await message.answer("📬 Нет ожидающих заявок.")
        return

    for req in pending[:10]:  # Максимум 10 за раз
        req_data = await req_uc.get_request_by_pk(req["pk"])
        if req_data:
            text = req_uc.format_request_text(req_data)
            await message.answer(
                text, parse_mode="HTML",
                reply_markup=_approve_kb(req["pk"]),
            )


# ══════════════════════════════════════════════════════
#  C) УПРАВЛЕНИЕ ПОЛУЧАТЕЛЯМИ
# ══════════════════════════════════════════════════════

@router.message(F.text == "📬 Управление получателями")
async def start_receiver_mgmt(message: Message, state: FSMContext) -> None:
    is_adm = await admin_uc.is_admin(message.from_user.id)
    if not is_adm:
        await message.answer("⚠️ Только администраторы могут управлять получателями.")
        return

    logger.info("[request] Управление получателями tg:%d", message.from_user.id)
    await state.clear()
    await state.set_state(ReceiverMgmtStates.menu)
    await message.answer(
        "📬 <b>Управление получателями заявок</b>",
        parse_mode="HTML",
        reply_markup=_receiver_menu_kb(),
    )


# ── Список ──

@router.callback_query(ReceiverMgmtStates.menu, F.data == "rcv_list")
async def list_receivers_cb(callback: CallbackQuery) -> None:
    await callback.answer()
    text = await req_uc.format_receiver_list()
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=_receiver_menu_kb(),
        )
    except Exception:
        pass


# ── Добавить ──

@router.callback_query(ReceiverMgmtStates.menu, F.data == "rcv_add")
async def add_receiver_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    available = await req_uc.get_available_for_receiver()
    if not available:
        try:
            await callback.message.edit_text(
                "ℹ️ Нет доступных сотрудников для добавления.\n"
                "(Все авторизованные уже получатели, или никто не авторизован.)",
                reply_markup=_receiver_menu_kb(),
            )
        except Exception:
            pass
        return

    await state.update_data(_available=available)
    await state.set_state(ReceiverMgmtStates.choosing_employee)
    try:
        await callback.message.edit_text(
            "👤 Выберите сотрудника для добавления:",
            reply_markup=_employees_kb(available),
        )
    except Exception:
        pass


@router.callback_query(ReceiverMgmtStates.choosing_employee, F.data.startswith("rcv_pick:"))
async def pick_receiver(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_str = callback.data.split(":", 1)[1]
    try:
        tg_id = int(tg_str)
    except ValueError:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    data = await state.get_data()
    available = data.get("_available", [])
    emp = next((e for e in available if e["telegram_id"] == tg_id), None)
    if not emp:
        await callback.answer("❌ Сотрудник не найден", show_alert=True)
        return

    added = await req_uc.add_receiver(
        telegram_id=tg_id,
        employee_id=emp["id"],
        employee_name=emp["name"],
        added_by=callback.from_user.id,
    )

    if added:
        msg = f"✅ {emp['name']} добавлен как получатель заявок."
    else:
        msg = f"ℹ️ {emp['name']} уже является получателем."

    await state.set_state(ReceiverMgmtStates.menu)
    try:
        await callback.message.edit_text(
            msg, reply_markup=_receiver_menu_kb(),
        )
    except Exception:
        pass


@router.callback_query(ReceiverMgmtStates.choosing_employee, F.data.startswith("rcv_emp_page:"))
async def page_employees(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    available = data.get("_available", [])
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_employees_kb(available, page),
        )
    except Exception:
        pass


# ── Удалить ──

@router.callback_query(ReceiverMgmtStates.menu, F.data == "rcv_remove")
async def remove_receiver_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    receivers = await req_uc.list_receivers()
    if not receivers:
        try:
            await callback.message.edit_text(
                "ℹ️ Список получателей пуст.",
                reply_markup=_receiver_menu_kb(),
            )
        except Exception:
            pass
        return

    await state.set_state(ReceiverMgmtStates.confirm_remove)
    try:
        await callback.message.edit_text(
            "❌ Выберите получателя для удаления:",
            reply_markup=_receivers_remove_kb(receivers),
        )
    except Exception:
        pass


@router.callback_query(ReceiverMgmtStates.confirm_remove, F.data.startswith("rcv_rm:"))
async def confirm_remove_receiver(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_str = callback.data.split(":", 1)[1]
    try:
        tg_id = int(tg_str)
    except ValueError:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    removed = await req_uc.remove_receiver(tg_id)
    msg = f"✅ Получатель tg:{tg_id} удалён." if removed else "ℹ️ Не найден."

    await state.set_state(ReceiverMgmtStates.menu)
    try:
        await callback.message.edit_text(msg, reply_markup=_receiver_menu_kb())
    except Exception:
        pass


# ── Закрыть ──

@router.callback_query(F.data == "rcv_close")
async def close_receiver_mgmt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_text("📬 Управление получателями закрыто.")
    except Exception:
        pass


@router.callback_query(F.data == "rcv_noop")
async def noop_receiver(callback: CallbackQuery) -> None:
    await callback.answer()
