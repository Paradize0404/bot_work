"""
Просмотр всех неотправленных документов (Ожидают отправки в iiko).

Показывает:
  • 📝 Списания — ожидают одобрения администратора
  • 📦 Заявки   — со статусом «pending»
  • 🧾 Приходные накладные — OCR-фото / JSON-чеки, готовые к отправке

Видимость:
  • Администратор — видит всё
  • Бухгалтер (PERM_OCR_SEND) — видит все накладные + свои списания/заявки
  • Обычный пользователь — только свои документы
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

from bot.middleware import auth_required
from use_cases import user_context as uctx
from use_cases import pending_all as all_uc
from use_cases import pending_writeoffs as wo_uc
from use_cases import product_request as req_uc
from use_cases import pending_incoming_invoice as inv_uc
from use_cases.permissions import has_permission
from bot.permission_map import (
    PERM_PENDING_VIEW,
    PERM_OCR_SEND,
    PERM_WRITEOFF_APPROVE,
    PERM_REQUEST_APPROVE,
)

logger = logging.getLogger(__name__)

router = Router(name="pending_docs_handlers")

_BUTTON_LABEL = "📋 Ожидают отправки"


# ════════════════════════════════════════════════════════
#  Главный экран
# ════════════════════════════════════════════════════════


@router.message(F.text == _BUTTON_LABEL)
@auth_required
async def show_pending_main(message: Message, state: FSMContext) -> None:
    """Главный экран: сводка по всем категориям."""
    await message.delete()
    logger.info("[pending] show_pending_main tg:%d", message.from_user.id)

    tg_id = message.from_user.id
    can_approve_wo = await has_permission(tg_id, PERM_WRITEOFF_APPROVE)
    can_approve_req = await has_permission(tg_id, PERM_REQUEST_APPROVE)
    is_accountant = await has_permission(tg_id, PERM_OCR_SEND)

    snap = await all_uc.get_snapshot(
        tg_id,
        can_approve_wo=can_approve_wo,
        can_approve_req=can_approve_req,
        is_accountant=is_accountant,
    )
    text = all_uc.format_snapshot_text(snap)

    # Кнопки-разделы (только если есть что показывать)
    rows: list[list[InlineKeyboardButton]] = []
    wo_count = len(snap.writeoffs)
    pending_reqs = [r for r in snap.requests if r.get("status") == "pending"]
    req_count = len(pending_reqs)
    inv_count = len(snap.invoices)

    if wo_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📝 Списания ({wo_count})",
                    callback_data="pend_section:writeoffs",
                )
            ]
        )
    if req_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 Заявки ({req_count})",
                    callback_data="pend_section:requests",
                )
            ]
        )
    if inv_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 Накладные ({inv_count})",
                    callback_data="pend_section:invoices",
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════
#  Навигация по разделам
# ════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("pend_section:"))
async def pend_section(callback: CallbackQuery) -> None:
    """Показать список документов одного раздела."""
    await callback.answer()
    section = callback.data.split(":", 1)[1]
    tg_id = callback.from_user.id
    can_approve_wo = await has_permission(tg_id, PERM_WRITEOFF_APPROVE)
    can_approve_req = await has_permission(tg_id, PERM_REQUEST_APPROVE)
    is_accountant = await has_permission(tg_id, PERM_OCR_SEND)

    if section == "writeoffs":
        await _show_writeoffs(callback, tg_id, can_see_all=can_approve_wo)
    elif section == "requests":
        await _show_requests(callback, tg_id, can_see_all=can_approve_req)
    elif section == "invoices":
        await _show_invoices(callback, tg_id, can_see_all=is_accountant)
    else:
        await callback.answer("⚠️ Неизвестный раздел", show_alert=True)


# ─── Списания ────────────────────────────────────────


async def _show_writeoffs(
    callback: CallbackQuery, tg_id: int, *, can_see_all: bool
) -> None:
    all_docs = await wo_uc.all_pending()
    docs = (
        all_docs if can_see_all else [d for d in all_docs if d.author_chat_id == tg_id]
    )

    if not docs:
        await callback.message.delete()
        await callback.message.answer("📝 Ожидающих списаний нет.")
        return

    lines: list[str] = [f"<b>📝 Списания ({len(docs)})</b>\n"]
    for doc in docs:
        lines.append(
            f"🆔 <code>{doc.doc_id}</code>  👤 {doc.author_name}\n"
            f"   🏬 {doc.store_name}  📦 {len(doc.items)} поз."
        )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="pend_back")]
        ]
    )
    await callback.message.delete()
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=back_kb
    )


# ─── Заявки ──────────────────────────────────────────


async def _show_requests(
    callback: CallbackQuery, tg_id: int, *, can_see_all: bool
) -> None:
    if can_see_all:
        reqs = await req_uc.get_pending_requests_full()
    else:
        reqs = await req_uc.get_user_requests(tg_id, limit=50)

    pending = [r for r in reqs if r.get("status") == "pending"]
    if not pending:
        await callback.message.delete()
        await callback.message.answer("📦 Ожидающих заявок нет.")
        return

    lines: list[str] = [f"<b>📦 Заявки ({len(pending)})</b>\n"]
    for req in pending:
        from datetime import datetime as _dt

        created = req.get("created_at")
        if isinstance(created, str):
            try:
                date_str = _dt.fromisoformat(created).strftime("%d.%m %H:%M")
            except ValueError:
                date_str = created
        elif created:
            date_str = created.strftime("%d.%m %H:%M")
        else:
            date_str = "?"
        items = req.get("items", [])
        lines.append(
            f"📋 <b>#{req['pk']}</b>  {date_str}  👤 {req.get('requester_name','?')}\n"
            f"   📦 {len(items)} поз."
        )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="pend_back")]
        ]
    )
    await callback.message.delete()
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=back_kb
    )


# ─── Приходные накладные ─────────────────────────────


async def _show_invoices(
    callback: CallbackQuery,
    tg_id: int,
    *,
    can_see_all: bool,
) -> None:
    if can_see_all:
        infos = await inv_uc.get_all()
    else:
        info = await inv_uc.get_info_for_user(tg_id)
        infos = [info] if info else []

    if not infos:
        await callback.message.delete()
        await callback.message.answer("🧾 Приходных накладных, ожидающих отправки, нет.")
        return

    lines: list[str] = [f"<b>🧾 Приходные накладные ({len(infos)})</b>\n"]
    for info in infos:
        lines.append(inv_uc.format_invoice_info(info))
        lines.append("")  # разделитель

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="pend_back")]
        ]
    )
    await callback.message.delete()
    await callback.message.answer(
        "\n".join(lines).strip(), parse_mode="HTML", reply_markup=back_kb
    )


# ─── Назад ───────────────────────────────────────────


@router.callback_query(F.data == "pend_back")
async def pend_back(callback: CallbackQuery) -> None:
    """Вернуться на главный экран сводки."""
    await callback.answer()
    tg_id = callback.from_user.id
    can_approve_wo = await has_permission(tg_id, PERM_WRITEOFF_APPROVE)
    can_approve_req = await has_permission(tg_id, PERM_REQUEST_APPROVE)
    is_accountant = await has_permission(tg_id, PERM_OCR_SEND)

    snap = await all_uc.get_snapshot(
        tg_id,
        can_approve_wo=can_approve_wo,
        can_approve_req=can_approve_req,
        is_accountant=is_accountant,
    )
    text = all_uc.format_snapshot_text(snap)

    wo_count = len(snap.writeoffs)
    pending_reqs = [r for r in snap.requests if r.get("status") == "pending"]
    req_count = len(pending_reqs)
    inv_count = len(snap.invoices)

    rows: list[list[InlineKeyboardButton]] = []
    if wo_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📝 Списания ({wo_count})",
                    callback_data="pend_section:writeoffs",
                )
            ]
        )
    if req_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 Заявки ({req_count})",
                    callback_data="pend_section:requests",
                )
            ]
        )
    if inv_count:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 Накладные ({inv_count})",
                    callback_data="pend_section:invoices",
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await callback.message.delete()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
