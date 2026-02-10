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
    choose_field = State()       # склад / счёт / позиции
    choose_store = State()       # выбор нового склада
    choose_account = State()     # выбор нового счёта
    choose_item_idx = State()    # какой номер позиции
    choose_item_action = State() # наименование или количество
    new_product_search = State() # поиск нового товара
    new_quantity = State()       # ввод нового количества


# ══════════════════════════════════════════════════════
#  Клавиатуры — создание
# ══════════════════════════════════════════════════════

def _stores_kb(stores: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=s["name"], callback_data=f"wo_store:{s['id']}")]
        for s in stores
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


ACC_PAGE_SIZE = 10


def _accounts_kb(accounts: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    total = len(accounts)
    start = page * ACC_PAGE_SIZE
    end = start + ACC_PAGE_SIZE
    page_items = accounts[start:end]

    buttons = [
        [InlineKeyboardButton(text=a["name"], callback_data=f"wo_acc:{a['id']}")]
        for a in page_items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"wo_acc_page:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️ Далее", callback_data=f"wo_acc_page:{page + 1}"))
    if nav:
        total_pages = (total + ACC_PAGE_SIZE - 1) // ACC_PAGE_SIZE
        nav.insert(len(nav) // 2, InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="wo_noop",
        ))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _products_kb(products: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"wo_prod:{p['id']}")]
        for p in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _add_more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить на проверку", callback_data="wo_send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="wo_cancel")],
    ])


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
    data = await state.get_data()
    header_id = data.get("header_msg_id")
    text = _build_summary(data)
    if header_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=header_id,
                                        text=text, parse_mode="HTML")
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("[writeoff] summary edit fail: %s", exc)
    msg = await bot.send_message(chat_id, text, parse_mode="HTML")
    await state.update_data(header_msg_id=msg.message_id)


async def _send_prompt(
    bot: Bot, chat_id: int, state: FSMContext,
    text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    data = await state.get_data()
    prompt_id = data.get("prompt_msg_id")
    if prompt_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=prompt_id,
                                        text=text, reply_markup=reply_markup)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("[writeoff] prompt edit fail: %s", exc)
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await state.update_data(prompt_msg_id=msg.message_id)


# ══════════════════════════════════════════════════════
#  Защита: текст в inline-состояниях
# ══════════════════════════════════════════════════════

@router.message(WriteoffStates.store)
async def _ignore_text_store(message: Message) -> None:
    logger.debug("[writeoff] Текст в store-состоянии tg:%d, text='%s'", message.from_user.id, message.text)
    try: await message.delete()
    except Exception: pass
    await message.answer("👆 Нажмите на кнопку склада выше.")


@router.message(WriteoffStates.account)
async def _ignore_text_account(message: Message) -> None:
    logger.debug("[writeoff] Текст в account-состоянии tg:%d, text='%s'", message.from_user.id, message.text)
    try: await message.delete()
    except Exception: pass
    await message.answer("👆 Нажмите на кнопку счёта выше.")


# ══════════════════════════════════════════════════════
#  СОЗДАНИЕ АКТА (сотрудник) — шаги 1–7
# ══════════════════════════════════════════════════════

# ── 1. Старт ──

@router.message(F.text == "📝 Создать списание")
async def start_writeoff(message: Message, state: FSMContext) -> None:
    await state.clear()
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала авторизуйтесь (/start) и выберите ресторан.")
        return

    logger.info("[writeoff] Старт. user=%d, dept=%s (%s), role=%s",
                message.from_user.id, ctx.department_id, ctx.department_name, ctx.role_name)

    # Параллельно: склады + is_admin (экономим ~400 мс)
    stores, is_bot_admin = await asyncio.gather(
        wo_uc.get_stores_for_department(ctx.department_id),
        admin_uc.is_admin(message.from_user.id),
    )
    if not stores:
        await message.answer("❌ У вашего подразделения нет складов (бар/кухня).")
        return

    await state.update_data(
        user_fullname=ctx.employee_name,
        department_id=ctx.department_id,
        items=[],
        _stores_cache=stores,
    )

    # ── Авто-выбор склада по должности (бот-админы всегда выбирают вручную) ──
    if is_bot_admin:
        role_type = "admin"
        store_keyword = None
        logger.info("[writeoff] Бот-админ tg:%d — ручной выбор склада", message.from_user.id)
    else:
        role_type = wo_uc.classify_role(ctx.role_name)
        store_keyword = wo_uc.get_store_keyword_for_role(role_type)

    if store_keyword:
        # Ищем склад, в названии которого есть ключевое слово (бар/кухня)
        matched = [s for s in stores if store_keyword in s["name"].lower()]
        if matched:
            auto_store = matched[0]
            await state.update_data(store_id=auto_store["id"], store_name=auto_store["name"])
            logger.info("[writeoff] Авто-склад по роли «%s» → %s (%s)",
                        ctx.role_name, auto_store["name"], auto_store["id"])

            summary_msg = await message.answer(_build_summary(await state.get_data()), parse_mode="HTML")
            await state.update_data(header_msg_id=summary_msg.message_id)

            # Переходим сразу к выбору счёта
            accounts = await wo_uc.get_writeoff_accounts(auto_store["name"])
            if not accounts:
                msg = await message.answer(
                    f"🏬 Склад: <b>{auto_store['name']}</b> (авто)\n"
                    "⚠️ Нет счетов списания для этого склада.",
                    parse_mode="HTML",
                )
                await state.update_data(prompt_msg_id=msg.message_id)
                await state.clear()
                return

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

        # Если склад по ключевому слову не найден — показываем выбор
        logger.warning("[writeoff] Склад «%s» не найден для роли «%s», показываю выбор",
                       store_keyword, ctx.role_name)

    # ── Ручной выбор склада (для админов / если авто не сработал) ──
    summary_msg = await message.answer(_build_summary(await state.get_data()), parse_mode="HTML")
    await state.update_data(header_msg_id=summary_msg.message_id)
    msg = await message.answer("🏬 Выберите склад:", reply_markup=_stores_kb(stores))
    await state.update_data(prompt_msg_id=msg.message_id)
    await state.set_state(WriteoffStates.store)


# ── 2. Выбор склада ──

@router.callback_query(WriteoffStates.store, F.data.startswith("wo_store:"))
async def choose_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff] Выбор склада tg:%d, store_id=%s", callback.from_user.id, store_id)
    data = await state.get_data()
    stores = data.get("_stores_cache") or await wo_uc.get_stores_for_department(data["department_id"])
    store = next((s for s in stores if s["id"] == store_id), None)
    if not store:
        await callback.answer("❌ Склад не найден", show_alert=True)
        return

    await state.update_data(store_id=store_id, store_name=store["name"])
    logger.info("[writeoff] Склад: %s (%s)", store["name"], store_id)
    await _update_summary(callback.bot, callback.message.chat.id, state)

    accounts = await wo_uc.get_writeoff_accounts(store["name"])
    if not accounts:
        await _send_prompt(callback.bot, callback.message.chat.id, state,
                           "⚠️ Нет счетов списания для этого склада.")
        await state.clear()
        return

    await state.update_data(_accounts_cache=accounts)
    await state.set_state(WriteoffStates.account)
    await _send_prompt(callback.bot, callback.message.chat.id, state,
                       f"📂 Выберите счёт списания ({len(accounts)}):",
                       reply_markup=_accounts_kb(accounts, page=0))


# ── 3. Выбор счёта ──

@router.callback_query(WriteoffStates.account, F.data == "wo_noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(WriteoffStates.account, F.data.startswith("wo_acc_page:"))
async def accounts_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    logger.debug("[writeoff] Пагинация счетов tg:%d, page=%d", callback.from_user.id, page)
    data = await state.get_data()
    accounts = data.get("_accounts_cache") or await wo_uc.get_writeoff_accounts(data.get("store_name", ""))
    await _send_prompt(callback.bot, callback.message.chat.id, state,
                       f"📂 Выберите счёт списания ({len(accounts)}):",
                       reply_markup=_accounts_kb(accounts, page=page))


@router.callback_query(WriteoffStates.account, F.data.startswith("wo_acc:"))
async def choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff] Выбор счёта tg:%d, acc_id=%s", callback.from_user.id, account_id)
    data = await state.get_data()
    accounts = data.get("_accounts_cache") or await wo_uc.get_writeoff_accounts(data.get("store_name", ""))
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        await callback.answer("❌ Счёт не найден", show_alert=True)
        return

    await state.update_data(account_id=account_id, account_name=account["name"])
    logger.info("[writeoff] Счёт: %s (%s)", account["name"], account_id)
    await _update_summary(callback.bot, callback.message.chat.id, state)
    await state.set_state(WriteoffStates.reason)
    await _send_prompt(callback.bot, callback.message.chat.id, state,
                       "📝 Введите причину списания:")


# ── 4. Причина ──

@router.message(WriteoffStates.reason)
async def set_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    logger.info("[writeoff] Ввод причины tg:%d, len=%d", message.from_user.id, len(reason))
    try: await message.delete()
    except Exception: pass

    if not reason:
        await message.answer("❌ Причина не может быть пустой.")
        return
    if len(reason) > 500:
        await message.answer("❌ Макс. 500 символов.")
        return

    await state.update_data(reason=reason)
    logger.info("[writeoff] Причина: %s", reason)
    await _update_summary(message.bot, message.chat.id, state)
    await state.set_state(WriteoffStates.add_items)
    await _send_prompt(message.bot, message.chat.id, state,
                       "🔍 Введите часть названия товара для поиска:")


# ── 5. Поиск товара ──

@router.message(WriteoffStates.add_items)
async def search_product(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    logger.info("[writeoff] Поиск товара tg:%d, query='%s'", message.from_user.id, query)
    try: await message.delete()
    except Exception: pass
    if not query:
        return
    if len(query) < 2:
        data = await state.get_data()
        await _send_prompt(message.bot, message.chat.id, state,
                           "❌ Минимум 2 символа для поиска:",
                           reply_markup=_add_more_kb() if data.get("items") else None)
        return

    data = await state.get_data()
    if len(data.get("items", [])) >= MAX_ITEMS:
        await _send_prompt(message.bot, message.chat.id, state,
                           f"⚠️ Макс. {MAX_ITEMS} позиций. Нажмите «Отправить».",
                           reply_markup=_add_more_kb())
        return

    products = await wo_uc.search_products(query)
    if not products:
        await _send_prompt(message.bot, message.chat.id, state,
                           "🔎 Ничего не найдено. Попробуйте другой запрос:",
                           reply_markup=_add_more_kb() if data.get("items") else None)
        return

    cache = {p["id"]: p for p in products}
    await state.update_data(product_cache=cache)
    sel_id = data.get("selection_msg_id")
    kb = _products_kb(products)
    if sel_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=sel_id,
                text=f"Найдено {len(products)}. Выберите товар:", reply_markup=kb)
            return
        except Exception:
            pass
    msg = await message.answer(f"Найдено {len(products)}. Выберите товар:", reply_markup=kb)
    await state.update_data(selection_msg_id=msg.message_id)


@router.callback_query(WriteoffStates.add_items, F.data.startswith("wo_prod:"))
async def select_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    product_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff] Выбор товара tg:%d, prod_id=%s", callback.from_user.id, product_id)
    data = await state.get_data()
    product = data.get("product_cache", {}).get(product_id)
    if not product:
        await callback.answer("❌ Товар не найден. Повторите поиск.", show_alert=True)
        return

    logger.info("[writeoff] Товар: %s (%s)", product["name"], product_id)

    # Единицы уже заполнены в search_products (batch-resolve), fallback на DB
    unit_name = product.get("unit_name") or await wo_uc.get_unit_name(product.get("main_unit"))
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
        current_item=product, current_unit_name=unit_name,
        current_unit_norm=norm, current_unit_label=unit_label,
        selection_msg_id=None,
    )
    await state.set_state(WriteoffStates.quantity)
    try:
        await callback.message.edit_text(prompt, parse_mode="HTML")
    except Exception:
        msg = await callback.message.answer(prompt, parse_mode="HTML")
        await state.update_data(quantity_prompt_id=msg.message_id)
        return
    await state.update_data(quantity_prompt_id=callback.message.message_id)


# ── 6. Количество ──

@router.message(WriteoffStates.quantity)
async def save_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", ".").strip()
    logger.info("[writeoff] Ввод количества tg:%d, raw='%s'", message.from_user.id, raw)
    try:
        qty = float(raw)
    except ValueError:
        await message.answer("❌ Введите число. Пример: 500 или 1.5")
        return
    if qty < QTY_MIN:
        await message.answer(f"❌ Минимум {QTY_MIN}.")
        return
    if qty > QTY_MAX:
        await message.answer(f"❌ Макс. {QTY_MAX}.")
        return

    try: await message.delete()
    except Exception: pass

    data = await state.get_data()
    item = data.get("current_item")
    if not item:
        await state.set_state(WriteoffStates.add_items)
        await _send_prompt(message.bot, message.chat.id, state,
                           "⚠️ Что-то пошло не так. Введите название товара заново:")
        return

    norm = data.get("current_unit_norm", "pcs")
    unit_label = data.get("current_unit_label", "шт")
    converted = qty / 1000 if norm in ("kg", "l") else qty

    item["quantity"] = converted
    item["user_quantity"] = qty
    item["unit_label"] = unit_label

    items = data.get("items", [])
    items.append(item)

    q_prompt_id = data.get("quantity_prompt_id")
    if q_prompt_id:
        try: await message.bot.delete_message(chat_id=message.chat.id, message_id=q_prompt_id)
        except Exception: pass

    await state.update_data(items=items, current_item=None, quantity_prompt_id=None)
    logger.info("[writeoff] Позиция: %s — %s %s (→ %s), всего: %d",
                item.get("name"), qty, unit_label, converted, len(items))
    await _update_summary(message.bot, message.chat.id, state)
    await state.set_state(WriteoffStates.add_items)
    await _send_prompt(message.bot, message.chat.id, state,
                       "🔍 Введите название товара или нажмите «Отправить»:",
                       reply_markup=_add_more_kb())


# ══════════════════════════════════════════════════════
#  7. ОТПРАВКА НА ПРОВЕРКУ АДМИНАМ
# ══════════════════════════════════════════════════════

@router.callback_query(WriteoffStates.add_items, F.data == "wo_send")
async def finalize_writeoff(callback: CallbackQuery, state: FSMContext) -> None:
    """Вместо прямой отправки — отправляем документ на проверку админам."""
    user_id = callback.from_user.id
    logger.info("[writeoff] Отправка на проверку tg:%d", user_id)
    if user_id in _sending_lock:
        await callback.answer("⏳ Уже отправляется…")
        return

    _sending_lock.add(user_id)
    try:
        data = await state.get_data()
        items = data.get("items", [])
        if not items:
            await callback.answer("❌ Добавьте хотя бы один товар", show_alert=True)
            return
        non_zero = [i for i in items if i.get("quantity", 0) > 0]
        if not non_zero:
            await callback.answer("❌ Все позиции с количеством 0.", show_alert=True)
            return

        await callback.answer()

        admin_ids = await admin_uc.get_admin_ids()

        if not admin_ids:
            # Нет админов — отправляем напрямую (fallback)
            await _send_prompt(callback.bot, callback.message.chat.id, state,
                               f"⏳ Отправляем акт ({len(non_zero)} позиций)...")
            document = wo_uc.build_writeoff_document(
                store_id=data["store_id"], account_id=data["account_id"],
                reason=data.get("reason", ""), items=items,
                author_name=data.get("user_fullname", ""))
            bot = callback.bot
            chat_id = callback.message.chat.id
            await state.clear()

            async def _bg():
                result = await wo_uc.send_writeoff_document(document)
                await bot.send_message(chat_id, result)
            asyncio.create_task(_bg())
            return

        # Создаём pending-документ
        doc = pending.create(
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

        await _send_prompt(callback.bot, callback.message.chat.id, state,
                           "✅ Акт отправлен на проверку администраторам. Ожидайте.")
        await state.clear()

        # Рассылаем всем админам
        bot = callback.bot
        text = pending.build_summary_text(doc)
        kb = pending.admin_keyboard(doc.doc_id)

        for admin_id in admin_ids:
            try:
                msg = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
                doc.admin_msg_ids[admin_id] = msg.message_id
            except Exception as exc:
                logger.warning("[writeoff] Не удалось отправить админу %d: %s", admin_id, exc)

        logger.info("[writeoff] Документ %s отправлен %d админам",
                    doc.doc_id, len(doc.admin_msg_ids))
    finally:
        _sending_lock.discard(user_id)


# ══════════════════════════════════════════════════════
#  ОБРАБОТКА АДМИНАМИ
# ══════════════════════════════════════════════════════

async def _remove_admin_keyboards(bot: Bot, doc: pending.PendingWriteoff,
                                   status_text: str, except_admin: int = 0) -> None:
    """Убрать кнопки у всех админов (один из них уже обработал)."""
    for admin_id, msg_id in doc.admin_msg_ids.items():
        if admin_id == except_admin:
            continue
        try:
            await bot.edit_message_text(
                chat_id=admin_id, message_id=msg_id,
                text=pending.build_summary_text(doc) + f"\n\n{status_text}",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── Одобрить ──

@router.callback_query(F.data.startswith("woa_approve:"))
async def admin_approve(callback: CallbackQuery) -> None:
    await callback.answer()
    doc_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff] Одобрение tg:%d, doc=%s", callback.from_user.id, doc_id)
    doc = pending.get(doc_id)
    if not doc:
        await callback.answer("⚠️ Документ уже обработан или не найден.", show_alert=True)
        return

    if not pending.try_lock(doc_id):
        await callback.answer("⏳ Другой админ уже обрабатывает этот документ.", show_alert=True)
        return

    bot = callback.bot
    admin_id = callback.from_user.id
    admin_name = callback.from_user.full_name

    # Обновляем сообщение текущего админа
    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc) + f"\n\n⏳ Отправляется в iiko... ({admin_name})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Убираем кнопки у остальных
    await _remove_admin_keyboards(bot, doc,
                                   f"✅ Одобрено admin {admin_name}", except_admin=admin_id)

    # Отправляем в iiko
    document = wo_uc.build_writeoff_document(
        store_id=doc.store_id, account_id=doc.account_id,
        reason=doc.reason, items=doc.items,
        author_name=doc.author_name)
    result = await wo_uc.send_writeoff_document(document)

    # Обновляем сообщение админа
    try:
        await callback.message.edit_text(
            pending.build_summary_text(doc) + f"\n\n{result}\n👤 {admin_name}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Уведомляем автора
    try:
        await bot.send_message(doc.author_chat_id, f"{result}\n(проверил: {admin_name})")
    except Exception:
        pass

    pending.remove(doc_id)
    logger.info("[writeoff] Документ %s одобрен admin %d (%s)", doc_id, admin_id, admin_name)


# ── Отклонить ──

@router.callback_query(F.data.startswith("woa_reject:"))
async def admin_reject(callback: CallbackQuery) -> None:
    await callback.answer()
    doc_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff] Отклонение tg:%d, doc=%s", callback.from_user.id, doc_id)
    doc = pending.get(doc_id)
    if not doc:
        await callback.answer("⚠️ Документ уже обработан.", show_alert=True)
        return
    if not pending.try_lock(doc_id):
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

    await _remove_admin_keyboards(bot, doc,
                                   f"❌ Отклонено admin {admin_name}",
                                   except_admin=callback.from_user.id)
    try:
        await bot.send_message(doc.author_chat_id,
                                f"❌ Акт списания отклонён администратором ({admin_name}).")
    except Exception:
        pass

    pending.remove(doc_id)
    logger.info("[writeoff] Документ %s отклонён admin %d", doc_id, callback.from_user.id)


# ══════════════════════════════════════════════════════
#  РЕДАКТИРОВАНИЕ АДМИНОМ
# ══════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("woa_edit:"))
async def admin_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Админ решил отредактировать документ."""
    await callback.answer()
    doc_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Начало редактирования tg:%d, doc=%s", callback.from_user.id, doc_id)
    doc = pending.get(doc_id)
    if not doc:
        await callback.answer("⚠️ Документ не найден.", show_alert=True)
        return
    if not pending.try_lock(doc_id):
        await callback.answer("⏳ Другой админ уже редактирует.", show_alert=True)
        return

    admin_name = callback.from_user.full_name

    # Убираем кнопки у всех (включая текущего)
    await _remove_admin_keyboards(callback.bot, doc,
                                   f"✏️ Редактирует {admin_name}",
                                   except_admin=0)

    # Сохраняем doc_id в FSM для этого админа
    await state.update_data(edit_doc_id=doc_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏬 Склад", callback_data="woe_field:store")],
        [InlineKeyboardButton(text="📂 Счёт", callback_data="woe_field:account")],
        [InlineKeyboardButton(text="📦 Позиции", callback_data="woe_field:items")],
        [InlineKeyboardButton(text="❌ Отмена редактирования", callback_data="woe_cancel")],
    ])
    await state.set_state(AdminEditStates.choose_field)
    await callback.message.answer(
        pending.build_summary_text(doc) + "\n\n✏️ <b>Что редактируем?</b>",
        parse_mode="HTML", reply_markup=kb)


# ── Отмена редактирования ──

@router.callback_query(F.data == "woe_cancel")
async def admin_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    logger.info("[writeoff-edit] Отмена редактирования tg:%d, doc=%s", callback.from_user.id, doc_id)
    await state.clear()

    if doc_id:
        pending.unlock(doc_id)
        doc = pending.get(doc_id)
        if doc:
            # Перерассылаем кнопки заново
            text = pending.build_summary_text(doc)
            kb = pending.admin_keyboard(doc_id)
            _ids = await admin_uc.get_admin_ids()
            for admin_id in _ids:
                try:
                    msg = await callback.bot.send_message(admin_id, text,
                                                           parse_mode="HTML", reply_markup=kb)
                    doc.admin_msg_ids[admin_id] = msg.message_id
                except Exception:
                    pass

    try: await callback.message.edit_text("❌ Редактирование отменено.")
    except Exception: pass


# ── Выбор поля для редактирования ──

@router.callback_query(AdminEditStates.choose_field, F.data.startswith("woe_field:"))
async def admin_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    field = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Выбор поля tg:%d, field=%s", callback.from_user.id, field)
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = pending.get(doc_id) if doc_id else None
    if not doc:
        await state.clear()
        await callback.answer("⚠️ Документ не найден.", show_alert=True)
        return

    if field == "store":
        stores = await wo_uc.get_stores_for_department(doc.department_id)
        if not stores:
            await callback.message.answer("❌ Нет доступных складов.")
            return
        await state.update_data(_edit_stores=stores)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s["name"], callback_data=f"woe_store:{s['id']}")]
            for s in stores
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]])
        await state.set_state(AdminEditStates.choose_store)
        await callback.message.edit_text("🏬 Выберите новый склад:", reply_markup=kb)

    elif field == "account":
        accounts = await wo_uc.get_writeoff_accounts(doc.store_name)
        if not accounts:
            await callback.message.answer("❌ Нет счетов.")
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
            await callback.message.answer("❌ В документе нет позиций.")
            return
        buttons = [
            [InlineKeyboardButton(
                text=f"{i}. {item['name']} — {item.get('user_quantity', item.get('quantity', 0))} {item.get('unit_label', 'шт')}",
                callback_data=f"woe_item:{i-1}")]
            for i, item in enumerate(items, 1)
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await state.set_state(AdminEditStates.choose_item_idx)
        await callback.message.edit_text("📦 Какую позицию редактировать?", reply_markup=kb)


# ── Новый склад ──

@router.callback_query(AdminEditStates.choose_store, F.data.startswith("woe_store:"))
async def admin_edit_store(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    store_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Новый склад tg:%d, store_id=%s", callback.from_user.id, store_id)
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = pending.get(doc_id) if doc_id else None
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
    logger.info("[writeoff-edit] Склад изменён на %s (%s)", store["name"], store_id)

    await _finish_edit(callback, state, doc)


# ── Новый счёт ──

@router.callback_query(AdminEditStates.choose_account, F.data.startswith("woe_acc:"))
async def admin_edit_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Новый счёт tg:%d, acc_id=%s", callback.from_user.id, account_id)
    data = await state.get_data()
    doc_id = data.get("edit_doc_id")
    doc = pending.get(doc_id) if doc_id else None
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
    logger.info("[writeoff-edit] Счёт изменён на %s (%s)", account["name"], account_id)

    await _finish_edit(callback, state, doc)


# ── Выбор позиции ──

@router.callback_query(AdminEditStates.choose_item_idx, F.data.startswith("woe_item:"))
async def admin_edit_item_idx(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    idx = int(callback.data.split(":", 1)[1])
    logger.info("[writeoff-edit] Выбор позиции tg:%d, idx=%d", callback.from_user.id, idx)
    data = await state.get_data()
    doc = pending.get(data.get("edit_doc_id", ""))
    if not doc or idx >= len(doc.items):
        await callback.answer("❌ Позиция не найдена", show_alert=True)
        return

    item = doc.items[idx]
    await state.update_data(edit_item_idx=idx)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Сменить наименование", callback_data="woe_action:name")],
        [InlineKeyboardButton(text="🔢 Изменить количество", callback_data="woe_action:qty")],
        [InlineKeyboardButton(text="🗑 Удалить позицию", callback_data="woe_action:delete")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")],
    ])
    uq = item.get("user_quantity", item.get("quantity", 0))
    ul = item.get("unit_label", "шт")
    await state.set_state(AdminEditStates.choose_item_action)
    await callback.message.edit_text(
        f"📦 Позиция #{idx+1}: <b>{item['name']}</b> — {uq} {ul}\n\nЧто меняем?",
        parse_mode="HTML", reply_markup=kb)


# ── Действие с позицией ──

@router.callback_query(AdminEditStates.choose_item_action, F.data.startswith("woe_action:"))
async def admin_edit_item_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    action = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Действие с позицией tg:%d, action=%s", callback.from_user.id, action)
    data = await state.get_data()
    doc = pending.get(data.get("edit_doc_id", ""))
    idx = data.get("edit_item_idx", -1)

    if not doc or idx < 0 or idx >= len(doc.items):
        await state.clear()
        return

    if action == "delete":
        removed = doc.items.pop(idx)
        logger.info("[writeoff-edit] Удалена позиция #%d: %s", idx+1, removed.get("name"))
        await _finish_edit(callback, state, doc)
        return

    if action == "name":
        await state.set_state(AdminEditStates.new_product_search)
        await callback.message.edit_text("🔍 Введите часть названия нового товара:")
        return

    if action == "qty":
        item = doc.items[idx]
        unit_label = item.get("unit_label", "шт")
        await state.set_state(AdminEditStates.new_quantity)
        await callback.message.edit_text(
            f"🔢 Введите новое количество ({unit_label}) для «{item['name']}»:")
        return


# ── Поиск нового товара (замена наименования) ──

@router.message(AdminEditStates.new_product_search)
async def admin_search_new_product(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    logger.info("[writeoff-edit] Поиск нового товара tg:%d, query='%s'", message.from_user.id, query)
    try: await message.delete()
    except Exception: pass
    if len(query) < 2:
        await message.answer("❌ Минимум 2 символа.")
        return

    products = await wo_uc.search_products(query)
    if not products:
        await message.answer("🔎 Ничего. Попробуйте другой запрос:")
        return

    cache = {p["id"]: p for p in products}
    await state.update_data(_edit_product_cache=cache)

    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"woe_newprod:{p['id']}")]
        for p in products
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="woe_cancel")]]
    await message.answer("Выберите новый товар:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdminEditStates.new_product_search, F.data.startswith("woe_newprod:"))
async def admin_pick_new_product(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pid = callback.data.split(":", 1)[1]
    logger.info("[writeoff-edit] Выбран новый товар tg:%d, prod_id=%s", callback.from_user.id, pid)
    data = await state.get_data()
    doc = pending.get(data.get("edit_doc_id", ""))
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
    logger.info("[writeoff-edit] Позиция #%d: %s → %s", idx+1, old_name, product["name"])
    await _finish_edit(callback, state, doc)


# ── Ввод нового количества ──

@router.message(AdminEditStates.new_quantity)
async def admin_set_new_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(",", ".").strip()
    logger.info("[writeoff-edit] Новое количество tg:%d, raw='%s'", message.from_user.id, raw)
    try:
        qty = float(raw)
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    if qty < QTY_MIN or qty > QTY_MAX:
        await message.answer(f"❌ Допустимо: {QTY_MIN}–{QTY_MAX}.")
        return

    try: await message.delete()
    except Exception: pass

    data = await state.get_data()
    doc = pending.get(data.get("edit_doc_id", ""))
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
    logger.info("[writeoff-edit] Позиция #%d кол-во: %s → %s", idx+1, qty, converted)

    await _finish_edit_msg(message, state, doc)


# ── Завершение редактирования → назад к кнопкам ──

async def _finish_edit(callback: CallbackQuery, state: FSMContext,
                       doc: pending.PendingWriteoff) -> None:
    """Завершить редактирование: разблокировать, разослать обновлённый документ."""
    doc_id = doc.doc_id
    logger.info("[writeoff-edit] Завершение редактирования tg:%d, doc=%s", callback.from_user.id, doc_id)
    await state.clear()
    pending.unlock(doc_id)

    text = pending.build_summary_text(doc)
    kb = pending.admin_keyboard(doc_id)

    # Обновляем сообщение текущего админа
    try:
        await callback.message.edit_text(text + "\n\n✏️ <i>Отредактировано</i>",
                                          parse_mode="HTML")
    except Exception:
        pass

    # Рассылаем обновлённый документ всем админам
    _ids = await admin_uc.get_admin_ids()
    for admin_id in _ids:
        try:
            msg = await callback.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
            doc.admin_msg_ids[admin_id] = msg.message_id
        except Exception:
            pass


async def _finish_edit_msg(message: Message, state: FSMContext,
                           doc: pending.PendingWriteoff) -> None:
    """То же, но из message-хэндлера (не callback)."""
    doc_id = doc.doc_id
    logger.info("[writeoff-edit] Завершение редактирования (msg) tg:%d, doc=%s", message.from_user.id, doc_id)
    await state.clear()
    pending.unlock(doc_id)

    text = pending.build_summary_text(doc)
    kb = pending.admin_keyboard(doc_id)

    await message.answer(text + "\n\n✏️ <i>Отредактировано</i>", parse_mode="HTML")

    _ids = await admin_uc.get_admin_ids()
    for admin_id in _ids:
        try:
            msg = await message.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
            doc.admin_msg_ids[admin_id] = msg.message_id
        except Exception:
            pass


# ══════════════════════════════════════════════════════
#  Отмена создания
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "wo_cancel")
async def cancel_writeoff(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[writeoff] Отменено user %d", callback.from_user.id)
    await state.clear()
    wo_cache.invalidate()
    try: await callback.message.edit_text("❌ Создание акта списания отменено.")
    except Exception: await callback.message.answer("❌ Создание акта списания отменено.")
