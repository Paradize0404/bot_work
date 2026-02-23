"""
CRUD для pending_incoming_invoice.

Таблица хранит приходные накладные (OCR-фото / JSON-чеки), ожидающие
подтверждения отправки в iiko. Заменяет in-memory _pending_invoices в
bot/document_handlers.py → данные переживают рестарт бота.

Одна запись на tg_id: при повторной загрузке старая перезаписывается.
Удаляется при «📤 Отправить в iiko» или «❌ Отменить».
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.models import PendingIncomingInvoice
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
#  DTO
# ════════════════════════════════════════════════════════


@dataclass
class PendingInvoiceInfo:
    """Метаданные pending-пачки (без тяжёлого invoices_json)."""

    pk: int
    tg_id: int
    source_type: str  # 'ocr' или 'json_receipt'
    department_id: str | None
    department_name: str | None
    author_name: str | None
    invoices_count: int
    items_count: int
    created_at: datetime


# ════════════════════════════════════════════════════════
#  CRUD
# ════════════════════════════════════════════════════════


async def save(
    tg_id: int,
    source_type: str,
    invoices: list[dict],
    *,
    department_id: str | None = None,
    department_name: str | None = None,
    author_name: str | None = None,
) -> int:
    """Сохранить pending накладные для пользователя. Перезаписывает старые."""
    total_items = sum(len(inv.get("items", [])) for inv in invoices)
    async with async_session_factory() as session:
        # DELETE старой записи (если есть) — unique по tg_id
        await session.execute(
            delete(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        row = PendingIncomingInvoice(
            tg_id=tg_id,
            source_type=source_type,
            department_id=department_id,
            department_name=department_name,
            author_name=author_name,
            invoices_count=len(invoices),
            items_count=total_items,
            invoices_json=invoices,
            created_at=now_kgd(),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            "[pending_invoice] Сохранено tg:%d source=%s invoices=%d items=%d",
            tg_id,
            source_type,
            len(invoices),
            total_items,
        )
        return row.pk


async def get(tg_id: int) -> list[dict] | None:
    """Получить список invoice dict для пользователя (без удаления)."""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        return row.invoices_json if row else None


async def pop(tg_id: int) -> list[dict] | None:
    """Получить и удалить pending накладные пользователя (атомарно)."""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        if row is None:
            return None
        invoices = row.invoices_json
        await session.execute(
            delete(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        await session.commit()
        logger.info("[pending_invoice] Удалено (pop) tg:%d", tg_id)
        return invoices


async def delete_by_tg(tg_id: int) -> None:
    """Удалить pending накладные пользователя."""
    async with async_session_factory() as session:
        await session.execute(
            delete(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        await session.commit()


async def update_invoices(tg_id: int, invoices: list[dict]) -> None:
    """Обновить invoices_json и счётчики для записи пользователя."""
    from sqlalchemy import update as sa_update

    total_items = sum(len(inv.get("items", [])) for inv in invoices)
    async with async_session_factory() as session:
        await session.execute(
            sa_update(PendingIncomingInvoice)
            .where(PendingIncomingInvoice.tg_id == tg_id)
            .values(
                invoices_json=invoices,
                invoices_count=len(invoices),
                items_count=total_items,
            )
        )
        await session.commit()
    logger.info(
        "[pending_invoice] invoices_json обновлён tg:%d invoices=%d items=%d",
        tg_id,
        len(invoices),
        total_items,
    )


async def update_summary_msg_ids(
    owner_tg_id: int, viewer_tg_id: int, msg_id: int
) -> None:
    """Сохранить ID сообщения с кнопками для конкретного зрителя."""
    from sqlalchemy import update as sa_update

    async with async_session_factory() as session:
        row = await session.scalar(
            select(PendingIncomingInvoice).where(
                PendingIncomingInvoice.tg_id == owner_tg_id
            )
        )
        if row is None:
            return
        current = dict(row.summary_msg_ids or {})
        current[str(viewer_tg_id)] = msg_id
        await session.execute(
            sa_update(PendingIncomingInvoice)
            .where(PendingIncomingInvoice.tg_id == owner_tg_id)
            .values(summary_msg_ids=current)
        )
        await session.commit()


async def get_summary_msg_ids(owner_tg_id: int) -> dict[int, int]:
    """Вернуть {viewer_tg_id: message_id} для pending накладной."""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(PendingIncomingInvoice).where(
                PendingIncomingInvoice.tg_id == owner_tg_id
            )
        )
        if row is None or not row.summary_msg_ids:
            return {}
        return {int(k): v for k, v in row.summary_msg_ids.items()}


async def get_all() -> list[PendingInvoiceInfo]:
    """Все pending накладные (для просмотра администратором / бухгалтером)."""
    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(PendingIncomingInvoice).order_by(
                    PendingIncomingInvoice.created_at
                )
            )
        ).all()
        return [_to_info(r) for r in rows]


async def get_info_for_user(tg_id: int) -> PendingInvoiceInfo | None:
    """Метаданные pending-пачки пользователя (без тяжёлого invoices_json)."""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(PendingIncomingInvoice).where(PendingIncomingInvoice.tg_id == tg_id)
        )
        return _to_info(row) if row else None


# ════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════


def _to_info(row: PendingIncomingInvoice) -> PendingInvoiceInfo:
    return PendingInvoiceInfo(
        pk=row.pk,
        tg_id=row.tg_id,
        source_type=row.source_type,
        department_id=row.department_id,
        department_name=row.department_name,
        author_name=row.author_name,
        invoices_count=row.invoices_count,
        items_count=row.items_count,
        created_at=row.created_at,
    )


def format_invoice_info(info: PendingInvoiceInfo) -> str:
    """Краткое текстовое описание для списка ожидающих."""
    source_label = {
        "ocr": "📷 OCR-фото",
        "json_receipt": "🧾 JSON-чек",
    }.get(info.source_type, info.source_type)
    dept = info.department_name or "—"
    lines = [
        f"{source_label} · <b>{dept}</b>",
        f"👤 {info.author_name or '—'}",
        f"📦 {info.invoices_count} накл. · {info.items_count} поз.",
        f"🕐 {info.created_at.strftime('%d.%m %H:%M') if info.created_at else '—'}",
    ]
    return "\n".join(lines)
