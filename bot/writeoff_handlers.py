"""
Telegram-хэндлеры: акт списания (writeoff) + проверка админами.

Флоу:
  1. Сотрудник создаёт акт (склад → счёт → причина → товары → количество)
  2. Нажимает «Отправить» → документ уходит ВСЕМ админам на проверку
  3. Админ видит акт с кнопками: ✅ Отправить | ✏️ Редактировать | ❌ Отклонить
  4. Если один админ нажал — у остальных кнопки убираются (нет задвоений)
  5. Редактирование: склад / счёт / позиции → номер → наименование или количество

Оптимизации (из предыдущей версии):
  - TTL-кеш, FSM-кеш, preload на «📄 Документы»
  - callback.answer() ПЕРВЫМ
  - Защита от дурака: текст в inline-состояниях, double-click, лимиты
"""

import asyncio
import logging

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

from use_cases import admin as admin_uc
from use_cases import writeoff as wo_uc
from use_cases import writeoff_cache as wo_cache
from use_cases import user_context as uctx
from use_cases import pending_writeoffs as pending
from use_cases import writeoff_history as wo_hist
from bot.middleware import (
    set_cancel_kb,
    restore_menu_kb,
    validate_callback_uuid,
    validate_callback_int,
    extract_callback_value,
    MAX_TEXT_SEARCH,
    truncate_input,
)
from bot._utils import (
    writeoffs_keyboard,
    items_inline_kb,
    send_prompt_msg,
    update_summary_msg,
    safe_page,
)

logger = logging.getLogger(__name__)

router = Router(name="writeoff_handlers")

# Защита от повторной отправки
_sending_lock: set[int] = set()

MAX_ITEMS = 50
QTY_MIN = 0.001
QTY_MAX = 99999


# ══════════════════════════════════════════════════════
#  FSM States — создание акта (сотрудник)
# ══════════════════════════════════════════════════════


class WriteoffStates(StatesGroup):
    store = State()
    account = State()
    reason = State()
    add_items = State()
    quantity = State()


# ══════════════════════════════════════════════════════
#  FSM States — редактирование акта (админ)
# ══════════════════════════════════════════════════════


class AdminEditStates(StatesGroup):
    choose_field = State()  # склад / счёт / позиции
    choose_store = State()  # выбор нового склада
    choose_account = State()  # выбор нового счёта
    choose_item_idx = State()  # какой номер позиции
    choose_item_action = State()  # наименование или количество
    new_product_search = State()  # поиск нового товара
    new_quantity = State()  # ввод нового количества


# ══════════════════════════════════════════════════════
#  FSM States — история списаний
# ══════════════════════════════════════════════════════


class HistoryStates(StatesGroup):
    browsing = State()  # просмотр списка
    viewing = State()  # детальный просмотр одной записи
    editing_reason = State()  # редактирование причины перед повтором
    editing_items = State()  # поиск товаров при редактировании
    editing_quantity = State()  # ввод количества при редактировании


# ══════════════════════════════════════════════════════
#  Клавиатуры — создание
# ══════════════════════════════════════════════════════


def _stores_kb(stores: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(
        stores, prefix="wo_store", cancel_data="wo_cancel", page=page
    )


ACC_PAGE_SIZE = 10

_ACC_PREFIXES = ("списание кухня", "списание бар", "списание")


def _short_acc_name(full_name: str) -> str:
    """Strip 'Списание кухня/бар' prefix and capitalise the remainder."""
    low = full_name.lower().strip()
    for prefix in _ACC_PREFIXES:
        if low.startswith(prefix):
            tail = full_name[len(prefix) :].strip()
            return tail[:1].upper() + tail[1:] if tail else full_name
    return full_name


def _accounts_kb(accounts: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    total = len(accounts)
    start = page * ACC_PAGE_SIZE
    end = start + ACC_PAGE_SIZE
    page_items = accounts[start:end]

    buttons = [
        [
            InlineKeyboardButton(
                text=_short_acc_name(a["name"]), callback_data=f"wo_acc:{a['id']}"
            )
        ]
        for a in page_items
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"wo_acc_page:{page - 1}"
            )
        )
    if end < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️ Далее", callback_data=f"wo_acc_page:{page + 1}"
            )
        )
    if nav:
        total_pages = (total + ACC_PAGE_SIZE - 1) // ACC_PAGE_SIZE
        nav.insert(
            len(nav) // 2,
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="wo_noop",
            ),
        )
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _products_kb(products: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    return items_inline_kb(
        products, prefix="wo_prod", cancel_data="wo_cancel", page=page
    )


def _add_more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить на проверку", callback_data="wo_send"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")],
        ]
    )


# ══════════════════════════════════════════════════════
#  Summary-сообщение
# ══════════════════════════════════════════════════════


def _build_summary(data: dict) -> str:
    store = data.get("store_name", "—")
    account = data.get("account_name", "—")
    reason = data.get("reason") or "—"
    user = data.get("user_fullname", "—")

    text = (
        f"📄 <b>Акт списания</b>\n"
        f"🏬 <b>Склад:</b> {store}\n"
        f"📂 <b>Счёт списания:</b> {account}\n"
        f"📝 <b>Причина:</b> {reason}\n"
        f"👤 <b>Сотрудник:</b> {user}"
    )
    items = data.get("items", [])
    if items:
        text += "\n\n<b>Позиции:</b>"
        for i, item in enumerate(items, 1):
            uq = item.get("user_quantity", item.get("quantity", 0))
            unit_label = item.get("unit_label", "шт")
            text += f"\n  {i}. {item['name']} — {uq} {unit_label}"
    return text


async def _update_summary(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await update_summary_msg(bot, chat_id, state, _build_summary, log_tag="writeoff")


async def _send_prompt(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    await send_prompt_msg(bot, chat_id, state, text, reply_markup, log_tag="writeoff")


# ══════════════════════════════════════════════════════
#  Защита: текст в inline-состояниях
# ══════════════════════════════════════════════════════


@router.message(WriteoffStates.store)
async def _ignore_text_store(message: Message) -> None:
    logger.debug(
        "[writeoff] Текст в store-состоянии tg:%d, text='%s'",
        message.from_user.id,
        message.text,
    )
    try:
        await message.delete()
    except Exception:
        pass


@router.message(WriteoffStates.account)
async def _ignore_text_account(message: Message) -> None:
    logger.debug(
        "[writeoff] Текст в account-состоянии tg:%d, text='%s'",
        message.from_user.id,
        message.text,
    )
    try:
        await message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  СОЗДАНИЕ АКТА (сотрудник) — шаги 1–7
# ══════════════════════════════════════════════════════

# ── 1. Старт ──


@router.message(F.text == "📝 Создать списание")
async def start_writeoff(message: Message, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] start_writeoff tg:%d", message.from_user.id)
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
        "[writeoff] Старт. user=%d, dept=%s (%s), role=%s",
        message.from_user.id,
        ctx.department_id,
        ctx.department_name,
        ctx.role_name,
    )

    # Фоновый прогрев кеша номенклатуры с фильтрацией по папкам подразделения
    asyncio.create_task(wo_uc.preload_products(ctx.department_id))

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    # Параллельно: has_permission + prepare_writeoff
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    is_bot_admin = await perm_uc.has_permission(
        message.from_user.id, PERM_WRITEOFF_APPROVE
    )
    wo_start = await wo_uc.prepare_writeoff(
        department_id=ctx.department_id,
        role_name=ctx.role_name,
        is_bot_admin=is_bot_admin,
    )

    if not wo_start.stores:
        await message.answer("❌ У вашего подразделения нет складов (бар/кухня).")
        return

    await state.update_data(
        user_fullname=ctx.employee_name,
        department_id=ctx.department_id,
        items=[],
        _stores_cache=wo_start.stores,
    )

    # Авто-выбор склада по должности
    if wo_start.auto_store and wo_start.accounts:
        auto_store = wo_start.auto_store
        accounts = wo_start.accounts
        await state.update_data(
            store_id=auto_store["id"], store_name=auto_store["name"]
        )
        logger.info(
            "[writeoff] Авто-склад по роли «%s» → %s (%s)",
            ctx.role_name,
            auto_store["name"],
            auto_store["id"],
        )

        summary_msg = await message.answer(
            _build_summary(await state.get_data()), parse_mode="HTML"
        )
        await state.update_data(header_msg_id=summary_msg.message_id)

        await state.update_data(_accounts_cache=accounts)
        await state.set_state(WriteoffStates.account)
        msg = await message.answer(
            f"🏬 Склад: <b>{auto_store['name']}</b> (авто)\n"
            f"📂 Выберите счёт списания ({len(accounts)}):",
            parse_mode="HTML",
            reply_markup=_accounts_kb(accounts, page=0),
        )
        await state.update_data(prompt_msg_id=msg.message_id)
        return

    if wo_start.auto_store and not wo_start.accounts:
        # Авто-склад найден, но нет счетов
        auto_store = wo_start.auto_store
        await state.update_data(
            store_id=auto_store["id"], store_name=auto_store["name"]
        )
        summary_msg = await message.answer(
            _build_summary(await state.get_data()), parse_mode="HTML"
        )
        await state.update_data(header_msg_id=summary_msg.message_id)
        msg = await message.answer(
            f"🏬 Склад: <b>{auto_store['name']}</b> (авто)\n"
            "⚠️ Нет счетов списания для этого склада.",
            parse_mode="HTML",
        )
        await state.update_data(prompt_msg_id=msg.message_id)
        await state.clear()
        return

    # Ручной выбор склада
    summary_msg = await message.answer(
        _build_summary(await state.get_data()), parse_mode="HTML"
    )
    await state.update_data(header_msg_id=summary_msg.message_id)
    msg = await message.answer(
        "🏬 Выберите склад:", reply_markup=_stores_kb(wo_start.stores, page=0)
    )
    await state.update_data(prompt_msg_id=msg.message_id)


@router.callback_query(F.data.startswith("wo_store_page:"))
async def writeoff_store_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] writeoff_store_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    stores = data.get("_stores_cache", [])
    if not stores:
        await callback.answer("Склады не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=_stores_kb(stores, page=page))
    await state.set_state(WriteoffStates.store)


# ── 2. Выбор склада ──


@router.callback_query(WriteoffStates.store, F.data.startswith("wo_store:"))
async def choose_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = await validate_callback_uuid(callback, callback.data)
    if not store_id:
        return
    logger.info(
        "[writeoff] Выбор склада tg:%d, store_id=%s", callback.from_user.id, store_id
    )
    data = await state.get_data()
    stores = data.get("_stores_cache") or await wo_uc.get_stores_for_department(
        data["department_id"]
    )
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        await callback.answer("❌ Склад не найден", show_alert=True)
        return

    await state.update_data(store_id=store_id, store_name=store["name"])
    logger.info("[writeoff] Склад: %s (%s)", store["name"], store_id)
    await _update_summary(callback.bot, callback.message.chat.id, state)

    accounts = await wo_uc.get_writeoff_accounts(store["name"])
    if not accounts:
        await _send_prompt(
            callback.bot,
            callback.message.chat.id,
            state,
            "⚠️ Нет счетов списания для этого склада.",
        )
        await state.clear()
        return

    await state.update_data(_accounts_cache=accounts)
    await state.set_state(WriteoffStates.account)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        f"📂 Выберите счёт списания ({len(accounts)}):",
        reply_markup=_accounts_kb(accounts, page=0),
    )


# ── 3. Выбор счёта ──


@router.callback_query(WriteoffStates.account, F.data == "wo_noop")
async def noop_callback(callback: CallbackQuery) -> None:
    logger.info("[writeoff_handlers] noop_callback tg:%d", callback.from_user.id)
    await callback.answer()


@router.callback_query(WriteoffStates.account, F.data.startswith("wo_acc_page:"))
async def accounts_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = await validate_callback_int(callback, callback.data)
    if page is None:
        return
    logger.debug(
        "[writeoff] Пагинация счетов tg:%d, page=%d", callback.from_user.id, page
    )
    data = await state.get_data()
    accounts = data.get("_accounts_cache") or await wo_uc.get_writeoff_accounts(
        data.get("store_name", "")
    )
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        f"📂 Выберите счёт списания ({len(accounts)}):",
        reply_markup=_accounts_kb(accounts, page=page),
    )


@router.callback_query(WriteoffStates.account, F.data.startswith("wo_acc:"))
async def choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = await validate_callback_uuid(callback, callback.data)
    if not account_id:
        return
    logger.info(
        "[writeoff] Выбор счёта tg:%d, acc_id=%s", callback.from_user.id, account_id
    )
    data = await state.get_data()
    accounts = data.get("_accounts_cache") or await wo_uc.get_writeoff_accounts(
        data.get("store_name", "")
    )
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        await callback.answer("❌ Счёт не найден", show_alert=True)
        return

    await state.update_data(account_id=account_id, account_name=account["name"])
    logger.info("[writeoff] Счёт: %s (%s)", account["name"], account_id)
    await _update_summary(callback.bot, callback.message.chat.id, state)
    await state.set_state(WriteoffStates.reason)
    await _send_prompt(
        callback.bot,
        callback.message.chat.id,
        state,
        "📝 Введите причину списания:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")],
            ]
        ),
    )


# ── 4. Причина ──


@router.message(WriteoffStates.reason)
async def set_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    logger.info(
        "[writeoff] Ввод причины tg:%d, len=%d", message.from_user.id, len(reason)
    )
    try:
        await message.delete()
    except Exception:
        pass

    if not reason:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Причина не может быть пустой. Введите причину:",
        )
        return
    if len(reason) > 500:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Макс. 500 символов. Введите причину покороче:",
        )
        return

    await state.update_data(reason=reason)
    logger.info("[writeoff] Причина: %s", reason)
    await _update_summary(message.bot, message.chat.id, state)
    await state.set_state(WriteoffStates.add_items)
    await _send_prompt(
        message.bot,
        message.chat.id,
        state,
        "🔍 Введите часть названия товара для поиска:",
    )


# ── 5. Поиск товара ──


@router.message(WriteoffStates.add_items)
async def search_product(message: Message, state: FSMContext) -> None:
    query = truncate_input((message.text or "").strip(), MAX_TEXT_SEARCH)
    logger.info(
        "[writeoff] Поиск товара tg:%d, query='%s'", message.from_user.id, query
    )
    try:
        await message.delete()
    except Exception:
        pass
    if not query:
        return
    if len(query) < 2:
        data = await state.get_data()
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "❌ Минимум 2 символа для поиска:",
            reply_markup=_add_more_kb() if data.get("items") else None,
        )
        return

    data = await state.get_data()
    if len(data.get("items", [])) >= MAX_ITEMS:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            f"⚠️ Макс. {MAX_ITEMS} позиций. Нажмите «Отправить».",
            reply_markup=_add_more_kb(),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await wo_uc.search_products(
        query, department_id=data.get("department_id")
    )
    if not products:
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "🔎 Ничего не найдено. Попробуйте другой запрос:",
            reply_markup=_add_more_kb() if data.get("items") else None,
        )
        return

    cache = {p["id"]: p for p in products}
    await state.update_data(product_cache=cache, _products_list=products)
    sel_id = data.get("selection_msg_id")
    kb = _products_kb(products, page=0)
    if sel_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sel_id,
                text=f"Найдено {len(products)}. Выберите товар:",
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    msg = await message.answer(
        f"Найдено {len(products)}. Выберите товар:", reply_markup=kb
    )
    await state.update_data(selection_msg_id=msg.message_id)


@router.callback_query(F.data.startswith("wo_prod_page:"))
async def writeoff_prod_page(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] writeoff_prod_page tg:%d", callback.from_user.id)
    await callback.answer()
    page = safe_page(callback.data)
    data = await state.get_data()
    products = data.get("_products_list", [])
    if not products:
        await callback.answer("Товары не найдены", show_alert=True)
        return
    await callback.message.edit_reply_markup(
        reply_markup=_products_kb(products, page=page)
    )


@router.callback_query(WriteoffStates.add_items, F.data.startswith("wo_prod:"))
async def select_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    product_id = await validate_callback_uuid(callback, callback.data)
    if not product_id:
        return
    logger.info(
        "[writeoff] Выбор товара tg:%d, prod_id=%s", callback.from_user.id, product_id
    )
    data = await state.get_data()
    product = data.get("product_cache", {}).get(product_id)
    if not product:
        await callback.answer("❌ Товар не найден. Повторите поиск.", show_alert=True)
        return

    logger.info("[writeoff] Товар: %s (%s)", product["name"], product_id)

    # Единицы уже заполнены в search_products (batch-resolve), fallback на DB
    unit_name = product.get("unit_name") or await wo_uc.get_unit_name(
        product.get("main_unit")
    )
    norm = product.get("unit_norm") or wo_uc.normalize_unit(unit_name)

    if norm == "kg":
        prompt = f"📏 Сколько <b>грамм</b> для «{product['name']}»?"
        unit_label = "г"
    elif norm == "l":
        prompt = f"📏 Сколько <b>мл</b> для «{product['name']}»?"
        unit_label = "мл"
    else:
        prompt = f"📏 Сколько <b>{unit_name}</b> для «{product['name']}»?"
        unit_label = unit_name

    _qty_cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")],
        ]
    )

    # Удаляем старый prompt (поиск товара), чтобы не задваивалось
    old_prompt_id = data.get("prompt_msg_id")
    if old_prompt_id and old_prompt_id != callback.message.message_id:
        try:
            await callback.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=old_prompt_id
            )
        except Exception:
            pass

    await state.update_data(
        current_item=product,
        current_unit_name=unit_name,
        current_unit_norm=norm,
        current_unit_label=unit_label,
        selection_msg_id=None,
    )
    await state.set_state(WriteoffStates.quantity)
    try:
        await callback.message.edit_text(
            prompt, parse_mode="HTML", reply_markup=_qty_cancel_kb
        )
    except Exception:
        msg = await callback.message.answer(
            prompt, parse_mode="HTML", reply_markup=_qty_cancel_kb
        )
        await state.update_data(
            quantity_prompt_id=msg.message_id, prompt_msg_id=msg.message_id
        )
        return
    await state.update_data(
        quantity_prompt_id=callback.message.message_id,
        prompt_msg_id=callback.message.message_id,
    )


# ── 6. Количество ──


@router.message(WriteoffStates.quantity)
async def save_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", ".").strip()
    logger.info("[writeoff] Ввод количества tg:%d, raw='%s'", message.from_user.id, raw)
    try:
        await message.delete()
    except Exception:
        pass

    try:
        qty = float(raw)
    except ValueError:
        await _send_prompt(
            message.bot, message.chat.id, state, "⚠️ Введите число. Пример: 500 или 1.5"
        )
        return
    if qty < QTY_MIN:
        await _send_prompt(message.bot, message.chat.id, state, f"⚠️ Минимум {QTY_MIN}.")
        return
    if qty > QTY_MAX:
        await _send_prompt(message.bot, message.chat.id, state, f"⚠️ Макс. {QTY_MAX}.")
        return

    data = await state.get_data()
    item = data.get("current_item")
    if not item:
        await state.set_state(WriteoffStates.add_items)
        await _send_prompt(
            message.bot,
            message.chat.id,
            state,
            "⚠️ Что-то пошло не так. Введите название товара заново:",
        )
        return

    norm = data.get("current_unit_norm", "pcs")
    unit_label = data.get("current_unit_label", "шт")
    converted = qty / 1000 if norm in ("kg", "l") else qty

    item["quantity"] = converted
    item["user_quantity"] = qty
    item["unit_label"] = unit_label

    items = data.get("items", [])
    items.append(item)

    await state.update_data(items=items, current_item=None, quantity_prompt_id=None)
    logger.info(
        "[writeoff] Позиция: %s — %s %s (→ %s), всего: %d",
        item.get("name"),
        qty,
        unit_label,
        converted,
        len(items),
    )
    await _update_summary(message.bot, message.chat.id, state)
    await state.set_state(WriteoffStates.add_items)
    await _send_prompt(
        message.bot,
        message.chat.id,
        state,
        "🔍 Введите название товара или нажмите «Отправить»:",
        reply_markup=_add_more_kb(),
    )


# ══════════════════════════════════════════════════════
#  7. ОТПРАВКА НА ПРОВЕРКУ АДМИНАМ
# ══════════════════════════════════════════════════════


@router.callback_query(WriteoffStates.add_items, F.data == "wo_send")
async def finalize_writeoff(callback: CallbackQuery, state: FSMContext) -> None:
    """Вместо прямой отправки — отправляем документ на проверку админам."""
    await callback.answer()
    user_id = callback.from_user.id
    logger.info("[writeoff] Отправка на проверку tg:%d", user_id)
    if user_id in _sending_lock:
        return

    _sending_lock.add(user_id)
    try:
        data = await state.get_data()
        items = data.get("items", [])
        if not items:
            await _send_prompt(
                callback.bot,
                callback.message.chat.id,
                state,
                "⚠️ Добавьте хотя бы один товар.",
                reply_markup=_add_more_kb(),
            )
            return
        non_zero = [i for i in items if i.get("quantity", 0) > 0]
        if not non_zero:
            await _send_prompt(
                callback.bot,
                callback.message.chat.id,
                state,
                "⚠️ Все позиции с количеством 0.",
                reply_markup=_add_more_kb(),
            )
            return

        from use_cases import permissions as perm_uc
        from bot.permission_map import PERM_WRITEOFF_APPROVE

        admin_ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)

        if not admin_ids:
            # Нет админов — отправляем напрямую (fallback)
            await _send_prompt(
                callback.bot,
                callback.message.chat.id,
                state,
                f"⏳ Отправляем акт ({len(non_zero)} позиций)...",
            )
            bot = callback.bot
            chat_id = callback.message.chat.id
            tg_id = callback.from_user.id
            _data_snapshot = dict(data)
            await state.clear()
            await restore_menu_kb(
                bot, chat_id, state, "📝 Списания:", writeoffs_keyboard()
            )

            async def _bg():
                result = await wo_uc.finalize_without_admins(
                    store_id=_data_snapshot["store_id"],
                    account_id=_data_snapshot["account_id"],
                    reason=_data_snapshot.get("reason", ""),
                    items=items,
                    author_name=_data_snapshot.get("user_fullname", ""),
                )
                await bot.send_message(chat_id, result)
                # Сохраняем в историю при успехе
                if result.startswith("✅"):
                    try:
                        await wo_hist.save_to_history(
                            telegram_id=tg_id,
                            employee_name=_data_snapshot.get("user_fullname", ""),
                            department_id=_data_snapshot.get("department_id", ""),
                            store_id=_data_snapshot["store_id"],
                            store_name=_data_snapshot.get("store_name", ""),
                            account_id=_data_snapshot["account_id"],
                            account_name=_data_snapshot.get("account_name", ""),
                            reason=_data_snapshot.get("reason", ""),
                            items=items,
                        )
                    except Exception:
                        logger.warning(
                            "[writeoff] Ошибка сохранения в историю (no-admin)"
                        )

            asyncio.create_task(_bg())
            return

        # Создаём pending-документ
        doc = await pending.create(
            author_chat_id=callback.message.chat.id,
            author_name=data.get("user_fullname", "—"),
            store_id=data["store_id"],
            store_name=data.get("store_name", "—"),
            account_id=data["account_id"],
            account_name=data.get("account_name", "—"),
            reason=data.get("reason", ""),
            department_id=data.get("department_id", ""),
            items=items,
        )

        await _send_prompt(
            callback.bot,
            callback.message.chat.id,
            state,
            "✅ Акт отправлен на проверку администраторам. Ожидайте.",
        )
        await state.clear()
        await restore_menu_kb(
            callback.bot,
            callback.message.chat.id,
            state,
            "📝 Списания:",
            writeoffs_keyboard(),
        )

        # Рассылаем всем админам
        bot = callback.bot
        text = pending.build_summary_text(doc)
        kb = pending.admin_keyboard(doc.doc_id)

        for admin_id in admin_ids:
            try:
                msg = await bot.send_message(
                    admin_id, text, parse_mode="HTML", reply_markup=kb
                )
                doc.admin_msg_ids[admin_id] = msg.message_id
            except Exception as exc:
                logger.warning(
                    "[writeoff] Не удалось отправить админу %d: %s", admin_id, exc
                )

        await pending.save_admin_msg_ids(doc.doc_id, doc.admin_msg_ids)
        logger.info(
            "[writeoff] Документ %s отправлен %d админам",
            doc.doc_id,
            len(doc.admin_msg_ids),
        )
    finally:
        _sending_lock.discard(user_id)


# ══════════════════════════════════════════════════════
#  ОБРАБОТКА АДМИНАМИ
# ══════════════════════════════════════════════════════


async def _remove_admin_keyboards(
    bot: Bot, doc: pending.PendingWriteoff, status_text: str, except_admin: int = 0
) -> None:
    """Убрать кнопки у всех админов (один из них уже обработал)."""
    for admin_id, msg_id in doc.admin_msg_ids.items():
        if admin_id == except_admin:
            continue
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=msg_id,
                text=pending.build_summary_text(doc) + f"\n\n{status_text}",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── Одобрить ──


@router.callback_query(F.data.startswith("woa_approve:"))
async def admin_approve(callback: CallbackQuery) -> None:
    logger.info("[writeoff_handlers] admin_approve tg:%d", callback.from_user.id)
    await callback.answer()
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    if not await perm_uc.has_permission(callback.from_user.id, PERM_WRITEOFF_APPROVE):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        logger.warning(
            "[security] Попытка одобрения без прав tg:%d", callback.from_user.id
        )
        return
    doc_id = extract_callback_value(callback.data)
    if not doc_id:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return
    logger.info("[writeoff] Одобрение tg:%d, doc=%s", callback.from_user.id, doc_id)
    doc = await pending.get(doc_id)
    if not doc:
        await callback.answer(
            "⚠️ Документ уже обработан или не найден.", show_alert=True
        )
        return

    if not await pending.try_lock(doc_id):
        await callback.answer(
            "⏳ Другой админ уже обрабатывает этот документ.", show_alert=True
        )
        return

    bot = callback.bot
    admin_id = callback.from_user.id
    admin_name = callback.from_user.full_name

    # Обновляем сообщение текущего админа
    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc)
            + f"\n\n⏳ Отправляется в iiko... ({admin_name})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Убираем кнопки у остальных
    await _remove_admin_keyboards(
        bot, doc, f"✅ Одобрено admin {admin_name}", except_admin=admin_id
    )

    # Отправляем в iiko через use_case
    try:
        approval = await wo_uc.approve_writeoff(doc)
    except Exception as exc:
        logger.exception("[writeoff] Ошибка одобрения doc=%s", doc_id)
        try:
            await callback.message.edit_text(
                pending.build_summary_text(doc)
                + f"\n\n❌ Ошибка отправки в iiko: {exc}\n👤 {admin_name}\n"
                "Попробуйте ещё раз.",
                parse_mode="HTML",
                reply_markup=pending.admin_keyboard(doc_id),
            )
        except Exception:
            pass
        await pending.unlock(doc_id)
        return

    # Сохраняем в историю при успешной отправке
    if approval.success:
        try:
            await wo_hist.save_to_history(
                telegram_id=doc.author_chat_id,
                employee_name=doc.author_name,
                department_id=doc.department_id,
                store_id=doc.store_id,
                store_name=doc.store_name,
                account_id=doc.account_id,
                account_name=doc.account_name,
                reason=doc.reason,
                items=doc.items,
                approved_by_name=admin_name,
            )
        except Exception:
            logger.warning(
                "[writeoff] Ошибка сохранения в историю doc=%s", doc_id, exc_info=True
            )

    # Обновляем сообщение админа
    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc)
            + f"\n\n{approval.result_text}\n👤 {admin_name}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Уведомляем автора
    try:
        await bot.send_message(
            doc.author_chat_id, f"{approval.result_text}\n(проверил: {admin_name})"
        )
    except Exception:
        pass

    await pending.remove(doc_id)
    logger.info(
        "[writeoff] Документ %s одобрен admin %d (%s)", doc_id, admin_id, admin_name
    )


# ── Отклонить ──


@router.callback_query(F.data.startswith("woa_reject:"))
async def admin_reject(callback: CallbackQuery) -> None:
    logger.info("[writeoff_handlers] admin_reject tg:%d", callback.from_user.id)
    await callback.answer()
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    if not await perm_uc.has_permission(callback.from_user.id, PERM_WRITEOFF_APPROVE):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        logger.warning(
            "[security] Попытка отклонения без прав tg:%d", callback.from_user.id
        )
        return
    doc_id = extract_callback_value(callback.data)
    if not doc_id:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return
    logger.info("[writeoff] Отклонение tg:%d, doc=%s", callback.from_user.id, doc_id)
    doc = await pending.get(doc_id)
    if not doc:
        await callback.answer("⚠️ Документ уже обработан.", show_alert=True)
        return
    if not await pending.try_lock(doc_id):
        await callback.answer("⏳ Другой админ уже обрабатывает.", show_alert=True)
        return

    bot = callback.bot
    admin_name = callback.from_user.full_name

    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc) + f"\n\n❌ Отклонено ({admin_name})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _remove_admin_keyboards(
        bot, doc, f"❌ Отклонено admin {admin_name}", except_admin=callback.from_user.id
    )
    try:
        await bot.send_message(
            doc.author_chat_id,
            f"❌ Акт списания отклонён администратором ({admin_name}).",
        )
    except Exception:
        pass

    await pending.remove(doc_id)
    logger.info(
        "[writeoff] Документ %s отклонён admin %d", doc_id, callback.from_user.id
    )


# ══════════════════════════════════════════════════════
#  РЕДАКТИРОВАНИЕ АДМИНОМ
# ══════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("woa_edit:"))
async def admin_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Админ решил отредактировать документ."""
    logger.info("[writeoff_handlers] admin_edit_start tg:%d", callback.from_user.id)
    await callback.answer()
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    if not await perm_uc.has_permission(callback.from_user.id, PERM_WRITEOFF_APPROVE):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        logger.warning(
            "[security] Попытка редактирования без прав tg:%d", callback.from_user.id
        )
        return
    doc_id = extract_callback_value(callback.data)
    if not doc_id:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return
    logger.info(
        "[writeoff-edit] Начало редактирования tg:%d, doc=%s",
        callback.from_user.id,
        doc_id,
    )
    doc = await pending.get(doc_id)
    if not doc:
        await callback.answer("⚠️ Документ не найден.", show_alert=True)
        return
    if not await pending.try_lock(doc_id):
        await callback.answer("⏳ Другой админ уже редактирует.", show_alert=True)
        return

    admin_name = callback.from_user.full_name

    # Убираем кнопки у остальных админов
    await _remove_admin_keyboards(
        callback.bot,
        doc,
        f"✏️ Редактирует {admin_name}",
        except_admin=callback.from_user.id,
    )

    # Сохраняем doc_id в FSM для этого админа
    await state.update_data(edit_doc_id=doc_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏬 Склад", callback_data="woe_field:store")],
            [InlineKeyboardButton(text="📂 Счёт", callback_data="woe_field:account")],
            [InlineKeyboardButton(text="📦 Позиции", callback_data="woe_field:items")],
            [
                InlineKeyboardButton(
                    text="❌ Отмена редактирования", callback_data="woe_cancel"
                )
            ],
        ]
    )
    await state.set_state(AdminEditStates.choose_field)
    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc) + "\n\n✏️ <b>Что редактируем?</b>",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        await callback.message.answer(
            pending.build_summary_text(doc) + "\n\n✏️ <b>Что редактируем?</b>",
            parse_mode="HTML",
            reply_markup=kb,
        )


# ── Отмена редактирования ──


@router.callback_query(F.data == "woe_cancel")
async def admin_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    logger.info(
        "[writeoff-edit] Отмена редактирования tg:%d, doc=%s",
        callback.from_user.id,
        doc_id,
    )
    await state.clear()

    if doc_id:
        await pending.unlock(doc_id)
        doc = await pending.get(doc_id)
        if doc:
            # Перерассылаем кнопки заново
            text = pending.build_summary_text(doc)
            kb = pending.admin_keyboard(doc_id)
            from use_cases import permissions as perm_uc
            from bot.permission_map import PERM_WRITEOFF_APPROVE

            _ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)
            for admin_id in _ids:
                try:
                    msg = await callback.bot.send_message(
                        admin_id, text, parse_mode="HTML", reply_markup=kb
                    )
                    doc.admin_msg_ids[admin_id] = msg.message_id
                except Exception:
                    pass
            await pending.save_admin_msg_ids(doc_id, doc.admin_msg_ids)

    try:
        await callback.message.edit_text("❌ Редактирование отменено.")
    except Exception:
        pass


# ── Выбор поля для редактирования ──


@router.callback_query(AdminEditStates.choose_field, F.data.startswith("woe_field:"))
async def admin_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    field = callback.data.split(":", 1)[1]
    logger.info(
        "[writeoff-edit] Выбор поля tg:%d, field=%s", callback.from_user.id, field
    )
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = (await pending.get(doc_id)) if doc_id else None
    if not doc:
        await state.clear()
        await callback.answer("⚠️ Документ не найден.", show_alert=True)
        return

    if field == "store":
        stores = await wo_uc.get_stores_for_department(doc.department_id)
        if not stores:
            try:
                await callback.message.edit_text(
                    "❌ Нет доступных складов.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="❌ Отмена", callback_data="woe_cancel"
                                )
                            ]
                        ]
                    ),
                )
            except Exception:
                pass
            return
        await state.update_data(_edit_stores=stores)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=s["name"], callback_data=f"woe_store:{s['id']}"
                    )
                ]
                for s in stores
            ]
            + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
        )
        await state.set_state(AdminEditStates.choose_store)
        await callback.message.edit_text("🏬 Выберите новый склад:", reply_markup=kb)

    elif field == "account":
        accounts = await wo_uc.get_writeoff_accounts(doc.store_name)
        if not accounts:
            try:
                await callback.message.edit_text(
                    "❌ Нет счетов.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="❌ Отмена", callback_data="woe_cancel"
                                )
                            ]
                        ]
                    ),
                )
            except Exception:
                pass
            return
        await state.update_data(_edit_accounts=accounts)
        kb = _accounts_kb(accounts, page=0)
        # Переиспользуем wo_acc для выбора (добавим prefix woe_acc)
        buttons = [
            [InlineKeyboardButton(text=a["name"], callback_data=f"woe_acc:{a['id']}")]
            for a in accounts
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await state.set_state(AdminEditStates.choose_account)
        await callback.message.edit_text("📂 Выберите новый счёт:", reply_markup=kb)

    elif field == "items":
        items = doc.items
        if not items:
            try:
                await callback.message.edit_text(
                    "❌ В документе нет позиций.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="❌ Отмена", callback_data="woe_cancel"
                                )
                            ]
                        ]
                    ),
                )
            except Exception:
                pass
            return
        buttons = [
            [
                InlineKeyboardButton(
                    text=f"{i}. {item['name']} — {item.get('user_quantity', item.get('quantity', 0))} {item.get('unit_label', 'шт')}",
                    callback_data=f"woe_item:{i-1}",
                )
            ]
            for i, item in enumerate(items, 1)
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await state.set_state(AdminEditStates.choose_item_idx)
        await callback.message.edit_text(
            "📦 Какую позицию редактировать?", reply_markup=kb
        )


# ── Новый склад ──


@router.callback_query(AdminEditStates.choose_store, F.data.startswith("woe_store:"))
async def admin_edit_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = await validate_callback_uuid(callback, callback.data)
    if not store_id:
        return
    logger.info(
        "[writeoff-edit] Новый склад tg:%d, store_id=%s",
        callback.from_user.id,
        store_id,
    )
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = (await pending.get(doc_id)) if doc_id else None
    if not doc:
        await state.clear()
        return

    stores = data.get("_edit_stores", [])
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        await callback.answer("❌ Склад не найден", show_alert=True)
        return

    doc.store_id = store_id
    doc.store_name = store["name"]
    await pending.update_store(doc.doc_id, store_id, store["name"])
    logger.info("[writeoff-edit] Склад изменён на %s (%s)", store["name"], store_id)

    await _finish_edit(callback, state, doc)


# ── Новый счёт ──


@router.callback_query(AdminEditStates.choose_account, F.data.startswith("woe_acc:"))
async def admin_edit_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = await validate_callback_uuid(callback, callback.data)
    if not account_id:
        return
    logger.info(
        "[writeoff-edit] Новый счёт tg:%d, acc_id=%s", callback.from_user.id, account_id
    )
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = (await pending.get(doc_id)) if doc_id else None
    if not doc:
        await state.clear()
        return

    accounts = data.get("_edit_accounts", [])
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        await callback.answer("❌ Счёт не найден", show_alert=True)
        return

    doc.account_id = account_id
    doc.account_name = account["name"]
    await pending.update_account(doc.doc_id, account_id, account["name"])
    logger.info("[writeoff-edit] Счёт изменён на %s (%s)", account["name"], account_id)

    await _finish_edit(callback, state, doc)


# ── Выбор позиции ──


@router.callback_query(AdminEditStates.choose_item_idx, F.data.startswith("woe_item:"))
async def admin_edit_item_idx(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    idx = await validate_callback_int(callback, callback.data)
    if idx is None:
        return
    logger.info(
        "[writeoff-edit] Выбор позиции tg:%d, idx=%d", callback.from_user.id, idx
    )
    data = await state.get_data()
    doc = await pending.get(data.get("edit_doc_id", ""))
    if not doc or idx >= len(doc.items):
        await callback.answer("❌ Позиция не найдена", show_alert=True)
        return

    item = doc.items[idx]
    await state.update_data(edit_item_idx=idx)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Сменить наименование", callback_data="woe_action:name"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔢 Изменить количество", callback_data="woe_action:qty"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить позицию", callback_data="woe_action:delete"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")],
        ]
    )
    uq = item.get("user_quantity", item.get("quantity", 0))
    ul = item.get("unit_label", "шт")
    await state.set_state(AdminEditStates.choose_item_action)
    await callback.message.edit_text(
        f"📦 Позиция #{idx+1}: <b>{item['name']}</b> — {uq} {ul}\n\nЧто меняем?",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Действие с позицией ──


@router.callback_query(
    AdminEditStates.choose_item_action, F.data.startswith("woe_action:")
)
async def admin_edit_item_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    action = callback.data.split(":", 1)[1]
    logger.info(
        "[writeoff-edit] Действие с позицией tg:%d, action=%s",
        callback.from_user.id,
        action,
    )
    data = await state.get_data()
    doc = await pending.get(data.get("edit_doc_id", ""))
    idx = data.get("edit_item_idx", -1)

    if not doc or idx < 0 or idx >= len(doc.items):
        await state.clear()
        return

    if action == "delete":
        removed = doc.items.pop(idx)
        await pending.update_items(doc.doc_id, doc.items)
        logger.info(
            "[writeoff-edit] Удалена позиция #%d: %s", idx + 1, removed.get("name")
        )
        await _finish_edit(callback, state, doc)
        return

    if action == "name":
        await state.set_state(AdminEditStates.new_product_search)
        await callback.message.edit_text("🔍 Введите часть названия нового товара:")
        await state.update_data(_edit_prompt_id=callback.message.message_id)
        return

    if action == "qty":
        item = doc.items[idx]
        unit_label = item.get("unit_label", "шт")
        await state.set_state(AdminEditStates.new_quantity)
        await callback.message.edit_text(
            f"🔢 Введите новое количество ({unit_label}) для «{item['name']}»:"
        )
        await state.update_data(_edit_prompt_id=callback.message.message_id)
        return


# ── Поиск нового товара (замена наименования) ──


# Защита: текст в inline-состояниях редактирования
@router.message(AdminEditStates.choose_field)
@router.message(AdminEditStates.choose_store)
@router.message(AdminEditStates.choose_account)
@router.message(AdminEditStates.choose_item_idx)
@router.message(AdminEditStates.choose_item_action)
async def _ignore_text_admin_edit(message: Message) -> None:
    logger.debug(
        "[writeoff-edit] Игнор текста в inline-состоянии tg:%d", message.from_user.id
    )
    try:
        await message.delete()
    except Exception:
        pass


@router.message(AdminEditStates.new_product_search)
async def admin_search_new_product(message: Message, state: FSMContext) -> None:
    query = truncate_input((message.text or "").strip(), MAX_TEXT_SEARCH)
    logger.info(
        "[writeoff-edit] Поиск нового товара tg:%d, query='%s'",
        message.from_user.id,
        query,
    )
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    edit_prompt_id = data.get("_edit_prompt_id")

    if len(query) < 2:
        if edit_prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Минимум 2 символа. Введите название товара:",
                    chat_id=message.chat.id,
                    message_id=edit_prompt_id,
                )
            except Exception:
                pass
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await wo_uc.search_products(
        query, department_id=data.get("department_id")
    )
    if not products:
        if edit_prompt_id:
            try:
                await message.bot.edit_message_text(
                    "🔎 Ничего. Попробуйте другой запрос:",
                    chat_id=message.chat.id,
                    message_id=edit_prompt_id,
                )
            except Exception:
                pass
        return

    cache = {p["id"]: p for p in products}
    await state.update_data(_edit_product_cache=cache)

    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"woe_newprod:{p['id']}")]
        for p in products
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit_prompt_id:
        try:
            await message.bot.edit_message_text(
                "Выберите новый товар:",
                chat_id=message.chat.id,
                message_id=edit_prompt_id,
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    msg = await message.answer("Выберите новый товар:", reply_markup=kb)
    await state.update_data(_edit_prompt_id=msg.message_id)


@router.callback_query(
    AdminEditStates.new_product_search, F.data.startswith("woe_newprod:")
)
async def admin_pick_new_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pid = await validate_callback_uuid(callback, callback.data)
    if not pid:
        return
    logger.info(
        "[writeoff-edit] Выбран новый товар tg:%d, prod_id=%s",
        callback.from_user.id,
        pid,
    )
    data = await state.get_data()
    doc = await pending.get(data.get("edit_doc_id", ""))
    idx = data.get("edit_item_idx", -1)
    cache = data.get("_edit_product_cache", {})
    product = cache.get(pid)

    if not doc or idx < 0 or idx >= len(doc.items) or not product:
        await state.clear()
        return

    old_name = doc.items[idx]["name"]
    # Сохраняем новый товар, но сохраняем количество
    old_qty = doc.items[idx].get("quantity", 0)
    old_uq = doc.items[idx].get("user_quantity", 0)
    old_ul = doc.items[idx].get("unit_label", "шт")

    doc.items[idx] = {
        "id": product["id"],
        "name": product["name"],
        "main_unit": product.get("main_unit"),
        "product_type": product.get("product_type"),
        "quantity": old_qty,
        "user_quantity": old_uq,
        "unit_label": old_ul,
    }
    await pending.update_items(doc.doc_id, doc.items)
    logger.info(
        "[writeoff-edit] Позиция #%d: %s → %s", idx + 1, old_name, product["name"]
    )
    await _finish_edit(callback, state, doc)


# ── Ввод нового количества ──


@router.message(AdminEditStates.new_quantity)
async def admin_set_new_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", ".").strip()
    logger.info(
        "[writeoff-edit] Новое количество tg:%d, raw='%s'", message.from_user.id, raw
    )
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    edit_prompt_id = data.get("_edit_prompt_id")

    try:
        qty = float(raw)
    except ValueError:
        if edit_prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Введите число.",
                    chat_id=message.chat.id,
                    message_id=edit_prompt_id,
                )
            except Exception:
                pass
        return
    if qty < QTY_MIN or qty > QTY_MAX:
        if edit_prompt_id:
            try:
                await message.bot.edit_message_text(
                    f"⚠️ Допустимо: {QTY_MIN}–{QTY_MAX}.",
                    chat_id=message.chat.id,
                    message_id=edit_prompt_id,
                )
            except Exception:
                pass
        return
    doc = await pending.get(data.get("edit_doc_id", ""))
    idx = data.get("edit_item_idx", -1)
    if not doc or idx < 0 or idx >= len(doc.items):
        await state.clear()
        return

    item = doc.items[idx]
    unit_name = await wo_uc.get_unit_name(item.get("main_unit"))
    norm = wo_uc.normalize_unit(unit_name)
    converted = qty / 1000 if norm in ("kg", "l") else qty

    item["quantity"] = converted
    item["user_quantity"] = qty
    await pending.update_items(doc.doc_id, doc.items)
    logger.info("[writeoff-edit] Позиция #%d кол-во: %s → %s", idx + 1, qty, converted)

    await _finish_edit_msg(message, state, doc)


# ── Завершение редактирования → назад к кнопкам ──


async def _finish_edit(
    callback: CallbackQuery, state: FSMContext, doc: pending.PendingWriteoff
) -> None:
    """Завершить редактирование: разблокировать, обновить сообщения у всех админов."""
    doc_id = doc.doc_id
    logger.info(
        "[writeoff-edit] Завершение редактирования tg:%d, doc=%s",
        callback.from_user.id,
        doc_id,
    )
    await state.clear()
    await pending.unlock(doc_id)

    text = pending.build_summary_text(doc)
    kb = pending.admin_keyboard(doc_id)

    # Получаем список всех админов
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    admin_ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)
    existing_msgs = doc.admin_msg_ids.copy()
    new_msg_ids: dict[int, int] = {}

    for admin_id in admin_ids:
        msg_id = existing_msgs.get(admin_id)

        # Если есть существующее сообщение, пробуем отредактировать
        if msg_id:
            try:
                await callback.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                new_msg_ids[admin_id] = msg_id
                logger.debug(
                    "[writeoff-edit] Отредактировано сообщение #%d для админа tg:%d в документе %s",
                    msg_id,
                    admin_id,
                    doc_id,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "[writeoff-edit] Не удалось отредактировать сообщение #%d для tg:%d: %s. Отправляю новое.",
                    msg_id,
                    admin_id,
                    exc,
                )

        # Если нет существующего сообщения или редактирование не удалось, отправляем новое
        try:
            msg = await callback.bot.send_message(
                admin_id, text, parse_mode="HTML", reply_markup=kb
            )
            new_msg_ids[admin_id] = msg.message_id
            logger.debug(
                "[writeoff-edit] Отправлено новое сообщение #%d админу tg:%d для документа %s",
                msg.message_id,
                admin_id,
                doc_id,
            )
        except Exception as exc:
            logger.warning(
                "[writeoff-edit] Не удалось отправить сообщение админу tg:%d: %s",
                admin_id,
                exc,
            )

    doc.admin_msg_ids = new_msg_ids
    await pending.save_admin_msg_ids(doc_id, new_msg_ids)


async def _finish_edit_msg(
    message: Message, state: FSMContext, doc: pending.PendingWriteoff
) -> None:
    """То же, но из message-хэндлера (не callback)."""
    doc_id = doc.doc_id
    logger.info(
        "[writeoff-edit] Завершение редактирования (msg) tg:%d, doc=%s",
        message.from_user.id,
        doc_id,
    )
    await state.clear()
    await pending.unlock(doc_id)

    text = pending.build_summary_text(doc)
    kb = pending.admin_keyboard(doc_id)

    # Получаем список всех админов
    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    admin_ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)
    existing_msgs = doc.admin_msg_ids.copy()
    new_msg_ids: dict[int, int] = {}

    for admin_id in admin_ids:
        msg_id = existing_msgs.get(admin_id)

        # Если есть существующее сообщение, пробуем отредактировать
        if msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                new_msg_ids[admin_id] = msg_id
                logger.debug(
                    "[writeoff-edit] Отредактировано сообщение #%d для админа tg:%d в документе %s",
                    msg_id,
                    admin_id,
                    doc_id,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "[writeoff-edit] Не удалось отредактировать сообщение #%d для tg:%d: %s. Отправляю новое.",
                    msg_id,
                    admin_id,
                    exc,
                )

        # Если нет существующего сообщения или редактирование не удалось, отправляем новое
        try:
            msg = await message.bot.send_message(
                admin_id, text, parse_mode="HTML", reply_markup=kb
            )
            new_msg_ids[admin_id] = msg.message_id
            logger.debug(
                "[writeoff-edit] Отправлено новое сообщение #%d админу tg:%d для документа %s",
                msg.message_id,
                admin_id,
                doc_id,
            )
        except Exception as exc:
            logger.warning(
                "[writeoff-edit] Не удалось отправить сообщение админу tg:%d: %s",
                admin_id,
                exc,
            )

    doc.admin_msg_ids = new_msg_ids
    await pending.save_admin_msg_ids(doc_id, new_msg_ids)


# ══════════════════════════════════════════════════════
#  ИСТОРИЯ СПИСАНИЙ
# ══════════════════════════════════════════════════════


# Защита: текст в inline-состояниях истории
@router.message(HistoryStates.browsing)
@router.message(HistoryStates.viewing)
async def _ignore_text_history_inline(message: Message) -> None:
    logger.debug(
        "[writeoff-hist] Игнор текста в inline-состоянии tg:%d", message.from_user.id
    )
    try:
        await message.delete()
    except Exception:
        pass


HIST_PAGE_SIZE = wo_hist.HISTORY_PAGE_SIZE


def _history_list_kb(
    entries: list[wo_hist.HistoryEntry], page: int, total: int
) -> InlineKeyboardMarkup:
    """Клавиатура со списком истории + пагинация."""
    buttons = []
    for entry in entries:
        # Краткая строка: дата + склад + кол-во позиций
        n_items = len(entry.items) if entry.items else 0
        label = f"{entry.created_at} | {entry.store_name} ({n_items} поз.)"
        # Обрезаем до 60 символов (ограничение Telegram callback_data label)
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"woh_view:{entry.pk}",
                )
            ]
        )

    # Пагинация
    total_pages = max(1, (total + HIST_PAGE_SIZE - 1) // HIST_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"woh_page:{page - 1}")
        )
    if (page + 1) * HIST_PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton(text="▶️ Далее", callback_data=f"woh_page:{page + 1}")
        )
    if nav:
        nav.insert(
            len(nav) // 2,
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="woh_noop",
            ),
        )
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="woh_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _history_detail_kb(pk: int) -> InlineKeyboardMarkup:
    """Клавиатура при просмотре одной записи из истории."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Повторить как есть", callback_data=f"woh_reuse:{pk}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать и отправить", callback_data=f"woh_edit:{pk}"
                )
            ],
            [InlineKeyboardButton(text="◀️ К списку", callback_data="woh_back")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="woh_close")],
        ]
    )


def _hist_edit_kb() -> InlineKeyboardMarkup:
    """Клавиатура при редактировании копии из истории."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Изменить причину", callback_data="wohe_reason"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Редактировать позиции", callback_data="wohe_items"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить товар", callback_data="wohe_add_item"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Отправить на проверку", callback_data="wohe_send"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="woh_close")],
        ]
    )


def _hist_items_kb(items: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура выбора позиции для редактирования."""
    buttons = []
    for i, item in enumerate(items):
        uq = item.get("user_quantity", item.get("quantity", 0))
        ul = item.get("unit_label", "шт")
        label = f"{i+1}. {item.get('name', '?')} — {uq} {ul}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f"wohe_item:{i}")]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад к редактированию", callback_data="wohe_back"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _hist_item_action_kb(idx: int) -> InlineKeyboardMarkup:
    """Действия с позицией при редактировании из истории."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔢 Изменить количество", callback_data=f"wohe_qty:{idx}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить позицию", callback_data=f"wohe_del:{idx}"
                )
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="wohe_back")],
        ]
    )


# ── 1. Кнопка «🗂 История списаний» ──


@router.message(F.text == "🗂 История списаний")
async def start_history(message: Message, state: FSMContext) -> None:
    """Открыть историю списаний с фильтрацией по роли."""
    logger.info("[writeoff_handlers] start_history tg:%d", message.from_user.id)
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
        "[wo_history] Открытие истории tg:%d, dept=%s, role=%s",
        message.from_user.id,
        ctx.department_id,
        ctx.role_name,
    )

    from use_cases import permissions as perm_uc
    from bot.permission_map import PERM_WRITEOFF_APPROVE

    is_bot_admin = await perm_uc.has_permission(
        message.from_user.id, PERM_WRITEOFF_APPROVE
    )
    role_type = "admin" if is_bot_admin else wo_uc.classify_role(ctx.role_name)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    entries, total = await wo_hist.get_history(
        telegram_id=message.from_user.id,
        department_id=ctx.department_id,
        role_type=role_type,
        page=0,
    )

    if not entries:
        await message.answer("📋 История списаний пуста.")
        return

    role_label = {"bar": "🍸 Бар", "kitchen": "🍳 Кухня", "admin": "👑 Все"}.get(
        role_type, "📋 Все"
    )
    msg = await message.answer(
        f"📋 <b>История списаний</b> ({role_label})\n"
        f"Всего записей: {total}\n\n"
        "Выберите запись для просмотра:",
        parse_mode="HTML",
        reply_markup=_history_list_kb(entries, page=0, total=total),
    )
    await state.update_data(
        hist_page=0,
        hist_role_type=role_type,
        hist_department_id=ctx.department_id,
        hist_msg_id=msg.message_id,
    )
    await state.set_state(HistoryStates.browsing)


# ── 2. Пагинация ──


@router.callback_query(HistoryStates.browsing, F.data == "woh_noop")
async def hist_noop(callback: CallbackQuery) -> None:
    logger.info("[writeoff_handlers] hist_noop tg:%d", callback.from_user.id)
    await callback.answer()


@router.callback_query(HistoryStates.browsing, F.data.startswith("woh_page:"))
async def hist_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = safe_page(callback.data)
    logger.debug("[wo_history] Пагинация tg:%d, page=%d", callback.from_user.id, page)
    data = await state.get_data()
    role_type = data.get("hist_role_type", "unknown")
    department_id = data.get("hist_department_id", "")

    entries, total = await wo_hist.get_history(
        telegram_id=callback.from_user.id,
        department_id=department_id,
        role_type=role_type,
        page=page,
    )

    role_label = {"bar": "🍸 Бар", "kitchen": "🍳 Кухня", "admin": "👑 Все"}.get(
        role_type, "📋 Все"
    )
    try:
        await callback.message.edit_text(
            f"📋 <b>История списаний</b> ({role_label})\n"
            f"Всего записей: {total}\n\n"
            "Выберите запись:",
            parse_mode="HTML",
            reply_markup=_history_list_kb(entries, page=page, total=total),
        )
    except Exception:
        pass
    await state.update_data(hist_page=page)


# ── 3. Просмотр одной записи ──


@router.callback_query(HistoryStates.browsing, F.data.startswith("woh_view:"))
async def hist_view(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        pk = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.info("[wo_history] Просмотр записи tg:%d, pk=%d", callback.from_user.id, pk)
    entry = await wo_hist.get_history_entry(pk)
    if not entry:
        await callback.answer("⚠️ Запись не найдена.", show_alert=True)
        return

    text = wo_hist.build_history_summary(entry)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_history_detail_kb(pk),
        )
    except Exception:
        pass
    await state.update_data(hist_viewing_pk=pk)
    await state.set_state(HistoryStates.viewing)


# ── 4. Назад к списку ──


@router.callback_query(F.data == "woh_back")
async def hist_back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_back_to_list tg:%d", callback.from_user.id)
    await callback.answer()
    data = await state.get_data()
    page = data.get("hist_page", 0)
    role_type = data.get("hist_role_type", "unknown")
    department_id = data.get("hist_department_id", "")

    entries, total = await wo_hist.get_history(
        telegram_id=callback.from_user.id,
        department_id=department_id,
        role_type=role_type,
        page=page,
    )

    role_label = {"bar": "🍸 Бар", "kitchen": "🍳 Кухня", "admin": "👑 Все"}.get(
        role_type, "📋 Все"
    )
    try:
        await callback.message.edit_text(
            f"📋 <b>История списаний</b> ({role_label})\n"
            f"Всего записей: {total}\n\n"
            "Выберите запись:",
            parse_mode="HTML",
            reply_markup=_history_list_kb(entries, page=page, total=total),
        )
    except Exception:
        pass
    await state.set_state(HistoryStates.browsing)


# ── 5. Закрыть историю ──


@router.callback_query(F.data == "woh_close")
async def hist_close(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_close tg:%d", callback.from_user.id)
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_text("📋 История закрыта.")
    except Exception:
        pass
    await restore_menu_kb(
        callback.bot,
        callback.message.chat.id,
        state,
        "📝 Списания:",
        writeoffs_keyboard(),
    )


# ── 6. Повторить как есть (отправить копию на проверку) ──


@router.callback_query(HistoryStates.viewing, F.data.startswith("woh_reuse:"))
async def hist_reuse(callback: CallbackQuery, state: FSMContext) -> None:
    """Повторить списание из истории как есть → на проверку админам."""
    logger.info("[writeoff_handlers] hist_reuse tg:%d", callback.from_user.id)
    user_id = callback.from_user.id
    if user_id in _sending_lock:
        await callback.answer("⏳ Уже отправляется…")
        return

    await callback.answer()
    try:
        pk = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.info("[wo_history] Повтор из истории tg:%d, pk=%d", user_id, pk)
    entry = await wo_hist.get_history_entry(pk)
    if not entry:
        await callback.answer("⚠️ Запись не найдена.", show_alert=True)
        return

    ctx = await uctx.get_user_context(user_id)
    if not ctx:
        await callback.answer("⚠️ Авторизуйтесь заново (/start).", show_alert=True)
        return

    _sending_lock.add(user_id)
    try:
        from use_cases import permissions as perm_uc
        from bot.permission_map import PERM_WRITEOFF_APPROVE

        admin_ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)

        if not admin_ids:
            # Нет админов — отправляем напрямую
            try:
                await callback.message.edit_text("⏳ Отправляем акт напрямую...")
            except Exception:
                logger.debug("[wo_history] edit_text fail (reuse no-admin placeholder)")
            await state.clear()

            result = await wo_uc.finalize_without_admins(
                store_id=entry.store_id,
                account_id=entry.account_id,
                reason=entry.reason,
                items=entry.items,
                author_name=ctx.employee_name,
            )
            # Сохраняем в историю повторно при успехе
            if result.startswith("✅") and ctx.department_id:
                try:
                    await wo_hist.save_to_history(
                        telegram_id=user_id,
                        employee_name=ctx.employee_name,
                        department_id=ctx.department_id,
                        store_id=entry.store_id,
                        store_name=entry.store_name,
                        account_id=entry.account_id,
                        account_name=entry.account_name,
                        reason=entry.reason,
                        items=entry.items,
                    )
                except Exception:
                    logger.warning("[wo_history] Ошибка сохранения повтора в историю")
            try:
                await callback.message.edit_text(result)
            except Exception:
                await callback.bot.send_message(callback.message.chat.id, result)
            await restore_menu_kb(
                callback.bot,
                callback.message.chat.id,
                state,
                "📝 Списания:",
                writeoffs_keyboard(),
            )
            return

        # Создаём pending-документ
        doc = await pending.create(
            author_chat_id=callback.message.chat.id,
            author_name=ctx.employee_name,
            store_id=entry.store_id,
            store_name=entry.store_name,
            account_id=entry.account_id,
            account_name=entry.account_name,
            reason=entry.reason,
            department_id=ctx.department_id,
            items=list(entry.items),
        )

        try:
            await callback.message.edit_text(
                "✅ Акт из истории отправлен на проверку администраторам. Ожидайте."
            )
        except Exception:
            pass
        await state.clear()

        # Рассылаем всем админам
        text = pending.build_summary_text(doc)
        kb = pending.admin_keyboard(doc.doc_id)
        for admin_id in admin_ids:
            try:
                msg = await callback.bot.send_message(
                    admin_id, text, parse_mode="HTML", reply_markup=kb
                )
                doc.admin_msg_ids[admin_id] = msg.message_id
            except Exception as exc:
                logger.warning(
                    "[wo_history] Не удалось отправить админу %d: %s", admin_id, exc
                )

        await pending.save_admin_msg_ids(doc.doc_id, doc.admin_msg_ids)
        logger.info(
            "[wo_history] Повтор из истории pk=%d → doc %s, %d админов",
            pk,
            doc.doc_id,
            len(doc.admin_msg_ids),
        )
        await restore_menu_kb(
            callback.bot,
            callback.message.chat.id,
            state,
            "📝 Списания:",
            writeoffs_keyboard(),
        )
    finally:
        _sending_lock.discard(user_id)


# ── 7. Редактировать и отправить ──


@router.callback_query(HistoryStates.viewing, F.data.startswith("woh_edit:"))
async def hist_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Загрузить запись из истории для редактирования перед отправкой."""
    await callback.answer()
    try:
        pk = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.info(
        "[wo_history] Редактирование из истории tg:%d, pk=%d", callback.from_user.id, pk
    )
    entry = await wo_hist.get_history_entry(pk)
    if not entry:
        await callback.answer("⚠️ Запись не найдена.", show_alert=True)
        return

    # Сохраняем копию в FSM для редактирования
    await state.update_data(
        hist_edit_pk=pk,
        hist_edit_store_id=entry.store_id,
        hist_edit_store_name=entry.store_name,
        hist_edit_account_id=entry.account_id,
        hist_edit_account_name=entry.account_name,
        hist_edit_reason=entry.reason,
        hist_edit_items=list(entry.items),
    )

    text = wo_hist.build_history_summary(entry)
    text += "\n\n✏️ <b>Что хотите изменить?</b>"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_hist_edit_kb()
        )
    except Exception:
        pass
    await state.set_state(HistoryStates.viewing)


# ── 7a. Изменить причину ──


@router.callback_query(HistoryStates.viewing, F.data == "wohe_reason")
async def hist_edit_reason_start(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_reason_start tg:%d", callback.from_user.id)
    await callback.answer()
    try:
        await callback.message.edit_text("📝 Введите новую причину списания:")
    except Exception:
        pass
    await state.update_data(hist_edit_prompt_id=callback.message.message_id)
    await state.set_state(HistoryStates.editing_reason)


@router.message(HistoryStates.editing_reason)
async def hist_edit_reason_input(message: Message, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_reason_input tg:%d", message.from_user.id)
    reason = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not reason:
        return
    if len(reason) > 500:
        data = await state.get_data()
        prompt_id = data.get("hist_edit_prompt_id")
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Макс. 500 символов. Введите причину покороче:",
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                )
            except Exception:
                pass
        return

    await state.update_data(hist_edit_reason=reason)
    logger.info("[wo_history] Новая причина tg:%d: %s", message.from_user.id, reason)

    # Показываем обновлённый summary
    data = await state.get_data()
    text = _build_hist_edit_summary(data)
    text += "\n\n✏️ <b>Что ещё изменить?</b>"
    prompt_id = data.get("hist_edit_prompt_id")
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=prompt_id,
                parse_mode="HTML",
                reply_markup=_hist_edit_kb(),
            )
        except Exception:
            msg = await message.answer(
                text, parse_mode="HTML", reply_markup=_hist_edit_kb()
            )
            await state.update_data(hist_edit_prompt_id=msg.message_id)
    else:
        msg = await message.answer(
            text, parse_mode="HTML", reply_markup=_hist_edit_kb()
        )
        await state.update_data(hist_edit_prompt_id=msg.message_id)
    await state.set_state(HistoryStates.viewing)


# ── 7b. Редактировать позиции ──


@router.callback_query(HistoryStates.viewing, F.data == "wohe_items")
async def hist_edit_items_list(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_items_list tg:%d", callback.from_user.id)
    await callback.answer()
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if not items:
        await callback.answer("Нет позиций для редактирования.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            "📦 Выберите позицию для редактирования:",
            reply_markup=_hist_items_kb(items),
        )
    except Exception:
        pass


# ── 7b-1. Выбор позиции ──


@router.callback_query(HistoryStates.viewing, F.data.startswith("wohe_item:"))
async def hist_edit_item_select(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        idx = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.debug("[wo_history] Выбор позиции tg:%d, idx=%d", callback.from_user.id, idx)
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if idx >= len(items):
        await callback.answer("❌ Позиция не найдена.", show_alert=True)
        return

    item = items[idx]
    uq = item.get("user_quantity", item.get("quantity", 0))
    ul = item.get("unit_label", "шт")
    try:
        await callback.message.edit_text(
            f"📦 Позиция #{idx+1}: <b>{item.get('name', '?')}</b> — {uq} {ul}\n\nЧто сделать?",
            parse_mode="HTML",
            reply_markup=_hist_item_action_kb(idx),
        )
    except Exception:
        pass
    await state.update_data(hist_edit_item_idx=idx)


# ── 7b-2. Изменить количество ──


@router.callback_query(HistoryStates.viewing, F.data.startswith("wohe_qty:"))
async def hist_edit_qty_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        idx = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.debug(
        "[wo_history] Редакт. кол-во tg:%d, idx=%d", callback.from_user.id, idx
    )
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if idx >= len(items):
        return
    item = items[idx]
    ul = item.get("unit_label", "шт")
    try:
        await callback.message.edit_text(
            f"🔢 Введите новое количество ({ul}) для «{item.get('name', '?')}»:"
        )
    except Exception:
        pass
    await state.update_data(
        hist_edit_item_idx=idx, hist_edit_prompt_id=callback.message.message_id
    )
    await state.set_state(HistoryStates.editing_quantity)


@router.message(HistoryStates.editing_quantity)
async def hist_edit_qty_input(message: Message, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_qty_input tg:%d", message.from_user.id)
    raw = (message.text or "").replace(",", ".").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    prompt_id = data.get("hist_edit_prompt_id")

    try:
        qty = float(raw)
    except ValueError:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Введите число.", chat_id=message.chat.id, message_id=prompt_id
                )
            except Exception:
                pass
        return
    if qty < QTY_MIN or qty > QTY_MAX:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    f"⚠️ Допустимо: {QTY_MIN}–{QTY_MAX}.",
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                )
            except Exception:
                pass
        return

    idx = data.get("hist_edit_item_idx", -1)
    items = data.get("hist_edit_items", [])

    if idx == -1:
        # Добавление нового товара
        product = data.get("hist_edit_new_product")
        if not product:
            await state.set_state(HistoryStates.viewing)
            return

        unit_name = data.get("hist_edit_new_unit_name", "шт")
        norm = data.get("hist_edit_new_unit_norm", "pcs")
        unit_label = data.get("hist_edit_new_unit_label", "шт")
        converted = qty / 1000 if norm in ("kg", "l") else qty

        new_item = {
            "id": product["id"],
            "name": product["name"],
            "main_unit": product.get("main_unit"),
            "quantity": converted,
            "user_quantity": qty,
            "unit_label": unit_label,
        }
        items.append(new_item)
        await state.update_data(hist_edit_items=items)
        logger.info(
            "[wo_history] Добавлен товар: %s — %s %s", product["name"], qty, unit_label
        )
    elif idx < len(items):
        item = items[idx]
        unit_name = item.get("unit_label", "шт")
        norm = wo_uc.normalize_unit(unit_name)
        converted = qty / 1000 if norm in ("kg", "l") else qty

        item["quantity"] = converted
        item["user_quantity"] = qty
        items[idx] = item
        await state.update_data(hist_edit_items=items)
        logger.info("[wo_history] Позиция #%d кол-во: %s → %s", idx + 1, qty, converted)
    else:
        await state.set_state(HistoryStates.viewing)
        return

    # Вернуться к меню редактирования
    text = _build_hist_edit_summary(data | {"hist_edit_items": items})
    text += "\n\n✏️ <b>Что ещё изменить?</b>"
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=prompt_id,
                parse_mode="HTML",
                reply_markup=_hist_edit_kb(),
            )
        except Exception:
            msg = await message.answer(
                text, parse_mode="HTML", reply_markup=_hist_edit_kb()
            )
            await state.update_data(hist_edit_prompt_id=msg.message_id)
    else:
        msg = await message.answer(
            text, parse_mode="HTML", reply_markup=_hist_edit_kb()
        )
        await state.update_data(hist_edit_prompt_id=msg.message_id)
    await state.set_state(HistoryStates.viewing)


# ── 7b-3. Удалить позицию ──


@router.callback_query(HistoryStates.viewing, F.data.startswith("wohe_del:"))
async def hist_edit_item_delete(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        idx = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    logger.debug(
        "[wo_history] Удаление позиции tg:%d, idx=%d", callback.from_user.id, idx
    )
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if idx < len(items):
        removed = items.pop(idx)
        logger.info(
            "[wo_history] Удалена позиция #%d: %s", idx + 1, removed.get("name")
        )
        await state.update_data(hist_edit_items=items)

    text = _build_hist_edit_summary(data | {"hist_edit_items": items})
    text += "\n\n✏️ <b>Что ещё изменить?</b>"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_hist_edit_kb()
        )
    except Exception:
        pass


# ── 7c. Добавить товар ──


@router.callback_query(HistoryStates.viewing, F.data == "wohe_add_item")
async def hist_edit_add_item_start(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_add_item_start tg:%d", callback.from_user.id)
    await callback.answer()
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if len(items) >= MAX_ITEMS:
        await callback.answer(f"⚠️ Макс. {MAX_ITEMS} позиций.", show_alert=True)
        return
    try:
        await callback.message.edit_text("🔍 Введите часть названия товара:")
    except Exception:
        pass
    await state.update_data(hist_edit_prompt_id=callback.message.message_id)
    await state.set_state(HistoryStates.editing_items)


@router.message(HistoryStates.editing_items)
async def hist_edit_add_item_search(message: Message, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_add_item_search tg:%d", message.from_user.id)
    query = truncate_input((message.text or "").strip(), MAX_TEXT_SEARCH)
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    prompt_id = data.get("hist_edit_prompt_id")

    if len(query) < 2:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    "⚠️ Минимум 2 символа. Введите название товара:",
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                )
            except Exception:
                pass
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    products = await wo_uc.search_products(query)
    if not products:
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    "🔎 Ничего не найдено. Попробуйте другой запрос:",
                    chat_id=message.chat.id,
                    message_id=prompt_id,
                )
            except Exception:
                pass
        return

    cache = {p["id"]: p for p in products}
    await state.update_data(hist_edit_product_cache=cache)

    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"wohe_pick:{p['id']}")]
        for p in products
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="wohe_back")]]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                f"Найдено {len(products)}. Выберите товар:",
                chat_id=message.chat.id,
                message_id=prompt_id,
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    msg = await message.answer(
        f"Найдено {len(products)}. Выберите товар:", reply_markup=kb
    )
    await state.update_data(hist_edit_prompt_id=msg.message_id)


@router.callback_query(HistoryStates.editing_items, F.data.startswith("wohe_pick:"))
async def hist_edit_add_item_pick(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_add_item_pick tg:%d", callback.from_user.id)
    await callback.answer()
    pid = await validate_callback_uuid(callback, callback.data)
    if not pid:
        return
    data = await state.get_data()
    cache = data.get("hist_edit_product_cache", {})
    product = cache.get(pid)
    if not product:
        await callback.answer("❌ Товар не найден.", show_alert=True)
        return

    unit_name = product.get("unit_name") or await wo_uc.get_unit_name(
        product.get("main_unit")
    )
    norm = product.get("unit_norm") or wo_uc.normalize_unit(unit_name)

    if norm == "kg":
        prompt = f"📏 Сколько <b>грамм</b> для «{product['name']}»?"
        unit_label = "г"
    elif norm == "l":
        prompt = f"📏 Сколько <b>мл</b> для «{product['name']}»?"
        unit_label = "мл"
    else:
        prompt = f"📏 Сколько <b>{unit_name}</b> для «{product['name']}»?"
        unit_label = unit_name

    await state.update_data(
        hist_edit_new_product=product,
        hist_edit_new_unit_name=unit_name,
        hist_edit_new_unit_norm=norm,
        hist_edit_new_unit_label=unit_label,
    )
    try:
        await callback.message.edit_text(prompt, parse_mode="HTML")
    except Exception:
        pass
    await state.update_data(hist_edit_prompt_id=callback.message.message_id)
    await state.set_state(HistoryStates.editing_quantity)
    # Помечаем что добавляем новую позицию (не изменяем существующую)
    await state.update_data(hist_edit_item_idx=-1)


# ── Обработка ввода количества при добавлении нового товара ──
# (используется тот же HistoryStates.editing_quantity handler выше,
#  но если hist_edit_item_idx == -1, значит добавляем новый)

# Переопределяем handler для editing_quantity чтобы обработать оба случая:
# Уже зарегистрирован выше, но он проверяет idx.
# Нужно модифицировать — idx == -1 → добавление нового.

# ── 7d. Назад к меню редактирования ──


@router.callback_query(F.data == "wohe_back")
async def hist_edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[writeoff_handlers] hist_edit_back tg:%d", callback.from_user.id)
    await callback.answer()
    data = await state.get_data()
    text = _build_hist_edit_summary(data)
    text += "\n\n✏️ <b>Что ещё изменить?</b>"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_hist_edit_kb()
        )
    except Exception:
        pass
    await state.set_state(HistoryStates.viewing)


# ── 8. Отправить отредактированную копию на проверку ──


@router.callback_query(HistoryStates.viewing, F.data == "wohe_send")
async def hist_edit_send(callback: CallbackQuery, state: FSMContext) -> None:
    """Отправить отредактированную копию из истории на проверку."""
    logger.info("[writeoff_handlers] hist_edit_send tg:%d", callback.from_user.id)
    user_id = callback.from_user.id
    if user_id in _sending_lock:
        await callback.answer("⏳ Уже отправляется…")
        return

    await callback.answer()
    data = await state.get_data()
    items = data.get("hist_edit_items", [])
    if not items:
        await callback.answer("⚠️ Добавьте хотя бы одну позицию.", show_alert=True)
        return
    non_zero = [i for i in items if i.get("quantity", 0) > 0]
    if not non_zero:
        await callback.answer("⚠️ Все позиции с количеством 0.", show_alert=True)
        return

    ctx = await uctx.get_user_context(user_id)
    if not ctx:
        await callback.answer("⚠️ Авторизуйтесь заново (/start).", show_alert=True)
        return

    _sending_lock.add(user_id)
    try:
        store_id = data.get("hist_edit_store_id", "")
        store_name = data.get("hist_edit_store_name", "—")
        account_id = data.get("hist_edit_account_id", "")
        account_name = data.get("hist_edit_account_name", "—")
        reason = data.get("hist_edit_reason", "")

        from use_cases import permissions as perm_uc
        from bot.permission_map import PERM_WRITEOFF_APPROVE

        admin_ids = await perm_uc.get_users_with_permission(PERM_WRITEOFF_APPROVE)

        if not admin_ids:
            try:
                await callback.message.edit_text("⏳ Отправляем акт напрямую...")
            except Exception:
                pass
            await state.clear()

            result = await wo_uc.finalize_without_admins(
                store_id=store_id,
                account_id=account_id,
                reason=reason,
                items=items,
                author_name=ctx.employee_name,
            )
            # Сохраняем в историю при успехе
            if result.startswith("✅") and ctx.department_id:
                try:
                    await wo_hist.save_to_history(
                        telegram_id=user_id,
                        employee_name=ctx.employee_name,
                        department_id=ctx.department_id,
                        store_id=store_id,
                        store_name=store_name,
                        account_id=account_id,
                        account_name=account_name,
                        reason=reason,
                        items=items,
                    )
                except Exception:
                    logger.warning(
                        "[wo_history] Ошибка сохранения в историю (edit no-admin)"
                    )
            try:
                await callback.message.edit_text(result)
            except Exception:
                await callback.bot.send_message(callback.message.chat.id, result)
            await restore_menu_kb(
                callback.bot,
                callback.message.chat.id,
                state,
                "📝 Списания:",
                writeoffs_keyboard(),
            )
            return

        doc = await pending.create(
            author_chat_id=callback.message.chat.id,
            author_name=ctx.employee_name,
            store_id=store_id,
            store_name=store_name,
            account_id=account_id,
            account_name=account_name,
            reason=reason,
            department_id=ctx.department_id,
            items=items,
        )

        try:
            await callback.message.edit_text(
                "✅ Отредактированный акт отправлен на проверку. Ожидайте."
            )
        except Exception:
            pass
        await state.clear()

        text = pending.build_summary_text(doc)
        kb = pending.admin_keyboard(doc.doc_id)
        for admin_id in admin_ids:
            try:
                msg = await callback.bot.send_message(
                    admin_id, text, parse_mode="HTML", reply_markup=kb
                )
                doc.admin_msg_ids[admin_id] = msg.message_id
            except Exception as exc:
                logger.warning(
                    "[wo_history] Не удалось отправить админу %d: %s", admin_id, exc
                )

        await pending.save_admin_msg_ids(doc.doc_id, doc.admin_msg_ids)
        logger.info(
            "[wo_history] Отредакт. повтор → doc %s, %d админов",
            doc.doc_id,
            len(doc.admin_msg_ids),
        )
        await restore_menu_kb(
            callback.bot,
            callback.message.chat.id,
            state,
            "📝 Списания:",
            writeoffs_keyboard(),
        )
    finally:
        _sending_lock.discard(user_id)


# ── Хелпер: summary для редактирования копии из истории ──


def _build_hist_edit_summary(data: dict) -> str:
    """Текст summary при редактировании копии из истории."""
    store = data.get("hist_edit_store_name", "—")
    account = data.get("hist_edit_account_name", "—")
    reason = data.get("hist_edit_reason") or "—"
    items = data.get("hist_edit_items", [])

    text = (
        f"📋 <b>Копия из истории (редактирование)</b>\n"
        f"🏬 <b>Склад:</b> {store}\n"
        f"📂 <b>Счёт:</b> {account}\n"
        f"📝 <b>Причина:</b> {reason}\n"
    )
    if items:
        text += "\n<b>Позиции:</b>"
        for i, item in enumerate(items, 1):
            uq = item.get("user_quantity", item.get("quantity", 0))
            unit_label = item.get("unit_label", "шт")
            text += f"\n  {i}. {item.get('name', '?')} — {uq} {unit_label}"
    return text


# ══════════════════════════════════════════════════════
#  Отмена создания
# ══════════════════════════════════════════════════════


@router.callback_query(F.data == "wo_cancel")
async def cancel_writeoff(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[writeoff] Отменено user %d", callback.from_user.id)

    data = await state.get_data()
    # Удаляем header (summary), но НЕ prompt — его мы редактируем
    header_id = data.get("header_msg_id")
    if header_id and header_id != callback.message.message_id:
        try:
            await callback.bot.delete_message(callback.message.chat.id, header_id)
        except Exception:
            pass

    await state.clear()
    wo_cache.invalidate()
    try:
        await callback.message.edit_text("❌ Создание акта списания отменено.")
    except Exception:
        pass
    await restore_menu_kb(
        callback.bot,
        callback.message.chat.id,
        state,
        "📝 Списания:",
        writeoffs_keyboard(),
    )
