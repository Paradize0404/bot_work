"""
Telegram-хэндлеры: заявки на товары + управление получателями.

Три FSM-потока:

A) Создание заявки (любой авторизованный сотрудник):
  1. Поиск товаров по названию, добавление по одному с вводом количества
     - Склад-получатель авто-определяется по типу склада + подразделению пользователя
     - Контрагент авто-определяется из iiko_supplier по имени целевого склада
  2. Подтверждение, авто-группировка по складам,
     создание N заявок (одна на каждый склад), уведомление получателям
  Блокировки:
     - Гости (нет подразделения): «Свяжитесь с администратором»
     - Пользователь на подразделении == «Заведение для заявок»: «Смените подразделение»

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
from bot.middleware import set_cancel_kb, restore_menu_kb
from bot._utils import requests_keyboard, items_inline_kb, send_prompt_msg
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
from use_cases import product_request as req_uc
from use_cases import user_context as uctx
from use_cases import admin as admin_uc
from use_cases import pdf_invoice as pdf_uc
from use_cases.writeoff import normalize_unit

logger = logging.getLogger(__name__)

router = Router(name="request_handlers")

MAX_ITEMS = 50

# ── Блокировки заявок (как в списаниях) ──
# pk → (admin_tg_id, admin_name) — кто сейчас обрабатывает
_request_locks: dict[int, tuple[int, str]] = {}
# pk → {admin_tg_id: message_id} — сообщения с кнопками у админов
_request_admin_msgs: dict[int, dict[int, int]] = {}


def _try_lock_request(pk: int, admin_tg: int, admin_name: str) -> bool:
    """Залочить заявку. True если лок получен, False если уже занята."""
    if pk in _request_locks:
        return False
    _request_locks[pk] = (admin_tg, admin_name)
    return True


def _unlock_request(pk: int) -> None:
    _request_locks.pop(pk, None)


def _get_lock_owner(pk: int) -> tuple[int, str] | None:
    return _request_locks.get(pk)


# ══════════════════════════════════════════════════════
#  FSM States
# ══════════════════════════════════════════════════════

class CreateRequestStates(StatesGroup):
    add_items = State()          # поиск товаров по названию
    enter_item_qty = State()     # ввод количества для выбранного товара
    confirm = State()


class EditRequestStates(StatesGroup):
    choose_item = State()          # выбор позиции для редактирования
    choose_action = State()        # выбор действия (наименование/количество/удалить)
    new_product_search = State()   # поиск нового товара
    new_quantity = State()         # ввод нового количества


class DuplicateRequestStates(StatesGroup):
    enter_quantities = State()   # ввод новых количеств для дубля заявки
    confirm = State()


# ══════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════

def _suppliers_kb(suppliers: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(suppliers, prefix="req_sup", cancel_data="req_cancel", page=page)


def _req_products_kb(products: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура найденных товаров для заявки."""
    return items_inline_kb(products, prefix="reqp", cancel_data="req_cancel", page=page)


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


def _history_kb(requests: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура истории заявок с кнопкой 'Повторить'."""
    total = len(requests)
    page_size = 10
    start = page * page_size
    end = start + page_size
    page_items = requests[start:end]

    buttons = []
    for r in page_items:
        created = r.get("created_at")
        date_str = created.strftime("%d.%m") if created else "?"
        status_icon = {"approved": "✅", "pending": "⏳", "cancelled": "❌"}.get(r.get("status", ""), "?")
        items_count = len(r.get("items", []))
        label = f"{status_icon} #{r['pk']} {date_str} · {r.get('counteragent_name', '?')[:20]} · {items_count} поз."
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"req_hist_view:{r['pk']}"),
            InlineKeyboardButton(text="🔄", callback_data=f"req_dup:{r['pk']}"),
        ])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"req_hist_page:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️ Далее", callback_data=f"req_hist_page:{page + 1}"))
    
    if nav:
        total_pages = (total + page_size - 1) // page_size
        nav.insert(len(nav) // 2, InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="noop",
        ))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="req_hist_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _history_detail_kb(pk: int) -> InlineKeyboardMarkup:
    """Клавиатура при просмотре одной заявки: назад + повторить."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Повторить заявку", callback_data=f"req_dup:{pk}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="req_hist_back")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="req_hist_close")],
    ])


def _dup_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить заявку", callback_data="dup_confirm_send")],
        [InlineKeyboardButton(text="✏️ Ввести заново", callback_data="dup_reenter")],
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


# ══════════════════════════════════════════════════════
#  Отмена (общая)
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "req_cancel")
async def cancel_request_flow(callback: CallbackQuery, state: FSMContext) -> None:
    logger.debug("[request] Отмена флоу tg:%d", callback.from_user.id)
    await callback.answer("Отменено")

    # Если отменяем редактирование — снять блокировку и вернуть кнопки
    data = await state.get_data()
    edit_pk = data.get("_edit_pk")
    if edit_pk:
        _unlock_request(edit_pk)
        await _resend_admin_buttons(callback.bot, edit_pk)

    await state.clear()
    try:
        await callback.message.edit_text("❌ Заявка отменена.")
    except Exception:
        pass
    await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                          "📋 Заявки:", requests_keyboard())


# ══════════════════════════════════════════════════════
#  Хелпер: edit-or-send prompt (как в invoice_handlers)
# ══════════════════════════════════════════════════════

async def _send_prompt(
    bot: Bot, chat_id: int, state: FSMContext,
    text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправить/обновить prompt-сообщение (edit если возможно, иначе — новое)."""
    await send_prompt_msg(
        bot, chat_id, state, text, reply_markup,
        state_key="_bot_msg_id", log_tag="request",
    )


# ══════════════════════════════════════════════════════
#  Хелпер: текст сводки добавленных позиций
# ══════════════════════════════════════════════════════

def _items_summary(items: list[dict], user_dept: str, settings_dept: str = "") -> str:
    """Формирует текст сводки добавленных товаров (плоский список, без деления по складам)."""
    header = f"📤 <b>{user_dept}</b>"
    if settings_dept:
        header += f" → 📥 <b>{settings_dept}</b>"
    text = header + "\n\n"
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

@router.message(F.text == "✏️ Создать заявку")
async def start_create_request(message: Message, state: FSMContext) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)

    # ── Гость (нет в справочнике / нет подразделения) → блок ──
    if not ctx or not ctx.department_id:
        await message.answer(
            "⚠️ Свяжитесь с администратором для получения доступа."
        )
        return

    await set_cancel_kb(message.bot, message.chat.id, state)

    logger.info(
        "[request] Старт создания заявки tg:%d, dept=%s (%s)",
        message.from_user.id, ctx.department_id, ctx.department_name,
    )

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Параллельная загрузка: account + настройки (заведение) + store_type_map
    account, settings_stores, user_store_map = await asyncio.gather(
        inv_uc.get_revenue_account(),
        req_uc.get_request_stores(),
        req_uc.build_store_type_map(ctx.department_id),
    )

    if not account:
        await message.answer("❌ Счёт реализации не найден.")
        return

    # ── Пользователь на подразделении == «Заведение для заявок» → блок ──
    settings_dept_id = settings_stores[0]["id"] if settings_stores else None
    if settings_dept_id and ctx.department_id == settings_dept_id:
        logger.info(
            "[request] Блокировка: user dept == settings dept, tg:%d", message.from_user.id,
        )
        await message.answer(
            "⚠️ Вы находитесь на подразделении, куда приходят заявки.\n"
            "Смените подразделение (🏠 Сменить ресторан) и попробуйте снова."
        )
        await restore_menu_kb(message.bot, message.chat.id, state,
                              "📋 Заявки:", requests_keyboard())
        return

    if not user_store_map:
        await message.answer(
            "⚠️ У вашего подразделения нет складов.\n"
            "Синхронизируйте подразделения и попробуйте снова."
        )
        return

    settings_dept_name = settings_stores[0]["name"] if settings_stores else ""

    await state.update_data(
        department_id=ctx.department_id,
        department_name=ctx.department_name,
        requester_name=ctx.employee_name,
        account_id=account["id"],
        account_name=account["name"],
        _user_store_map=user_store_map,
        _settings_dept_id=settings_dept_id,
        _settings_dept_name=settings_dept_name,
        items=[],
    )

    # Пропускаем выбор поставщика → сразу к поиску товаров
    await state.set_state(CreateRequestStates.add_items)
    await _send_prompt(message.bot, message.chat.id, state,
        f"📤 <b>{ctx.department_name}</b> → 📥 <b>{settings_dept_name}</b>\n\n"
        "🔍 Введите название товара для поиска:",
    )


# ── (шаг выбора поставщика удалён — контрагент авто-определяется) ──


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
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Введите название товара для поиска.")
        return

    if len(query) > 200:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Макс. 200 символов. Попробуйте короче.")
        return

    data = await state.get_data()
    items = data.get("items", [])
    if len(items) >= MAX_ITEMS:
        await _send_prompt(message.bot, message.chat.id, state,
            f"⚠️ Максимум {MAX_ITEMS} позиций. Нажмите «✅ Отправить заявку».",
            reply_markup=_req_add_more_kb(len(items)),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await inv_uc.search_price_products(query)

    if not products:
        await _send_prompt(message.bot, message.chat.id, state,
            f"🔍 По запросу «{query}» ничего не найдено.\n"
            "Введите другое название:",
            reply_markup=_req_add_more_kb(len(items)) if items else None,
        )
        return

    await state.update_data(_products_cache=products)
    await _send_prompt(message.bot, message.chat.id, state,
        f"🔍 Найдено {len(products)}. Выберите товар:",
        reply_markup=_req_products_kb(products, page=0),
    )


@router.callback_query(F.data.startswith("req_sup_page:"))
async def request_sup_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    suppliers = data.get("_suppliers_cache", [])
    if not suppliers:
        await callback.answer("Контрагенты не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=_suppliers_kb(suppliers, page=page))
    await callback.answer()


@router.callback_query(F.data.startswith("reqp_page:"))
async def request_prod_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    products = data.get("_products_cache", [])
    if not products:
        await callback.answer("Товары не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=_req_products_kb(products, page=page))
    await callback.answer()


@router.callback_query(F.data.startswith("req_hist_page:"))
async def request_hist_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    requests = data.get("_history_cache", [])
    if not requests:
        await callback.answer("История не найдена", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=_history_kb(requests, page=page))
    await callback.answer()


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

    # ── Блокировка: товар без склада ──
    if not product.get("store_id"):
        await callback.answer(
            "⚠️ У товара не указан склад. Выберите другой товар.",
            show_alert=True,
        )
        await state.set_state(CreateRequestStates.add_items)
        await _send_prompt(callback.bot, callback.message.chat.id, state,
            f"⚠️ У товара <b>{product['name']}</b> не указан склад "
            "в прайс-листе.\n\n"
            "Выберите другой товар и обратитесь к администратору "
            "для указания склада этому товару.\n\n"
            "🔍 Введите название другого товара:",
            reply_markup=_req_add_more_kb(len(items)) if items else None,
        )
        return

    await state.set_state(CreateRequestStates.enter_item_qty)

    # ── Определяем цену: сначала из столбца поставщика, потом себестоимость ──
    source_store_name = product.get("store_name", "")
    user_store_map = data.get("_user_store_map", {})
    target = req_uc.resolve_target_store(source_store_name, user_store_map)
    target_store_name = target["name"] if target else ""

    supplier_price = await inv_uc.get_supplier_price_for_product(
        product["id"], target_store_name,
    ) if target_store_name else None
    cost_price = product.get("cost_price", 0)
    display_price = supplier_price or cost_price or 0

    if supplier_price:
        price_str = f"\n💰 цена: {supplier_price:.2f}₽/{unit}"
    elif cost_price:
        price_str = f"\n💰 себест.: {cost_price:.2f}₽/{unit}"
    else:
        price_str = ""

    try:
        await callback.message.edit_text(
            f"📦 <b>{product['name']}</b>{price_str}\n\n"
            f"✏️ Введите количество ({hint}):",
            parse_mode="HTML",
        )
    except Exception:
        pass


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
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Введите число.")
        return

    try:
        qty = float(raw)
    except ValueError:
        await _send_prompt(message.bot, message.chat.id, state,
            f"⚠️ Не удалось распознать число: «{raw}». Введите заново.")
        return

    if qty <= 0:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Количество должно быть > 0.")
        return

    data = await state.get_data()
    product = data.get("_selected_product")
    if not product:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Ошибка: товар не выбран. Начните поиск заново.")
        await state.set_state(CreateRequestStates.add_items)
        return

    # Цена: сначала из столбца поставщика (target store), потом себестоимость
    cost_price = product.get("cost_price", 0)
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

    # ── Авто-определение целевого склада ──
    source_store_id = product.get("store_id", "")
    source_store_name = product.get("store_name", "")
    user_store_map = data.get("_user_store_map", {})

    target = req_uc.resolve_target_store(source_store_name, user_store_map)
    target_store_id = target["id"] if target else ""
    target_store_name = target["name"] if target else ""

    if not target and source_store_name:
        logger.warning(
            "[request] Не найден целевой склад для '%s' в подразделении %s, tg:%d",
            source_store_name, data.get("department_name"), message.from_user.id,
        )

    # ── Цена из столбца поставщика → fallback себестоимость ──
    supplier_price = await inv_uc.get_supplier_price_for_product(
        product["id"], target_store_name,
    ) if target_store_name else None
    price = supplier_price or cost_price or 0

    items = data.get("items", [])
    items.append({
        "product_id": product["id"],
        "name": product["name"],
        "amount": converted,
        "price": price,
        "cost_price": cost_price,
        "main_unit": product.get("main_unit"),
        "unit_name": unit,
        "sell_price": price,
        "qty_display": qty_display,
        "raw_qty": qty,
        # Склад-источник (из прайс-листа, для расходной накладной)
        "store_id": source_store_id,
        "store_name": source_store_name,
        # Целевой склад (подразделение пользователя, для группировки и отображения)
        "target_store_id": target_store_id,
        "target_store_name": target_store_name,
    })
    await state.update_data(items=items, _selected_product=None)

    logger.info(
        "[request] Добавлен товар #%d: «%s» qty=%s, price=%.2f, "
        "source_store=%s → target_store=%s, tg:%d",
        len(items), product["name"], qty_display, price,
        source_store_name or "?", target_store_name or "?", message.from_user.id,
    )

    # Показываем сводку + предлагаем добавить ещё
    summary = _items_summary(
        items, data.get("department_name", "?"), data.get("_settings_dept_name", ""),
    )

    await state.set_state(CreateRequestStates.add_items)
    await _send_prompt(message.bot, message.chat.id, state,
        f"{summary}\n\n"
        "🔍 Введите название следующего товара или отправьте заявку:",
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

    dept_name = data.get("department_name", "?")
    settings_dept = data.get("_settings_dept_name", "")
    if items:
        summary = _items_summary(items, dept_name, settings_dept)
        text = f"🗑 Удалено: {removed['name']}\n\n{summary}\n\n🔍 Введите название товара:"
    else:
        header = f"📤 <b>{dept_name}</b>"
        if settings_dept:
            header += f" → 📥 <b>{settings_dept}</b>"
        text = (
            f"🗑 Удалено: {removed['name']}\n\n"
            f"{header}\n\n"
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

    # Проверяем: у товаров должен быть склад-источник (из прайс-листа)
    no_store = [it for it in items if not it.get("store_id")]
    if no_store:
        names = "\n".join(f"  • {it['name']}" for it in no_store[:10])
        try:
            await callback.message.edit_text(
                f"❌ У {len(no_store)} товаров не назначен склад в прайс-листе:\n"
                f"{names}\n\n"
                "Выберите другие товары и обратитесь к администратору "
                "для указания склада этим товарам.",
                parse_mode="HTML",
                reply_markup=_req_add_more_kb(len(items)),
            )
        except Exception:
            pass
        return

    summary = _items_summary(
        items, data.get("department_name", "?"), data.get("_settings_dept_name", ""),
    )

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


# ── 8. Подтверждение → одна заявка + раздельные уведомления ──

@router.callback_query(CreateRequestStates.confirm, F.data == "req_confirm_send")
async def confirm_send_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("⏳ Отправляю заявку...")

    # Перепроверка авторизации на финальном шаге
    ctx = await uctx.get_user_context(callback.from_user.id)
    if not ctx or not ctx.department_id:
        await state.clear()
        try:
            await callback.message.edit_text("⚠️ Сессия истекла. Пожалуйста, авторизуйтесь (/start).")
        except Exception:
            pass
        return

    data = await state.get_data()
    items = data.get("items", [])

    if not items:
        await callback.answer("❌ Нет позиций", show_alert=True)
        return

    # ── Определяем контрагента (по первому целевому складу) ──
    first_target_name = ""
    first_source_name = ""
    first_source_id = ""
    for it in items:
        if it.get("target_store_name"):
            first_target_name = it["target_store_name"]
            first_source_id = it.get("store_id", "")
            first_source_name = it.get("store_name", "")
            break
    if not first_source_id:
        first_source_id = items[0].get("store_id", "")
        first_source_name = items[0].get("store_name", "?")

    counteragent = await req_uc.find_counteragent_for_store(first_target_name) if first_target_name else None
    if not counteragent:
        counteragent = await req_uc.find_counteragent_for_store(first_source_name)
    if not counteragent:
        logger.warning(
            "[request] Контрагент не найден для '%s' / '%s', tg:%d",
            first_target_name, first_source_name, callback.from_user.id,
        )
        try:
            await callback.message.edit_text(
                "❌ Не удалось определить контрагента.\n\n"
                "Проверьте, что склады зарегистрированы как контрагенты в iiko.",
                parse_mode="HTML",
                reply_markup=_confirm_kb(),
            )
        except Exception:
            pass
        return

    total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)

    # ── Создаём ОДНУ заявку со всеми позициями ──
    pk = await req_uc.create_request(
        requester_tg=callback.from_user.id,
        requester_name=data.get("requester_name", "?"),
        department_id=data["department_id"],
        department_name=data.get("department_name", "?"),
        store_id=first_source_id,
        store_name=first_source_name,
        counteragent_id=counteragent["id"],
        counteragent_name=counteragent["name"],
        account_id=data["account_id"],
        account_name=data.get("account_name", "?"),
        items=items,
        total_sum=total_sum,
    )

    logger.info(
        "[request] Создана заявка #%d, items=%d, tg:%d",
        pk, len(items), callback.from_user.id,
    )

    # ── Формируем текст заявки ──
    req_data = await req_uc.get_request_by_pk(pk)
    settings_dept = data.get("_settings_dept_name", "")
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)

    # ── Уведомления: админам → с кнопками, получателям → информативное ──
    admin_ids = await admin_uc.get_admin_ids()
    receiver_ids = await req_uc.get_receiver_ids()

    # Убираем пересечение (админ не дублируется в получателях)
    receiver_only = [tg for tg in receiver_ids if tg not in set(admin_ids)]

    if not admin_ids and not receiver_only:
        await callback.message.edit_text(
            f"✅ Заявка #{pk} сохранена, но нет назначенных получателей.\n"
            "Попросите администратора добавить получателей заявок."
        )
        await state.clear()
        await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                              "📋 Заявки:", requests_keyboard())
        return

    total_sent = 0
    admin_msg_ids: dict[int, int] = {}

    # Админам — полная заявка с кнопками управления
    for tg_id in admin_ids:
        try:
            msg = await callback.bot.send_message(
                tg_id, text, parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            admin_msg_ids[tg_id] = msg.message_id
            total_sent += 1
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить админа tg:%d: %s", tg_id, exc)

    # Сохраняем для блокировки
    _request_admin_msgs[pk] = admin_msg_ids

    # Получателям — информативное (без кнопок)
    info_text = text + "\n\n<i>ℹ️ Информационное уведомление</i>"
    for tg_id in receiver_only:
        try:
            await callback.bot.send_message(tg_id, info_text, parse_mode="HTML")
            total_sent += 1
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить получателя tg:%d: %s", tg_id, exc)

    logger.info(
        "[request] Заявка #%d: admin=%d, receiver=%d, sent=%d",
        pk, len(admin_ids), len(receiver_only), total_sent,
    )

    await callback.message.edit_text(
        f"✅ Заявка #{pk} отправлена!\nОжидайте подтверждения."
    )
    await state.clear()
    await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                          "📋 Заявки:", requests_keyboard())


# ══════════════════════════════════════════════════════
#  Защита: текст в inline-состояниях
# ══════════════════════════════════════════════════════

@router.message(CreateRequestStates.confirm)
@router.message(DuplicateRequestStates.confirm)
async def _ignore_text_request(message: Message) -> None:
    logger.debug("[request] Игнор текста в inline-состоянии tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  B) ОДОБРЕНИЕ / РЕДАКТИРОВАНИЕ / ОТКЛОНЕНИЕ ЗАЯВКИ
# ══════════════════════════════════════════════════════


async def _update_other_admin_msgs(
    bot: Bot, pk: int, status_text: str, except_admin: int = 0,
) -> None:
    """Убрать кнопки / обновить статус у всех админов кроме текущего."""
    msgs = _request_admin_msgs.get(pk, {})
    targets = [(tg, mid) for tg, mid in msgs.items() if tg != except_admin]
    if not targets:
        return

    req_data = await req_uc.get_request_by_pk(pk)
    settings_stores = await req_uc.get_request_stores()
    settings_dept = settings_stores[0]["name"] if settings_stores else ""
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
    text += f"\n\n{status_text}"

    for admin_tg, msg_id in targets:
        try:
            await bot.edit_message_text(
                chat_id=admin_tg, message_id=msg_id,
                text=text, parse_mode="HTML",
            )
        except Exception:
            pass


async def _resend_admin_buttons(bot: Bot, pk: int) -> None:
    """Обновить сообщения с заявкой у всех админов (редактирование или отправка новых)."""
    admin_ids = await admin_uc.get_admin_ids()
    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data or req_data["status"] != "pending":
        return
    settings_stores = await req_uc.get_request_stores()
    settings_dept = settings_stores[0]["name"] if settings_stores else ""
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
    
    # Получаем существующие сообщения
    existing_msgs = _request_admin_msgs.get(pk, {})
    new_msgs: dict[int, int] = {}
    
    for tg_id in admin_ids:
        msg_id = existing_msgs.get(tg_id)
        
        # Если есть существующее сообщение, пробуем его отредактировать
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=tg_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_approve_kb(pk),
                )
                new_msgs[tg_id] = msg_id
                logger.debug("[request] Отредактировано сообщение #%d для админа tg:%d в заявке #%d", 
                            msg_id, tg_id, pk)
                continue
            except Exception as exc:
                logger.warning("[request] Не удалось отредактировать сообщение #%d для tg:%d: %s. Отправляю новое.", 
                             msg_id, tg_id, exc)
        
        # Если нет существующего сообщения или редактирование не удалось, отправляем новое
        try:
            msg = await bot.send_message(
                tg_id, text, parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            new_msgs[tg_id] = msg.message_id
            logger.debug("[request] Отправлено новое сообщение #%d админу tg:%d для заявки #%d", 
                        msg.message_id, tg_id, pk)
        except Exception as exc:
            logger.warning("[request] Не удалось отправить сообщение админу tg:%d: %s", tg_id, exc)
    
    _request_admin_msgs[pk] = new_msgs


async def _finish_request_edit(callback: CallbackQuery, state: FSMContext, pk: int, change_description: str) -> None:
    """Завершить редактирование заявки: обновить сообщения с комментарием."""
    _unlock_request(pk)
    await state.clear()
    
    # Получаем обновлённую заявку
    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        return
    
    settings_stores = await req_uc.get_request_stores()
    settings_dept = settings_stores[0]["name"] if settings_stores else ""
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
    text += f"\n\n✏️ <i>Изменено: {change_description}</i>"
    
    admin_ids = await admin_uc.get_admin_ids()
    existing_msgs = _request_admin_msgs.get(pk, {})
    new_msgs: dict[int, int] = {}
    
    for admin_id in admin_ids:
        msg_id = existing_msgs.get(admin_id)
        
        if msg_id:
            try:
                await callback.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_approve_kb(pk),
                )
                new_msgs[admin_id] = msg_id
                continue
            except Exception:
                pass
        
        # Fallback: отправить новое сообщение только если редактирование не удалось
        try:
            msg = await callback.bot.send_message(
                admin_id, text, parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            new_msgs[admin_id] = msg.message_id
        except Exception:
            pass
    
    _request_admin_msgs[pk] = new_msgs


async def _finish_request_edit_msg(message: Message, state: FSMContext, pk: int, change_description: str) -> None:
    """То же, но из message-хэндлера."""
    _unlock_request(pk)
    await state.clear()
    
    # Получаем обновлённую заявку
    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        return
    
    settings_stores = await req_uc.get_request_stores()
    settings_dept = settings_stores[0]["name"] if settings_stores else ""
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
    text += f"\n\n✏️ <i>Изменено: {change_description}</i>"
    
    admin_ids = await admin_uc.get_admin_ids()
    existing_msgs = _request_admin_msgs.get(pk, {})
    new_msgs: dict[int, int] = {}
    
    for admin_id in admin_ids:
        msg_id = existing_msgs.get(admin_id)
        
        if msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_approve_kb(pk),
                )
                new_msgs[admin_id] = msg_id
                continue
            except Exception:
                pass
        
        # Fallback: отправить новое сообщение только если редактирование не удалось
        try:
            msg = await message.bot.send_message(
                admin_id, text, parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            new_msgs[admin_id] = msg.message_id
        except Exception:
            pass
    
    _request_admin_msgs[pk] = new_msgs


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

    # Проверка прав доступа
    if not await req_uc.is_receiver(callback.from_user.id) and not await admin_uc.is_admin(callback.from_user.id):
        await callback.answer("⚠️ Нет доступа", show_alert=True)
        logger.warning("[request] Попытка одобрить заявку без прав tg:%d", callback.from_user.id)
        return

    # Блокировка: только один админ может обрабатывать заявку
    admin_name = callback.from_user.full_name
    lock_owner = _get_lock_owner(pk)
    if lock_owner:
        owner_tg, owner_name = lock_owner
        if owner_tg != callback.from_user.id:
            await callback.answer(f"⏳ Заявку обрабатывает {owner_name}", show_alert=True)
            return
    if not _try_lock_request(pk, callback.from_user.id, admin_name):
        await callback.answer("⏳ Заявка уже обрабатывается", show_alert=True)
        return

    try:
        await _do_approve_request(callback, pk)
    finally:
        _unlock_request(pk)


async def _do_approve_request(callback: CallbackQuery, pk: int) -> None:
    """
    Одобрение заявки: группировка позиций по складам → N расходных накладных в iiko.
    Внешне — одна заявка, внутри — по накладной на каждый склад.
    """
    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    if req_data["status"] != "pending":
        await callback.answer(f"⚠️ Заявка уже {req_data['status']}", show_alert=True)
        return

    ctx = await uctx.get_user_context(callback.from_user.id)
    admin_name = ctx.employee_name if ctx else callback.from_user.full_name

    logger.info(
        "[request] Одобрение заявки #%d tg:%d (%s), items=%d",
        pk, callback.from_user.id, admin_name, len(req_data.get("items", [])),
    )

    # Показать статус текущему админу
    try:
        settings_stores = await req_uc.get_request_stores()
        settings_dept = settings_stores[0]["name"] if settings_stores else ""
        status_text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
        status_text += f"\n\n⏳ Отправляется в iiko... ({admin_name})"
        await callback.message.edit_text(status_text, parse_mode="HTML")
    except Exception:
        pass

    # Убрать кнопки у остальных админов
    await _update_other_admin_msgs(
        callback.bot, pk, f"✅ Отправляет {admin_name}",
        except_admin=callback.from_user.id,
    )

    items = req_data.get("items", [])
    product_ids = [it["product_id"] for it in items if it.get("product_id")]
    containers = await inv_uc.get_product_containers(product_ids)

    author_name = admin_name
    requester = req_data.get("requester_name", "?")

    # ── Группировка позиций по target_store_id → N накладных ──
    from collections import OrderedDict
    store_groups: dict[str, list[dict]] = OrderedDict()
    for it in items:
        sid = it.get("target_store_id") or it.get("store_id", "")
        if sid not in store_groups:
            store_groups[sid] = []
        store_groups[sid].append(it)

    all_results: list[str] = []
    any_success = False

    for group_store_id, group_items in store_groups.items():
        source_store_id = group_items[0].get("store_id", req_data["store_id"])
        target_store_name = group_items[0].get("target_store_name", "")
        source_store_name = group_items[0].get("store_name", "")

        # Авто-определение контрагента для этой группы
        counteragent = await req_uc.find_counteragent_for_store(target_store_name) if target_store_name else None
        if not counteragent:
            counteragent = await req_uc.find_counteragent_for_store(source_store_name) if source_store_name else None
        if not counteragent:
            # Fallback на request-level контрагента
            counteragent = {"id": req_data["counteragent_id"], "name": req_data["counteragent_name"]}

        comment = f"Заявка #{pk} от {requester}"
        if author_name:
            comment += f" (Отправил: {author_name})"

        document = inv_uc.build_outgoing_invoice_document(
            store_id=source_store_id,
            counteragent_id=counteragent["id"],
            account_id=req_data["account_id"],
            items=group_items,
            containers=containers,
            comment=comment,
        )

        try:
            result_text = await inv_uc.send_outgoing_invoice_document(document)
        except Exception as exc:
            logger.exception("[request] Ошибка отправки накладной #%d (store=%s)", pk, group_store_id)
            result_text = f"❌ Ошибка: {exc}"

        store_label = target_store_name or source_store_name or group_store_id
        all_results.append(f"{store_label}: {result_text}")
        if result_text.startswith("✅"):
            any_success = True

    # Если хотя бы одна успешна — помечаем заявку approved
    if any_success:
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

        # Генерация PDF
        try:
            pdf_bytes = await asyncio.to_thread(
                pdf_uc.generate_invoice_pdf,
                items=items,
                store_name=req_data.get("store_name", ""),
                counteragent_name=req_data.get("counteragent_name", ""),
                account_name=req_data.get("account_name", ""),
                department_name=req_data.get("department_name", ""),
                author_name=author_name,
                comment=f"Заявка #{pk} от {requester}",
                total_sum=req_data.get("total_sum"),
                doc_title="Расходная накладная",
            )
            filename = pdf_uc.generate_invoice_filename(
                counteragent_name=req_data.get("counteragent_name", ""),
                store_name=req_data.get("store_name", ""),
            )
            pdf_file = BufferedInputFile(pdf_bytes, filename=filename)
            await callback.bot.send_document(
                callback.message.chat.id,
                pdf_file,
                caption="📄 Расходная накладная (2 копии)",
            )
            try:
                pdf_file2 = BufferedInputFile(pdf_bytes, filename=filename)
                await callback.bot.send_document(
                    req_data["requester_tg"],
                    pdf_file2,
                    caption=f"📄 Накладная по заявке #{pk} (2 копии)",
                )
            except Exception:
                logger.warning("[request] Не удалось отправить PDF создателю tg:%d",
                               req_data["requester_tg"], exc_info=True)
            logger.info("[request] PDF отправлен: %s (%.1f КБ)",
                        filename, len(pdf_bytes) / 1024)
        except Exception:
            logger.exception("[request] Ошибка генерации PDF для заявки #%d", pk)

    # Итоговый текст
    combined_result = "\n".join(all_results) if len(store_groups) > 1 else all_results[0] if all_results else "?"
    updated_req = await req_uc.get_request_by_pk(pk)
    text = req_uc.format_request_text(updated_req or req_data)
    text += f"\n\n{combined_result}\n👤 {admin_name}"
    kb = _approve_kb(pk) if not any_success else None
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

    # Обновить остальных админов финальным статусом
    final_status = f"✅ Отправлена в iiko ({admin_name})" if any_success else f"❌ Ошибка отправки ({admin_name})"
    await _update_other_admin_msgs(
        callback.bot, pk, final_status,
        except_admin=callback.from_user.id,
    )
    # Очистить трекинг сообщений
    _request_admin_msgs.pop(pk, None)


# ── Редактировать количества (получатель) ──

@router.callback_query(F.data.startswith("req_edit:"))
async def start_edit_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    # Проверка прав доступа
    if not await req_uc.is_receiver(callback.from_user.id) and not await admin_uc.is_admin(callback.from_user.id):
        await callback.answer("⚠️ Нет доступа", show_alert=True)
        logger.warning("[request] Попытка редактировать заявку без прав tg:%d", callback.from_user.id)
        return

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

    # Блокировка: только один админ может редактировать
    ctx = await uctx.get_user_context(callback.from_user.id)
    admin_name = ctx.employee_name if ctx else callback.from_user.full_name
    lock_owner = _get_lock_owner(pk)
    if lock_owner:
        owner_tg, owner_name = lock_owner
        if owner_tg != callback.from_user.id:
            await callback.answer(f"⏳ Редактирует {owner_name}", show_alert=True)
            return
    if not _try_lock_request(pk, callback.from_user.id, admin_name):
        await callback.answer("⏳ Заявка уже обрабатывается", show_alert=True)
        return

    logger.info("[request] Начало редактирования #%d tg:%d (%s)", pk, callback.from_user.id, admin_name)

    # Убрать кнопки у остальных админов
    await _update_other_admin_msgs(
        callback.bot, pk, f"✏️ Редактирует {admin_name}",
        except_admin=callback.from_user.id,
    )

    items = req_data.get("items", [])
    
    if not items:
        await callback.answer("❌ В заявке нет позиций", show_alert=True)
        _unlock_request(pk)
        return

    # Показываем список позиций для выбора
    buttons = [
        [InlineKeyboardButton(
            text=f"{i}. {it['name']} — {it.get('qty_display', str(it.get('amount', 0)))} {it.get('unit_name', 'шт')}",
            callback_data=f"req_edit_item:{i-1}"
        )]
        for i, it in enumerate(items, 1)
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]]
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await state.clear()
    await state.update_data(_edit_pk=pk, _edit_items=items, _bot_msg_id=callback.message.message_id)
    await state.set_state(EditRequestStates.choose_item)

    try:
        await callback.message.edit_text(
            f"✏️ <b>Редактирование заявки #{pk}</b>\n\n📦 Какую позицию редактировать?",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception:
        pass


# ── Выбор позиции ──

@router.callback_query(EditRequestStates.choose_item, F.data.startswith("req_edit_item:"))
async def choose_item_to_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    idx_str = callback.data.split(":", 1)[1]
    try:
        idx = int(idx_str)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    
    data = await state.get_data()
    items = data.get("_edit_items", [])
    pk = data.get("_edit_pk")
    
    if idx < 0 or idx >= len(items):
        await callback.answer("❌ Позиция не найдена", show_alert=True)
        return
    
    item = items[idx]
    await state.update_data(_edit_item_idx=idx)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Сменить наименование", callback_data="req_edit_action:name")],
        [InlineKeyboardButton(text="🔢 Изменить количество", callback_data="req_edit_action:qty")],
        [InlineKeyboardButton(text="🗑 Удалить позицию", callback_data="req_edit_action:delete")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
    ])
    
    qty_display = item.get("qty_display", f"{item.get('amount', 0)} {item.get('unit_name', 'шт')}")
    await state.set_state(EditRequestStates.choose_action)
    
    try:
        await callback.message.edit_text(
            f"📦 Позиция #{idx+1}: <b>{item['name']}</b> — {qty_display}\n\nЧто меняем?",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception:
        pass


# ── Действие с позицией ──

@router.callback_query(EditRequestStates.choose_action, F.data.startswith("req_edit_action:"))
async def choose_edit_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    action = callback.data.split(":", 1)[1]
    
    data = await state.get_data()
    pk = data.get("_edit_pk")
    items = data.get("_edit_items", [])
    idx = data.get("_edit_item_idx", -1)
    
    if idx < 0 or idx >= len(items):
        await state.clear()
        return
    
    item = items[idx]
    
    if action == "delete":
        # Удалить позицию
        removed = items.pop(idx)
        logger.info("[request] Удалена позиция #%d: %s из заявки #%d", idx+1, removed.get('name'), pk)
        
        if not items:
            await callback.answer("⚠️ Нельзя удалить последнюю позицию", show_alert=True)
            items.insert(idx, removed)  # вернуть обратно
            return
        
        # Обновить заявку в БД
        total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)
        await req_uc.update_request_items(pk, items, total_sum)
        
        # Обновить сообщения у всех админов
        await _finish_request_edit(callback, state, pk, f"Удалена позиция: {removed.get('name')}")
        return
    
    elif action == "name":
        # Сменить наименование
        await state.set_state(EditRequestStates.new_product_search)
        try:
            await callback.message.edit_text(
                "🔍 Введите часть названия нового товара:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]
                ])
            )
        except Exception:
            pass
        return
    
    elif action == "qty":
        # Изменить количество
        unit = item.get("unit_name", "шт")
        norm = normalize_unit(unit)
        if norm == "kg":
            hint = "в граммах"
        elif norm == "l":
            hint = "в мл"
        else:
            hint = f"в {unit}"
        
        await state.set_state(EditRequestStates.new_quantity)
        try:
            await callback.message.edit_text(
                f"🔢 Введите новое количество ({hint}) для «{item['name']}»:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]
                ])
            )
        except Exception:
            pass
        return


# ── Поиск нового товара ──

@router.message(EditRequestStates.new_product_search)
async def edit_search_new_product(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    logger.info("[request] Поиск нового товара для заявки tg:%d, query='%s'", message.from_user.id, query)
    try:
        await message.delete()
    except Exception:
        pass
    
    if len(query) < 2:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Минимум 2 символа. Введите название товара:")
        return
    
    from use_cases import invoice_cache as inv_uc
    products = await inv_uc.search_price_products(query)
    
    if not products:
        await _send_prompt(message.bot, message.chat.id, state,
            "🔎 Ничего не найдено. Попробуйте другой запрос:")
        return
    
    await state.update_data(_edit_product_cache={p["id"]: p for p in products})
    
    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"req_edit_newprod:{p['id']}")]
        for p in products[:15]  # ограничиваем 15 позициями
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")]]
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await _send_prompt(message.bot, message.chat.id, state,
        f"🔍 Найдено {len(products)}. Выберите товар:",
        reply_markup=kb)


@router.callback_query(EditRequestStates.new_product_search, F.data.startswith("req_edit_newprod:"))
async def edit_pick_new_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    prod_id = callback.data.split(":", 1)[1]
    
    try:
        UUID(prod_id)
    except ValueError:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return
    
    data = await state.get_data()
    pk = data.get("_edit_pk")
    items = data.get("_edit_items", [])
    idx = data.get("_edit_item_idx", -1)
    cache = data.get("_edit_product_cache", {})
    product = cache.get(prod_id)
    
    if not product or idx < 0 or idx >= len(items):
        await state.clear()
        return
    
    old_name = items[idx]["name"]
    
    # Определяем склад-источник и целевой склад для нового товара
    source_store_id = product.get("store_id", "")
    source_store_name = product.get("store_name", "")
    
    user_store_map = data.get("_user_store_map", {})
    target = req_uc.resolve_target_store(source_store_name, user_store_map) if source_store_name else None
    target_store_id = target["id"] if target else ""
    target_store_name = target["name"] if target else ""
    
    # Цена: сначала из столбца поставщика, потом себестоимость
    from use_cases import invoice_cache as inv_uc
    supplier_price = await inv_uc.get_supplier_price_for_product(
        product["id"], target_store_name,
    ) if target_store_name else None
    cost_price = product.get("cost_price", 0)
    price = supplier_price or cost_price or 0
    
    # Сохраняем количество из старой позиции
    old_amount = items[idx].get("amount", 0)
    old_qty_display = items[idx].get("qty_display", "")
    old_raw_qty = items[idx].get("raw_qty", old_amount)
    
    items[idx] = {
        "product_id": product["id"],
        "name": product["name"],
        "amount": old_amount,
        "price": price,
        "cost_price": cost_price,
        "main_unit": product.get("main_unit"),
        "unit_name": product.get("unit_name", "шт"),
        "sell_price": price,
        "qty_display": old_qty_display,
        "raw_qty": old_raw_qty,
        "store_id": source_store_id,
        "store_name": source_store_name,
        "target_store_id": target_store_id,
        "target_store_name": target_store_name,
    }
    
    logger.info("[request] Позиция #%d заменена: %s → %s в заявке #%d", idx+1, old_name, product["name"], pk)
    
    # Обновить заявку в БД
    total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)
    await req_uc.update_request_items(pk, items, total_sum)
    
    # Обновить сообщения у всех админов
    await _finish_request_edit(callback, state, pk, f"Замена: {old_name} → {product['name']}")


# ── Ввод нового количества ──

@router.message(EditRequestStates.new_quantity)
async def edit_enter_new_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    logger.info("[request] Новое количество для заявки tg:%d, raw='%s'", message.from_user.id, raw)
    try:
        await message.delete()
    except Exception:
        pass
    
    if not raw:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Введите число.")
        return
    
    try:
        qty = float(raw)
    except ValueError:
        await _send_prompt(message.bot, message.chat.id, state,
            f"⚠️ Не удалось распознать число: «{raw}». Введите заново.")
        return
    
    if qty <= 0:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Количество должно быть > 0.")
        return
    
    data = await state.get_data()
    pk = data.get("_edit_pk")
    items = data.get("_edit_items", [])
    idx = data.get("_edit_item_idx", -1)
    
    if idx < 0 or idx >= len(items):
        await state.clear()
        return
    
    item = items[idx]
    unit = item.get("unit_name", "шт")
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
        qty_display = f"{qty:.4g} {unit}"
    
    items[idx]["amount"] = converted
    items[idx]["qty_display"] = qty_display
    items[idx]["raw_qty"] = qty
    
    logger.info("[request] Позиция #%d кол-во изменено: %s в заявке #%d", idx+1, qty_display, pk)
    
    # Обновить заявку в БД
    total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)
    await req_uc.update_request_items(pk, items, total_sum)
    
    # Обновить сообщения у всех админов
    await _finish_request_edit_msg(message, state, pk, f"{item['name']}: количество изменено на {qty_display}")


# ── Защита: текст в inline-состояниях ──

@router.message(EditRequestStates.choose_item)
@router.message(EditRequestStates.choose_action)
async def _ignore_text_edit_inline(message: Message) -> None:
    logger.debug("[request] Игнор текста в inline-состоянии редактирования tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


# ── Отклонить заявку ──

@router.callback_query(F.data.startswith("req_reject:"))
async def reject_request(callback: CallbackQuery) -> None:
    await callback.answer()

    # Проверка прав доступа
    if not await req_uc.is_receiver(callback.from_user.id) and not await admin_uc.is_admin(callback.from_user.id):
        await callback.answer("⚠️ Нет доступа", show_alert=True)
        logger.warning("[request] Попытка отклонить заявку без прав tg:%d", callback.from_user.id)
        return

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

    # Блокировка
    ctx = await uctx.get_user_context(callback.from_user.id)
    admin_name = ctx.employee_name if ctx else callback.from_user.full_name
    lock_owner = _get_lock_owner(pk)
    if lock_owner:
        owner_tg, owner_name = lock_owner
        if owner_tg != callback.from_user.id:
            await callback.answer(f"⏳ Заявку обрабатывает {owner_name}", show_alert=True)
            return

    await req_uc.cancel_request(pk, callback.from_user.id)
    logger.info("[request] Заявка #%d отклонена tg:%d (%s)", pk, callback.from_user.id, admin_name)

    # Уведомить создателя
    try:
        await callback.bot.send_message(
            req_data["requester_tg"],
            f"❌ Ваша заявка #{pk} отклонена.\nОтклонил: {admin_name}",
        )
    except Exception:
        pass

    updated_req = await req_uc.get_request_by_pk(pk)
    text = req_uc.format_request_text(updated_req or req_data)
    text += f"\n\n👤 Отклонил: {admin_name}"
    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass

    # Обновить остальных админов
    await _update_other_admin_msgs(
        callback.bot, pk, f"❌ Отклонена ({admin_name})",
        except_admin=callback.from_user.id,
    )
    _request_admin_msgs.pop(pk, None)
    _unlock_request(pk)


# ══════════════════════════════════════════════════════
#  D) ИСТОРИЯ ЗАЯВОК + ДУБЛИРОВАНИЕ
# ══════════════════════════════════════════════════════

@router.message(F.text == "📒 История заявок")
async def view_request_history(message: Message, state: FSMContext) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx:
        await message.answer("⚠️ Сначала авторизуйтесь (/start).")
        return

    logger.info("[request] История заявок tg:%d", message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    requests = await req_uc.get_user_requests(message.from_user.id, limit=10)

    if not requests:
        await message.answer("📋 У вас пока нет заявок.")
        return

    await state.update_data(_history_cache=requests)
    await message.answer(
        "📋 <b>Ваши последние заявки</b>\n"
        "<i>Нажмите 🔄 чтобы повторить заявку с новым количеством:</i>",
        parse_mode="HTML",
        reply_markup=_history_kb(requests, page=0),
    )


@router.callback_query(F.data.startswith("req_hist_view:"))
async def view_history_item(callback: CallbackQuery) -> None:
    await callback.answer()
    logger.debug("[request] Просмотр заявки из истории tg:%d", callback.from_user.id)
    pk_str = callback.data.split(":", 1)[1]
    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    text = req_uc.format_request_text(req_data)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_history_detail_kb(pk),
        )
    except Exception:
        pass  # «message is not modified» — игнорируем


@router.callback_query(F.data == "req_hist_back")
async def back_to_history_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат из карточки заявки к списку истории."""
    await callback.answer()
    requests = await req_uc.get_user_requests(callback.from_user.id, limit=10)
    if not requests:
        try:
            await callback.message.edit_text("📋 У вас пока нет заявок.")
        except Exception:
            pass
        return
    await state.update_data(_history_cache=requests)
    try:
        await callback.message.edit_text(
            "📋 <b>Ваши последние заявки</b>\n"
            "<i>Нажмите 🔄 чтобы повторить заявку с новым количеством:</i>",
            parse_mode="HTML",
            reply_markup=_history_kb(requests, page=0),
        )
    except Exception:
        pass


@router.callback_query(F.data == "req_hist_close")
async def close_history(callback: CallbackQuery) -> None:
    await callback.answer()
    logger.debug("[request] Закрытие истории tg:%d", callback.from_user.id)
    try:
        await callback.message.edit_text("📋 История закрыта.")
    except Exception:
        pass


# ── Дублирование заявки ──

@router.callback_query(F.data.startswith("req_dup:"))
async def start_duplicate_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pk_str = callback.data.split(":", 1)[1]
    try:
        pk = int(pk_str)
    except ValueError:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    req_data = await req_uc.get_request_by_pk(pk)
    if not req_data:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    logger.info("[request] Дублирование заявки #%d tg:%d", pk, callback.from_user.id)
    await set_cancel_kb(callback.bot, callback.message.chat.id, state)

    items = req_data.get("items", [])
    if not items:
        await callback.answer("⚠️ В этой заявке нет позиций", show_alert=True)
        return

    # Обновляем склады из текущего прайс-листа + строим маппинг для подразделения
    await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)

    ctx = await uctx.get_user_context(callback.from_user.id)
    user_dept_id = ctx.department_id if ctx else req_data["department_id"]

    store_map, user_store_map, settings_stores = await asyncio.gather(
        inv_uc.get_product_store_map([it.get("product_id", "") for it in items]),
        req_uc.build_store_type_map(user_dept_id),
        req_uc.get_request_stores(),
    )

    # Обогащаем items текущими данными о складах + авто-матч целевых
    for it in items:
        pid = it.get("product_id", "")
        if pid in store_map:
            it["store_id"] = store_map[pid]["store_id"]
            it["store_name"] = store_map[pid]["store_name"]
        source_store_name = it.get("store_name", "")
        target = req_uc.resolve_target_store(source_store_name, user_store_map)
        it["target_store_id"] = target["id"] if target else ""
        it["target_store_name"] = target["name"] if target else ""

    account = await inv_uc.get_revenue_account()

    settings_dept_name = settings_stores[0]["name"] if settings_stores else ""

    await state.clear()
    await state.update_data(
        _dup_source_pk=pk,
        department_id=user_dept_id,
        department_name=ctx.department_name if ctx else req_data["department_name"],
        requester_name=ctx.employee_name if ctx else req_data.get("requester_name", "?"),
        account_id=account["id"] if account else req_data["account_id"],
        account_name=account["name"] if account else req_data["account_name"],
        _dup_items=items,
        _user_store_map=user_store_map,
        _settings_dept_name=settings_dept_name,
    )

    # Показать позиции с текущими количествами
    dept_name = ctx.department_name if ctx else req_data.get("department_name", "?")

    # Предзагрузка цен поставщиков для отображения
    unique_target_stores = set(
        it.get("target_store_name", "") for it in items if it.get("target_store_name")
    )
    store_price_maps: dict[str, dict[str, float]] = {}
    for sn in unique_target_stores:
        store_price_maps[sn] = await inv_uc.get_supplier_prices_by_store(sn)

    header = f"📤 <b>{dept_name}</b>"
    if settings_dept_name:
        header += f" → 📥 <b>{settings_dept_name}</b>"
    text = (
        f"🔄 <b>Повторение заявки #{pk}</b>\n"
        f"{header}\n\n"
        f"<b>Позиции ({len(items)}):</b>\n"
    )
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
        target_sn = it.get("target_store_name", "")
        supplier_price = store_price_maps.get(target_sn, {}).get(it.get("product_id", ""))
        price = supplier_price or it.get("cost_price", 0) or it.get("price", 0)
        price_str = f" — {price:.2f}₽/{unit}" if price else ""
        text += f"  {i}. {it.get('name', '?')} — было: {current:.4g}{price_str} (в {hint})\n"

    text += (
        "\n✏️ <b>Введите новые количества</b>\n"
        "(по одному числу на строке, в том же порядке):"
    )

    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel")],
    ])

    await state.set_state(DuplicateRequestStates.enter_quantities)
    await state.update_data(_bot_msg_id=callback.message.message_id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb)
    except Exception:
        pass


@router.message(DuplicateRequestStates.enter_quantities)
async def dup_enter_quantities(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    logger.info("[request] Дубль: ввод кол-в tg:%d, raw='%s'", message.from_user.id, raw[:100])
    try:
        await message.delete()
    except Exception:
        pass

    if not raw:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Введите количества (по числу на строке).")
        return

    if len(raw) > 2000:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Слишком длинный ввод. Максимум 2000 символов.")
        return

    data = await state.get_data()
    items = data.get("_dup_items", [])

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
            await _send_prompt(message.bot, message.chat.id, state,
                f"⚠️ Не удалось распознать: «{p}». Введите заново.")
            return

    if len(quantities) != len(items):
        await _send_prompt(message.bot, message.chat.id, state,
            f"⚠️ Ожидается {len(items)} чисел, получено {len(quantities)}.\n"
            "Введите заново:"
        )
        return

    # ── Предзагрузка цен поставщиков для уникальных целевых складов ──
    unique_target_stores = set(
        it.get("target_store_name", "") for it in items if it.get("target_store_name")
    )
    store_price_maps: dict[str, dict[str, float]] = {}
    for sn in unique_target_stores:
        store_price_maps[sn] = await inv_uc.get_supplier_prices_by_store(sn)

    new_items: list[dict] = []
    total_sum = 0.0
    for i, (it, qty) in enumerate(zip(items, quantities), 1):
        if qty <= 0:
            continue

        target_sn = it.get("target_store_name", "")
        supplier_price = store_price_maps.get(target_sn, {}).get(it.get("product_id", ""))
        price = supplier_price or it.get("cost_price", 0) or it.get("price", 0)
        unit = it.get("unit_name", "шт")
        norm = normalize_unit(unit)

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

        new_items.append({
            "product_id": it.get("product_id"),
            "name": it.get("name", "?"),
            "amount": converted,
            "price": price,
            "cost_price": it.get("cost_price", 0),
            "main_unit": it.get("main_unit"),
            "unit_name": unit,
            "sell_price": price,
            "qty_display": qty_display,
            "raw_qty": qty,
            "store_id": it.get("store_id", ""),
            "store_name": it.get("store_name", ""),
            "target_store_id": it.get("target_store_id", ""),
            "target_store_name": it.get("target_store_name", ""),
        })

    if not new_items:
        await _send_prompt(message.bot, message.chat.id, state,
            "⚠️ Все позиции с количеством 0. Введите заново.")
        return

    source_pk = data.get("_dup_source_pk", "?")
    dept_name = data.get("department_name", "?")
    settings_dept = data.get("_settings_dept_name", "")
    summary = _items_summary(new_items, dept_name, settings_dept)
    text = f"📝 <b>Новая заявка (на основе #{source_pk})</b>\n\n{summary}"
    text += "\n\n<i>Проверьте и отправьте заявку.</i>"

    await state.update_data(
        _new_items=new_items,
        _total_sum=total_sum,
    )
    await state.set_state(DuplicateRequestStates.confirm)
    await _send_prompt(message.bot, message.chat.id, state,
        text, reply_markup=_dup_confirm_kb())


@router.callback_query(DuplicateRequestStates.confirm, F.data == "dup_reenter")
async def dup_reenter(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    items = data.get("_dup_items", [])

    text = f"<b>Позиции ({len(items)}):</b>\n"
    for i, it in enumerate(items, 1):
        unit = it.get("unit_name", "шт")
        norm = normalize_unit(unit)
        if norm == "kg":
            hint = "в граммах"
        elif norm == "l":
            hint = "в мл"
        else:
            hint = f"в {unit}"
        price = it.get("cost_price", 0) or it.get("price", 0)
        price_str = f" — {price:.2f}₽/{unit}" if price else ""
        text += f"  {i}. {it.get('name', '?')}{price_str} → <i>{hint}</i>\n"

    text += "\n✏️ Введите количества заново (по числу на строке):"

    await state.set_state(DuplicateRequestStates.enter_quantities)
    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(DuplicateRequestStates.confirm, F.data == "dup_confirm_send")
async def dup_confirm_send(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("⏳ Отправляю заявку...")

    # Перепроверка авторизации на финальном шаге
    ctx = await uctx.get_user_context(callback.from_user.id)
    if not ctx or not ctx.department_id:
        await state.clear()
        try:
            await callback.message.edit_text("⚠️ Сессия истекла. Пожалуйста, авторизуйтесь (/start).")
        except Exception:
            pass
        return

    data = await state.get_data()
    items = data.get("_new_items", [])

    if not items:
        await callback.answer("❌ Нет позиций", show_alert=True)
        return

    # Проверяем store_id у всех товаров
    no_store = [it for it in items if not it.get("store_id")]
    if no_store:
        names = "\n".join(f"  • {it['name']}" for it in no_store[:10])
        try:
            await callback.message.edit_text(
                f"❌ У {len(no_store)} товаров не назначен склад в прайс-листе:\n"
                f"{names}\n\n"
                "Выберите другие товары и обратитесь к администратору "
                "для указания склада этим товарам.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await state.clear()
        await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                              "📋 Заявки:", requests_keyboard())
        return

    source_pk = data.get("_dup_source_pk", "?")

    # ── Определяем контрагента (по первому целевому складу) ──
    first_target_name = ""
    first_source_id = ""
    first_source_name = ""
    for it in items:
        if it.get("target_store_name"):
            first_target_name = it["target_store_name"]
            first_source_id = it.get("store_id", "")
            first_source_name = it.get("store_name", "")
            break
    if not first_source_id:
        first_source_id = items[0].get("store_id", "")
        first_source_name = items[0].get("store_name", "?")

    counteragent = await req_uc.find_counteragent_for_store(first_target_name) if first_target_name else None
    if not counteragent:
        counteragent = await req_uc.find_counteragent_for_store(first_source_name)
    if not counteragent:
        logger.warning(
            "[request] Контрагент не найден для '%s' в дубле, tg:%d",
            first_target_name or first_source_name, callback.from_user.id,
        )
        try:
            await callback.message.edit_text(
                "❌ Не удалось определить контрагента.\n\n"
                "Проверьте, что склады зарегистрированы как контрагенты в iiko.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await state.clear()
        await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                              "📋 Заявки:", requests_keyboard())
        return

    total_sum = sum(it.get("amount", 0) * it.get("price", 0) for it in items)

    # ── Одна заявка со всеми позициями ──
    pk = await req_uc.create_request(
        requester_tg=callback.from_user.id,
        requester_name=data.get("requester_name", "?"),
        department_id=data["department_id"],
        department_name=data.get("department_name", "?"),
        store_id=first_source_id,
        store_name=first_source_name,
        counteragent_id=counteragent["id"],
        counteragent_name=counteragent["name"],
        account_id=data["account_id"],
        account_name=data.get("account_name", "?"),
        items=items,
        total_sum=total_sum,
    )

    logger.info("[request] Дубль #%s → новая #%d, items=%d, tg:%d",
                source_pk, pk, len(items), callback.from_user.id)

    # ── Формируем текст ──
    req_data = await req_uc.get_request_by_pk(pk)
    settings_stores = await req_uc.get_request_stores()
    settings_dept = settings_stores[0]["name"] if settings_stores else ""
    text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)
    text += f"\n\n🔄 <i>На основе заявки #{source_pk}</i>"

    # ── Уведомления: админам → с кнопками, получателям → информативное ──
    admin_ids = await admin_uc.get_admin_ids()
    receiver_ids = await req_uc.get_receiver_ids()
    receiver_only = [tg for tg in receiver_ids if tg not in set(admin_ids)]

    if not admin_ids and not receiver_only:
        await callback.message.edit_text(
            f"✅ Заявка #{pk} сохранена (дубль #{source_pk}), но нет получателей.\n"
            "Попросите администратора добавить получателей заявок."
        )
        await state.clear()
        await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                              "📋 Заявки:", requests_keyboard())
        return

    total_sent = 0
    admin_msg_ids: dict[int, int] = {}
    for tg_id in admin_ids:
        try:
            msg = await callback.bot.send_message(
                tg_id, text, parse_mode="HTML",
                reply_markup=_approve_kb(pk),
            )
            admin_msg_ids[tg_id] = msg.message_id
            total_sent += 1
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить админа tg:%d: %s", tg_id, exc)

    _request_admin_msgs[pk] = admin_msg_ids

    info_text = text + "\n\n<i>ℹ️ Информационное уведомление</i>"
    for tg_id in receiver_only:
        try:
            await callback.bot.send_message(tg_id, info_text, parse_mode="HTML")
            total_sent += 1
        except Exception as exc:
            logger.warning("[request] Не удалось уведомить получателя tg:%d: %s", tg_id, exc)

    logger.info(
        "[request] Дубль #%s → #%d, admin=%d, receiver=%d, sent=%d",
        source_pk, pk, len(admin_ids), len(receiver_only), total_sent,
    )

    await callback.message.edit_text(
        f"✅ Заявка #{pk} (дубль #{source_pk}) отправлена!\nОжидайте подтверждения."
    )
    await state.clear()
    await restore_menu_kb(callback.bot, callback.message.chat.id, state,
                          "📋 Заявки:", requests_keyboard())


# ══════════════════════════════════════════════════════
#  Просмотр заявок (получатели)
# ══════════════════════════════════════════════════════

@router.message(F.text == "📬 Входящие заявки")
async def view_pending_requests(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    is_rcv = await req_uc.is_receiver(message.from_user.id)
    is_adm = await admin_uc.is_admin(message.from_user.id)
    if not is_rcv and not is_adm:
        await message.answer("⚠️ У вас нет доступа к заявкам.")
        return

    logger.info("[request] Просмотр входящих заявок tg:%d", message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    pending, settings_stores = await asyncio.gather(
        req_uc.get_pending_requests_full(),
        req_uc.get_request_stores(),
    )
    settings_dept = settings_stores[0]["name"] if settings_stores else ""

    if not pending:
        await message.answer("📬 Нет ожидающих заявок.")
        return

    for req_data in pending[:10]:
        pk = req_data["pk"]
        text = req_uc.format_request_text(req_data, settings_dept_name=settings_dept)

        # Если заявка заблокирована — показать кем
        lock_owner = _get_lock_owner(pk)
        if lock_owner:
            _, owner_name = lock_owner
            text += f"\n\n⏳ Обрабатывает: {owner_name}"

        # Админы — кнопки управления (если не залочена), остальные — информативное
        if is_adm:
            kb = _approve_kb(pk) if not lock_owner else None
            msg = await message.answer(text, parse_mode="HTML", reply_markup=kb)
            # Обновляем трекинг сообщений
            if pk not in _request_admin_msgs:
                _request_admin_msgs[pk] = {}
            _request_admin_msgs[pk][message.from_user.id] = msg.message_id
        else:
            await message.answer(
                text + "\n\n<i>ℹ️ Информационное уведомление</i>",
                parse_mode="HTML",
            )


# DEPRECATED: Управление получателями перенесено в Google Таблицу
# (столбец «📬 Получатель» на листе «Права доступа»).
