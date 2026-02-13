"""
Use-case: заявки на товары (product requests).

Флоу:
  1. Создатель (точка) выбирает склад → поставщика → вводит количества
  2. Заявка сохраняется в БД (status=pending), уведомление → получателям
  3. Получатель видит заявку, нажимает «Отправить» →
     создаётся расходная накладная в iiko (через outgoing_invoice)

Получатели определяются через Google Таблицу (столбец «📬 Получатель»
на листе «Права доступа»).
"""

import logging
import time
from datetime import datetime, timezone
from use_cases._helpers import now_kgd
from uuid import UUID

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.models import ProductRequest

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Получатели заявок — делегируем в permissions (GSheet)
# ═══════════════════════════════════════════════════════

async def get_receiver_ids() -> list[int]:
    """Список telegram_id всех получателей заявок (из GSheet кеша)."""
    from use_cases import permissions as perm_uc
    return await perm_uc.get_receiver_ids()


async def is_receiver(telegram_id: int) -> bool:
    """Проверить, является ли пользователь получателем заявок (из GSheet кеша)."""
    from use_cases import permissions as perm_uc
    return await perm_uc.is_receiver(telegram_id)


# ═══════════════════════════════════════════════════════
# Создание / получение заявок
# ═══════════════════════════════════════════════════════

async def create_request(
    *,
    requester_tg: int,
    requester_name: str,
    department_id: str,
    department_name: str,
    store_id: str,
    store_name: str,
    counteragent_id: str,
    counteragent_name: str,
    account_id: str,
    account_name: str,
    items: list[dict],
    total_sum: float,
    comment: str = "",
) -> int:
    """Создать заявку (status=pending). Возвращает pk."""
    t0 = time.monotonic()
    async with async_session_factory() as session:
        req = ProductRequest(
            status="pending",
            requester_tg=requester_tg,
            requester_name=requester_name,
            department_id=UUID(department_id),
            department_name=department_name,
            store_id=UUID(store_id),
            store_name=store_name,
            counteragent_id=UUID(counteragent_id),
            counteragent_name=counteragent_name,
            account_id=UUID(account_id),
            account_name=account_name,
            items=items,
            total_sum=total_sum,
            comment=comment,
        )
        session.add(req)
        await session.commit()
        pk = req.pk

    logger.info(
        "[request] ✅ Заявка pk=%d создана: tg:%d, dept=%s, store=%s, items=%d, sum=%.2f (%.2f сек)",
        pk, requester_tg, department_name, store_name,
        len(items), total_sum, time.monotonic() - t0,
    )
    return pk


async def get_request_by_pk(pk: int) -> dict | None:
    """Получить заявку по pk."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()

    if not r:
        return None

    return {
        "pk": r.pk,
        "status": r.status,
        "requester_tg": r.requester_tg,
        "requester_name": r.requester_name,
        "department_id": str(r.department_id),
        "department_name": r.department_name,
        "store_id": str(r.store_id),
        "store_name": r.store_name,
        "counteragent_id": str(r.counteragent_id),
        "counteragent_name": r.counteragent_name,
        "account_id": str(r.account_id),
        "account_name": r.account_name,
        "items": r.items or [],
        "total_sum": float(r.total_sum) if r.total_sum else 0.0,
        "comment": r.comment,
        "approved_by": r.approved_by,
        "created_at": r.created_at,
        "approved_at": r.approved_at,
    }


async def get_pending_requests() -> list[dict]:
    """Все заявки со статусом pending."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.status == "pending")
            .order_by(ProductRequest.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "requester_name": r.requester_name,
            "department_name": r.department_name,
            "store_name": r.store_name,
            "counteragent_name": r.counteragent_name,
            "items_count": len(r.items) if r.items else 0,
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def get_pending_requests_full() -> list[dict]:
    """Все заявки pending с полными данными (без N+1)."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.status == "pending")
            .order_by(ProductRequest.created_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "status": r.status,
            "requester_tg": r.requester_tg,
            "requester_name": r.requester_name,
            "department_id": str(r.department_id),
            "department_name": r.department_name,
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "counteragent_id": str(r.counteragent_id),
            "counteragent_name": r.counteragent_name,
            "account_id": str(r.account_id),
            "account_name": r.account_name,
            "items": r.items or [],
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "comment": r.comment,
            "approved_by": r.approved_by,
            "created_at": r.created_at,
            "approved_at": r.approved_at,
        }
        for r in rows
    ]


async def get_user_requests(telegram_id: int, limit: int = 10) -> list[dict]:
    """Последние заявки пользователя (approved/pending/cancelled), для истории."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.requester_tg == telegram_id)
            .order_by(ProductRequest.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "status": r.status,
            "requester_name": r.requester_name,
            "department_id": str(r.department_id),
            "department_name": r.department_name,
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "counteragent_id": str(r.counteragent_id),
            "counteragent_name": r.counteragent_name,
            "account_id": str(r.account_id),
            "account_name": r.account_name,
            "items": r.items or [],
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def approve_request(pk: int, approved_by: int) -> bool:
    """Пометить заявку как approved."""
    now = now_kgd()
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.status = "approved"
        r.approved_by = approved_by
        r.approved_at = now
        await session.commit()

    logger.info("[request] ✅ Заявка pk=%d approved by tg:%d", pk, approved_by)
    return True


async def cancel_request(pk: int, cancelled_by: int) -> bool:
    """Отменить заявку."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.status = "cancelled"
        r.approved_by = cancelled_by
        await session.commit()

    logger.info("[request] ❌ Заявка pk=%d cancelled by tg:%d", pk, cancelled_by)
    return True


async def update_request_items(pk: int, items: list[dict], total_sum: float) -> bool:
    """Обновить позиции заявки (при редактировании получателем)."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.items = items
        r.total_sum = total_sum
        await session.commit()

    logger.info("[request] ✏️ Заявка pk=%d items обновлены (%d поз., sum=%.2f)", pk, len(items), total_sum)
    return True


def format_request_text(req: dict) -> str:
    """HTML-текст заявки для отображения."""
    items = req.get("items", [])
    created = req.get("created_at")
    date_str = created.strftime("%d.%m.%Y %H:%M") if created else "?"

    text = (
        f"📝 <b>Заявка #{req['pk']}</b>\n"
        f"📅 {date_str}\n"
        f"👤 {req.get('requester_name', '?')}\n"
        f"🏨 {req.get('department_name', '?')}\n"
        f"🏬 {req.get('store_name', '?')}\n"
        f"🏢 {req.get('counteragent_name', '?')}\n\n"
        f"<b>Позиции ({len(items)}):</b>\n"
    )
    for i, item in enumerate(items, 1):
        name = item.get("name", "?")
        amount = item.get("amount", 0)
        price = item.get("price", 0)
        unit = item.get("unit_name", "шт")
        line_sum = round(amount * price, 2)
        text += f"  {i}. {name} × {amount:.4g} {unit}"
        if price:
            text += f" × {price:.2f}₽ = {line_sum:.2f}₽"
        text += "\n"

    total = req.get("total_sum", 0)
    text += f"\n<b>Итого: {total:.2f}₽</b>"

    if req.get("comment"):
        text += f"\n💬 {req['comment']}"

    status_map = {"pending": "⏳ Ожидает", "approved": "✅ Отправлена", "cancelled": "❌ Отменена"}
    text += f"\n\n<b>Статус:</b> {status_map.get(req.get('status', ''), req.get('status', ''))}"
    return text
