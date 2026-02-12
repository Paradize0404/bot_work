"""
Use-case: заявки на товары (product requests).

Флоу:
  1. Создатель (точка) выбирает склад → поставщика → вводит количества
  2. Заявка сохраняется в БД (status=pending), уведомление → получателям
  3. Получатель видит заявку, нажимает «Отправить» →
     создаётся расходная накладная в iiko (через outgoing_invoice)

Управление получателями:
  - add_receiver / remove_receiver / list_receivers / is_receiver
  - Аналогично admin.py (кеш + БД)
"""

import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, delete, func

from db.engine import async_session_factory
from db.models import Employee, RequestReceiver, ProductRequest

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Кеш receiver_ids (аналог admin_ids)
# ═══════════════════════════════════════════════════════

_receiver_ids_cache: list[int] | None = None


async def get_receiver_ids() -> list[int]:
    """Список telegram_id всех получателей заявок. Кешируется."""
    global _receiver_ids_cache
    if _receiver_ids_cache is not None:
        return _receiver_ids_cache

    async with async_session_factory() as session:
        stmt = select(RequestReceiver.telegram_id)
        result = await session.execute(stmt)
        ids = [row[0] for row in result.all()]

    _receiver_ids_cache = ids
    logger.info("[request] Загружено %d получателей заявок из БД", len(ids))
    return ids


def _invalidate_cache() -> None:
    global _receiver_ids_cache
    _receiver_ids_cache = None


async def is_receiver(telegram_id: int) -> bool:
    """Проверить, является ли пользователь получателем заявок."""
    ids = await get_receiver_ids()
    return telegram_id in ids


# ═══════════════════════════════════════════════════════
# CRUD получателей
# ═══════════════════════════════════════════════════════

async def get_employees_with_telegram() -> list[dict]:
    """Все сотрудники с telegram_id (авторизованные)."""
    async with async_session_factory() as session:
        stmt = (
            select(Employee)
            .where(Employee.telegram_id.isnot(None))
            .where(Employee.deleted == False)  # noqa: E712
            .order_by(Employee.last_name, Employee.first_name)
        )
        result = await session.execute(stmt)
        employees = result.scalars().all()

    return [
        {
            "id": str(emp.id),
            "name": emp.name or f"{emp.last_name} {emp.first_name}",
            "last_name": emp.last_name or "",
            "first_name": emp.first_name or "",
            "telegram_id": emp.telegram_id,
        }
        for emp in employees
    ]


async def list_receivers() -> list[dict]:
    """Текущие получатели заявок."""
    async with async_session_factory() as session:
        stmt = select(RequestReceiver).order_by(RequestReceiver.added_at)
        result = await session.execute(stmt)
        receivers = result.scalars().all()

    return [
        {
            "telegram_id": r.telegram_id,
            "employee_id": str(r.employee_id),
            "employee_name": r.employee_name or "—",
            "added_at": r.added_at.strftime("%d.%m.%Y %H:%M") if r.added_at else "—",
        }
        for r in receivers
    ]


async def add_receiver(
    telegram_id: int,
    employee_id: str,
    employee_name: str,
    added_by: int | None = None,
) -> bool:
    """Добавить получателя. True = добавлен, False = уже есть."""
    async with async_session_factory() as session:
        exists = await session.execute(
            select(RequestReceiver).where(RequestReceiver.telegram_id == telegram_id)
        )
        if exists.scalar_one_or_none():
            logger.info("[request] tg:%d уже получатель", telegram_id)
            return False

        rec = RequestReceiver(
            telegram_id=telegram_id,
            employee_id=UUID(employee_id),
            employee_name=employee_name,
            added_by=added_by,
        )
        session.add(rec)
        await session.commit()

    _invalidate_cache()
    logger.info("[request] ✅ Добавлен получатель: %s (tg:%d), добавил tg:%s",
                employee_name, telegram_id, added_by)
    return True


async def remove_receiver(telegram_id: int) -> bool:
    """Удалить получателя. True = удалён, False = не был."""
    async with async_session_factory() as session:
        stmt = delete(RequestReceiver).where(RequestReceiver.telegram_id == telegram_id)
        result = await session.execute(stmt)
        await session.commit()
        removed = result.rowcount > 0

    if removed:
        _invalidate_cache()
        logger.info("[request] ❌ Удалён получатель tg:%d", telegram_id)
    return removed


async def get_available_for_receiver() -> list[dict]:
    """Сотрудники с telegram_id, которые ещё НЕ являются получателями."""
    employees = await get_employees_with_telegram()
    receiver_ids = await get_receiver_ids()
    return [e for e in employees if e["telegram_id"] not in receiver_ids]


async def format_receiver_list() -> str:
    """HTML-текст со списком получателей заявок."""
    receivers = await list_receivers()
    if not receivers:
        return "📬 <b>Получатели заявок</b>\n\n<i>Список пуст.</i>"

    lines = [f"📬 <b>Получатели заявок ({len(receivers)})</b>\n"]
    for i, r in enumerate(receivers, 1):
        lines.append(
            f"  {i}. {r['employee_name']}  "
            f"<code>tg:{r['telegram_id']}</code>  ({r['added_at']})"
        )
    return "\n".join(lines)


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


async def approve_request(pk: int, approved_by: int) -> bool:
    """Пометить заявку как approved."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
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
