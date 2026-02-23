"""
Унифицированный просмотр всех неотправленных документов.

Агрегирует данные из:
  • pending_writeoff      — акты списания (ожидают одобрения админа)
  • product_request       — заявки со статусом «pending»
  • pending_incoming_invoice — приходные накладные (OCR/JSON-чеки)

Используется обработчиком «📋 Ожидают отправки».
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from use_cases import pending_incoming_invoice as inv_uc
from use_cases import pending_writeoffs as wo_uc
from use_cases import product_request as req_uc

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
#  DTO
# ════════════════════════════════════════════════════════


@dataclass
class PendingSnapshot:
    """Снимок всех ожидающих документов для одного пользователя / всей системы."""

    writeoffs: list  # list[PendingWriteoff]
    requests: list  # list[dict] from get_pending_requests_full
    invoices: list  # list[PendingInvoiceInfo]


# ════════════════════════════════════════════════════════
#  Query
# ════════════════════════════════════════════════════════


async def get_snapshot(
    tg_id: int,
    *,
    can_approve_wo: bool = False,
    can_approve_req: bool = False,
    is_accountant: bool = False,
) -> PendingSnapshot:
    """
    Получить все ожидающие документы.

    Режимы:
      • can_approve_wo=True   → видит все списания (PERM_WRITEOFF_APPROVE)
      • can_approve_req=True  → видит все заявки (PERM_REQUEST_APPROVE)
      • is_accountant=True    → видит все накладные (PERM_OCR_SEND)
      • иначе — только свои
    """
    wo_task = asyncio.create_task(_get_writeoffs(tg_id, see_all=can_approve_wo))
    req_task = asyncio.create_task(_get_requests(tg_id, see_all=can_approve_req))
    inv_task = asyncio.create_task(_get_invoices(tg_id, see_all=is_accountant))

    writeoffs, requests, invoices = await asyncio.gather(
        wo_task, req_task, inv_task, return_exceptions=True
    )
    if isinstance(writeoffs, Exception):
        logger.exception("[pending_all] ошибка при загрузке списаний: %s", writeoffs)
        writeoffs = []
    if isinstance(requests, Exception):
        logger.exception("[pending_all] ошибка при загрузке заявок: %s", requests)
        requests = []
    if isinstance(invoices, Exception):
        logger.exception("[pending_all] ошибка при загрузке накладных: %s", invoices)
        invoices = []

    return PendingSnapshot(
        writeoffs=writeoffs,
        requests=requests,
        invoices=invoices,
    )


# ════════════════════════════════════════════════════════
#  Вспомогательные запросы
# ════════════════════════════════════════════════════════


async def _get_writeoffs(tg_id: int, *, see_all: bool) -> list:
    all_docs = await wo_uc.all_pending()
    if see_all:
        return all_docs
    return [d for d in all_docs if d.author_chat_id == tg_id]


async def _get_requests(tg_id: int, *, see_all: bool) -> list[dict]:
    if see_all:
        return await req_uc.get_pending_requests_full()
    return await req_uc.get_user_requests(tg_id, limit=50)


async def _get_invoices(tg_id: int, *, see_all: bool) -> list:
    if see_all:
        return await inv_uc.get_all()
    info = await inv_uc.get_info_for_user(tg_id)
    return [info] if info else []


# ════════════════════════════════════════════════════════
#  Форматирование
# ════════════════════════════════════════════════════════


def format_snapshot_text(snap: PendingSnapshot) -> str:
    """Краткое текстовое сводное сообщение о количестве ожидающих документов."""
    lines: list[str] = ["<b>📋 Ожидают отправки в iiko</b>\n"]

    # Списания
    wo_count = len(snap.writeoffs)
    if wo_count:
        lines.append(f"📝 <b>Списания:</b> {wo_count}")
    else:
        lines.append("📝 Списания: нет")

    # Заявки (только pending)
    pending_reqs = [r for r in snap.requests if r.get("status") == "pending"]
    req_count = len(pending_reqs)
    if req_count:
        lines.append(f"📦 <b>Заявки:</b> {req_count}")
    else:
        lines.append("📦 Заявки: нет")

    # Приходные накладные
    inv_count = len(snap.invoices)
    if inv_count:
        lines.append(f"🧾 <b>Приходные накладные:</b> {inv_count}")
    else:
        lines.append("🧾 Приходные накладные: нет")

    total = wo_count + req_count + inv_count
    if total == 0:
        lines.append("\n✅ Нет ожидающих документов")
    else:
        lines.append(f"\nВсего: <b>{total}</b>")

    return "\n".join(lines)
