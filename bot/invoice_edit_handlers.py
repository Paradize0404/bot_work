"""
FSM-редактирование pending приходных накладных.

Вход: callback_data = "inv_edit:{owner_tg_id}"
  — запускается из bot/pending_docs_handlers.py кнопкой ✏️ Редактировать.

Редактируемые поля:
  • Реквизиты: номер накладной, дата, поставщик (per-invoice)
  • Позиции: название, количество, цена (per-item в any invoice)

После завершения (✅ Готово) пересылает обновлённый превью в конец чата.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from use_cases import pending_incoming_invoice as inv_uc
from use_cases.incoming_invoice import format_invoice_preview

logger = logging.getLogger(__name__)

router = Router(name="invoice_edit_handlers")


# ════════════════════════════════════════════════════════
#  FSM States
# ════════════════════════════════════════════════════════


class EditInvoiceStates(StatesGroup):
    choose_what = State()  # меню: Реквизиты / Позиции / Готово
    choose_invoice = State()  # выбрать накладную (если > 1)
    choose_field = State()  # Номер / Дата / Поставщик
    choose_item = State()  # выбрать позицию
    choose_item_field = State()  # Название / Количество / Цена
    enter_value = State()  # ввести текстовое значение


# ════════════════════════════════════════════════════════
#  Keyboards
# ════════════════════════════════════════════════════════


def _menu_kb(owner_tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧾 Реквизиты  (номер / дата / поставщик)",
                    callback_data=f"inv_ed_req:{owner_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Позиции  (название / кол-во / цена)",
                    callback_data=f"inv_ed_items:{owner_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Готово",
                    callback_data=f"inv_ed_done:{owner_tg_id}",
                )
            ],
        ]
    )


def _back_kb(owner_tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"inv_ed_back:{owner_tg_id}"
                )
            ]
        ]
    )


# ════════════════════════════════════════════════════════
#  Entry: inv_edit:{owner_tg_id}
# ════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("inv_edit:"))
async def inv_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        owner_tg_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    logger.info("[inv_edit] start tg:%d owner:%d", callback.from_user.id, owner_tg_id)
    invoices = await inv_uc.get(owner_tg_id)
    if not invoices:
        await callback.answer("⚠️ Накладная не найдена", show_alert=True)
        return

    await state.set_state(EditInvoiceStates.choose_what)
    await state.update_data(inv_owner=owner_tg_id)
    await callback.message.edit_text(
        "✏️ <b>Редактирование накладной</b>\n\nЧто нужно изменить?",
        parse_mode="HTML",
        reply_markup=_menu_kb(owner_tg_id),
    )


# ════════════════════════════════════════════════════════
#  Реквизиты
# ════════════════════════════════════════════════════════


@router.callback_query(EditInvoiceStates.choose_what, F.data.startswith("inv_ed_req:"))
async def inv_ed_req(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] req tg:%d", callback.from_user.id)
    data = await state.get_data()
    owner_tg_id = data["inv_owner"]
    invoices = await inv_uc.get(owner_tg_id)

    if len(invoices) == 1:
        await state.update_data(inv_inv_idx=0)
        await _show_req_fields(callback, owner_tg_id, 0, invoices[0])
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"№{inv.get('documentNumber', '?')}  ·  {inv.get('store_name', '?')}",
                    callback_data=f"inv_ed_req_inv:{owner_tg_id}:{i}",
                )
            ]
            for i, inv in enumerate(invoices)
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"inv_ed_back:{owner_tg_id}"
                )
            ]
        )
        await callback.message.edit_text(
            "Выберите накладную для редактирования реквизитов:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await state.set_state(EditInvoiceStates.choose_invoice)
        # сохраняем контекст для маршрутизации выбора
        await state.update_data(inv_choose_context="req")


async def _show_req_fields(
    callback: CallbackQuery, owner_tg_id: int, inv_idx: int, inv: dict
) -> None:
    date_display = (inv.get("dateIncoming") or "")[:10]
    await callback.message.edit_text(
        f"📄 Накладная №{inv.get('documentNumber', '?')}  ·  {inv.get('store_name', '?')}\n"
        "Что изменить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🔢 Номер: {inv.get('documentNumber', '?')}",
                        callback_data=f"inv_ef:{owner_tg_id}:{inv_idx}:documentNumber",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"📅 Дата: {date_display or '—'}",
                        callback_data=f"inv_ef:{owner_tg_id}:{inv_idx}:dateIncoming",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"🏭 Поставщик: {inv.get('supplier_name', '?')}",
                        callback_data=f"inv_ef:{owner_tg_id}:{inv_idx}:supplier_name",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data=f"inv_ed_back:{owner_tg_id}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    EditInvoiceStates.choose_invoice, F.data.startswith("inv_ed_req_inv:")
)
async def inv_ed_req_inv(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] req_inv tg:%d", callback.from_user.id)
    try:
        _, _, owner_str, idx_str = callback.data.split(":")
        owner_tg_id, inv_idx = int(owner_str), int(idx_str)
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    invoices = await inv_uc.get(owner_tg_id)
    await state.update_data(inv_inv_idx=inv_idx)
    await state.set_state(EditInvoiceStates.choose_what)
    await _show_req_fields(callback, owner_tg_id, inv_idx, invoices[inv_idx])


# callback: inv_ef:{owner}:{inv_idx}:{field}
@router.callback_query(F.data.startswith("inv_ef:"))
async def inv_ef_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] ef_cb tg:%d", callback.from_user.id)
    try:
        parts = callback.data.split(":")
        owner_tg_id, inv_idx, field = int(parts[1]), int(parts[2]), parts[3]
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return

    PROMPTS = {
        "documentNumber": "🔢 Введите новый номер накладной:",
        "dateIncoming": "📅 Введите дату в формате ДД.ММ.ГГГГ:",
        "supplier_name": "🏭 Введите название поставщика:",
    }
    await state.set_state(EditInvoiceStates.enter_value)
    await state.update_data(
        inv_owner=owner_tg_id,
        inv_inv_idx=inv_idx,
        inv_edit_field=field,
        inv_edit_scope="requisite",
    )
    await callback.message.edit_text(
        PROMPTS.get(field, f"Введите новое значение для {field}:"),
        reply_markup=_back_kb(owner_tg_id),
    )


# ════════════════════════════════════════════════════════
#  Позиции
# ════════════════════════════════════════════════════════


@router.callback_query(
    EditInvoiceStates.choose_what, F.data.startswith("inv_ed_items:")
)
async def inv_ed_items(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] items tg:%d", callback.from_user.id)
    data = await state.get_data()
    owner_tg_id = data["inv_owner"]
    invoices = await inv_uc.get(owner_tg_id)

    if len(invoices) == 1:
        await state.update_data(inv_item_inv_idx=0)
        await state.set_state(EditInvoiceStates.choose_item)
        await _show_item_list(callback, owner_tg_id, 0, invoices[0]["items"])
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text=(
                        f"№{inv.get('documentNumber','?')}  ·  "
                        f"{inv.get('store_name','?')}  ({len(inv.get('items',[]))} поз.)"
                    ),
                    callback_data=f"inv_ed_items_inv:{owner_tg_id}:{i}",
                )
            ]
            for i, inv in enumerate(invoices)
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"inv_ed_back:{owner_tg_id}"
                )
            ]
        )
        await callback.message.edit_text(
            "Выберите накладную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await state.set_state(EditInvoiceStates.choose_invoice)
        await state.update_data(inv_choose_context="items")


@router.callback_query(
    EditInvoiceStates.choose_invoice, F.data.startswith("inv_ed_items_inv:")
)
async def inv_ed_items_inv(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] items_inv tg:%d", callback.from_user.id)
    try:
        _, _, owner_str, idx_str = callback.data.split(":")
        owner_tg_id, inv_idx = int(owner_str), int(idx_str)
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    invoices = await inv_uc.get(owner_tg_id)
    await state.update_data(inv_item_inv_idx=inv_idx)
    await state.set_state(EditInvoiceStates.choose_item)
    await _show_item_list(callback, owner_tg_id, inv_idx, invoices[inv_idx]["items"])


async def _show_item_list(
    callback: CallbackQuery,
    owner_tg_id: int,
    inv_idx: int,
    items: list[dict],
) -> None:
    rows = []
    for j, item in enumerate(items):
        name = (item.get("iiko_name") or item.get("raw_name") or "?")[:28]
        qty = item.get("amount", 0)
        price = item.get("price", 0)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{j + 1}. {name}  ×{qty:g}  {price:.2f}₽",
                    callback_data=f"inv_ed_item:{owner_tg_id}:{inv_idx}:{j}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"inv_ed_back:{owner_tg_id}"
            )
        ]
    )
    await callback.message.edit_text(
        "📦 Выберите позицию для редактирования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# callback: inv_ed_item:{owner}:{inv_idx}:{item_idx}
@router.callback_query(F.data.startswith("inv_ed_item:"))
async def inv_ed_item(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] item tg:%d", callback.from_user.id)
    try:
        parts = callback.data.split(":")
        owner_tg_id, inv_idx, item_idx = int(parts[1]), int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    invoices = await inv_uc.get(owner_tg_id)
    item = invoices[inv_idx]["items"][item_idx]
    name = item.get("iiko_name") or item.get("raw_name") or "?"
    await state.update_data(
        inv_owner=owner_tg_id,
        inv_item_inv_idx=inv_idx,
        inv_item_idx=item_idx,
    )
    await state.set_state(EditInvoiceStates.choose_item_field)
    await callback.message.edit_text(
        f"📦 <b>{name}</b>\n"
        f"Кол-во: {item.get('amount', 0):g}  |  Цена: {item.get('price', 0):.2f}₽\n"
        "Что изменить?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Название",
                        callback_data=(
                            f"inv_if:{owner_tg_id}:{inv_idx}:{item_idx}:iiko_name"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"📊 Количество: {item.get('amount', 0):g}",
                        callback_data=(
                            f"inv_if:{owner_tg_id}:{inv_idx}:{item_idx}:amount"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"💵 Цена: {item.get('price', 0):.2f}₽",
                        callback_data=(
                            f"inv_if:{owner_tg_id}:{inv_idx}:{item_idx}:price"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data=f"inv_ed_back:{owner_tg_id}",
                    )
                ],
            ]
        ),
    )


# callback: inv_if:{owner}:{inv_idx}:{item_idx}:{field}
@router.callback_query(F.data.startswith("inv_if:"))
async def inv_if_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[inv_edit] if_cb tg:%d", callback.from_user.id)
    try:
        parts = callback.data.split(":")
        owner_tg_id, inv_idx, item_idx, field = (
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            parts[4],
        )
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    PROMPTS = {
        "iiko_name": "✏️ Введите новое название товара:",
        "amount": "📊 Введите новое количество (например: 5 или 2.5):",
        "price": "💵 Введите новую цену за единицу (например: 150.50):",
    }
    await state.set_state(EditInvoiceStates.enter_value)
    await state.update_data(
        inv_owner=owner_tg_id,
        inv_item_inv_idx=inv_idx,
        inv_item_idx=item_idx,
        inv_edit_field=field,
        inv_edit_scope="item",
    )
    await callback.message.edit_text(
        PROMPTS.get(field, f"Введите значение для {field}:"),
        reply_markup=_back_kb(owner_tg_id),
    )


# ════════════════════════════════════════════════════════
#  Ввод значения
# ════════════════════════════════════════════════════════


@router.message(EditInvoiceStates.enter_value)
async def inv_enter_value(message: Message, state: FSMContext) -> None:
    logger.info("[inv_edit] enter_value tg:%d", message.from_user.id)
    data = await state.get_data()
    owner_tg_id: int = data["inv_owner"]
    scope: str = data.get("inv_edit_scope", "")
    field: str = data["inv_edit_field"]
    raw = (message.text or "").strip()

    invoices = await inv_uc.get(owner_tg_id)
    if not invoices:
        await message.answer("⚠️ Накладная не найдена.")
        await state.clear()
        return

    try:
        if scope == "requisite":
            inv_idx: int = data["inv_inv_idx"]
            if field == "dateIncoming":
                dt = datetime.strptime(raw, "%d.%m.%Y")
                value: object = dt.strftime("%Y-%m-%dT00:00:00")
            else:
                value = raw
            invoices[inv_idx][field] = value

        elif scope == "item":
            inv_idx = data["inv_item_inv_idx"]
            item_idx: int = data["inv_item_idx"]
            item = invoices[inv_idx]["items"][item_idx]
            if field in ("amount", "price"):
                num = round(float(raw.replace(",", ".")), 4 if field == "amount" else 2)
                item[field] = num
                item["sum"] = round(
                    float(item.get("amount", 0)) * float(item.get("price", 0)), 2
                )
            else:
                # iiko_name
                item["iiko_name"] = raw
                item["raw_name"] = raw

    except ValueError:
        await message.answer(
            "⚠️ Неверный формат. Попробуйте ещё раз:",
            reply_markup=_back_kb(owner_tg_id),
        )
        return

    await inv_uc.update_invoices(owner_tg_id, invoices)
    await state.set_state(EditInvoiceStates.choose_what)
    await message.answer("✅ Сохранено.", reply_markup=_menu_kb(owner_tg_id))


# ════════════════════════════════════════════════════════
#  Готово / Назад
# ════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("inv_ed_done:"))
async def inv_ed_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Завершить редактирование: показать обновлённый превью."""
    await callback.answer()
    logger.info("[inv_edit] done tg:%d", callback.from_user.id)
    data = await state.get_data()
    try:
        owner_tg_id = data.get("inv_owner") or int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    tg_id = callback.from_user.id

    invoices = await inv_uc.get(owner_tg_id)
    await state.clear()

    if not invoices:
        await callback.message.answer("✅ Редактирование завершено.")
        return

    # Удаляем старое summary-сообщение
    msg_ids = await inv_uc.get_summary_msg_ids(owner_tg_id)
    old_msg_id = msg_ids.get(tg_id)
    if old_msg_id:
        try:
            await callback.bot.delete_message(tg_id, old_msg_id)
        except Exception:
            logger.debug("suppressed", exc_info=True)
    # Пересылаем обновлённый превью
    info = await inv_uc.get_info_for_user(owner_tg_id)
    author_line = (
        f"👤 {info.author_name or '?'}  🏬 {info.department_name or '?'}"
        if info
        else ""
    )
    preview = format_invoice_preview(invoices)
    text = f"{author_line}\n\n{preview}".strip() if author_line else preview

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Отправить в iiko",
                    callback_data=f"iiko_invoice_send:{owner_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать",
                    callback_data=f"inv_edit:{owner_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"iiko_invoice_cancel:{owner_tg_id}",
                )
            ],
        ]
    )
    new_msg = await callback.bot.send_message(
        tg_id, text, parse_mode="HTML", reply_markup=kb
    )
    await inv_uc.update_summary_msg_ids(owner_tg_id, tg_id, new_msg.message_id)


@router.callback_query(F.data.startswith("inv_ed_back:"))
async def inv_ed_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться в главное меню редактирования."""
    await callback.answer()
    logger.info("[inv_edit] back tg:%d", callback.from_user.id)
    data = await state.get_data()
    try:
        owner_tg_id = data.get("inv_owner") or int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка", show_alert=True)
        return
    await state.set_state(EditInvoiceStates.choose_what)
    await state.update_data(inv_owner=owner_tg_id)
    await callback.message.edit_text(
        "✏️ Что ещё изменить?",
        reply_markup=_menu_kb(owner_tg_id),
    )
