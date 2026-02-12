"""
Use-case: история списаний.

Позволяет:
  1. Сохранить одобренный акт списания в историю
  2. Получить историю для пользователя (с фильтрацией по роли)
  3. Загрузить конкретную запись для повторного использования

Фильтрация по ролям:
  - bar      → только записи с store_type='bar'
  - kitchen  → только записи с store_type='kitchen'
  - admin    → все записи по подразделению
  - unknown  → все записи по подразделению (ручной выбор)
"""

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, func, desc

from db.engine import async_session_factory
from db.models import WriteoffHistory

logger = logging.getLogger(__name__)

# Максимум записей в истории на пользователя
MAX_HISTORY_PER_USER = 200
# Сколько показывать в списке (последние N)
HISTORY_PAGE_SIZE = 10


def _detect_store_type(store_name: str) -> str | None:
    """Определить тип склада по имени: 'bar', 'kitchen' или None."""
    low = (store_name or "").lower()
    if "бар" in low:
        return "bar"
    if "кухня" in low or "кухн" in low:
        return "kitchen"
    return None


@dataclass(slots=True)
class HistoryEntry:
    """Одна запись из истории списаний."""
    pk: int
    employee_name: str
    store_id: str
    store_name: str
    account_id: str
    account_name: str
    reason: str
    items: list[dict]
    store_type: str | None
    created_at: str  # ISO-формат для отображения
    approved_by_name: str | None


async def save_to_history(
    telegram_id: int,
    employee_name: str,
    department_id: str,
    store_id: str,
    store_name: str,
    account_id: str,
    account_name: str,
    reason: str,
    items: list[dict],
    approved_by_name: str | None = None,
) -> int:
    """
    Сохранить одобренный акт в историю.

    Возвращает pk созданной записи.
    Если у пользователя больше MAX_HISTORY_PER_USER записей,
    удаляет самые старые.
    """
    t0 = time.monotonic()
    store_type = _detect_store_type(store_name)

    # Очищаем items от лишних полей (product_cache и т.п.)
    clean_items = []
    for item in items:
        clean_items.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "quantity": item.get("quantity"),
            "user_quantity": item.get("user_quantity"),
            "unit_label": item.get("unit_label", "шт"),
            "main_unit": item.get("main_unit"),
        })

    async with async_session_factory() as session:
        record = WriteoffHistory(
            telegram_id=telegram_id,
            employee_name=employee_name,
            department_id=UUID(department_id),
            store_id=UUID(store_id),
            store_name=store_name,
            account_id=UUID(account_id),
            account_name=account_name,
            reason=reason,
            items=clean_items,
            store_type=store_type,
            approved_by_name=approved_by_name,
        )
        session.add(record)
        await session.flush()
        pk = record.pk
        await session.commit()

    # Cleanup: удаляем старые записи сверх лимита (фон)
    try:
        await _cleanup_old_records(telegram_id)
    except Exception:
        logger.warning("[wo_history] Ошибка очистки старых записей tg:%d", telegram_id, exc_info=True)

    logger.info(
        "[wo_history] Сохранено: tg:%d, store=%s, items=%d, pk=%d (%.2f сек)",
        telegram_id, store_name, len(clean_items), pk, time.monotonic() - t0,
    )
    return pk


async def get_history(
    telegram_id: int,
    department_id: str,
    role_type: str,
    page: int = 0,
) -> tuple[list[HistoryEntry], int]:
    """
    Получить историю списаний для пользователя.

    Фильтрация:
      - role_type='bar'     → только store_type='bar'
      - role_type='kitchen' → только store_type='kitchen'
      - role_type='admin'   → все записи по department_id
      - role_type='unknown' → все записи по department_id

    Возвращает: (список записей, общее количество).
    """
    t0 = time.monotonic()

    async with async_session_factory() as session:
        # Базовый фильтр: по подразделению
        base_filter = WriteoffHistory.department_id == UUID(department_id)

        # Дополнительный фильтр по роли
        if role_type == "bar":
            role_filter = WriteoffHistory.store_type == "bar"
        elif role_type == "kitchen":
            role_filter = WriteoffHistory.store_type == "kitchen"
        else:
            # admin или unknown — показываем всё по подразделению
            role_filter = None

        # Подсчёт
        count_stmt = select(func.count(WriteoffHistory.pk)).where(base_filter)
        if role_filter is not None:
            count_stmt = count_stmt.where(role_filter)
        total = (await session.execute(count_stmt)).scalar_one()

        # Выборка страницы
        stmt = (
            select(WriteoffHistory)
            .where(base_filter)
        )
        if role_filter is not None:
            stmt = stmt.where(role_filter)
        stmt = (
            stmt
            .order_by(desc(WriteoffHistory.created_at))
            .offset(page * HISTORY_PAGE_SIZE)
            .limit(HISTORY_PAGE_SIZE)
        )

        result = await session.execute(stmt)
        records = result.scalars().all()

    entries = [
        HistoryEntry(
            pk=r.pk,
            employee_name=r.employee_name or "—",
            store_id=str(r.store_id),
            store_name=r.store_name or "—",
            account_id=str(r.account_id),
            account_name=r.account_name or "—",
            reason=r.reason or "—",
            items=r.items or [],
            store_type=r.store_type,
            created_at=r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "—",
            approved_by_name=r.approved_by_name,
        )
        for r in records
    ]

    logger.info(
        "[wo_history] История: tg:%d, dept=%s, role=%s, page=%d → %d/%d (%.2f сек)",
        telegram_id, department_id, role_type, page, len(entries), total, time.monotonic() - t0,
    )
    return entries, total


async def get_history_entry(pk: int) -> HistoryEntry | None:
    """Получить одну запись из истории по pk."""
    async with async_session_factory() as session:
        stmt = select(WriteoffHistory).where(WriteoffHistory.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()

    if not r:
        return None

    return HistoryEntry(
        pk=r.pk,
        employee_name=r.employee_name or "—",
        store_id=str(r.store_id),
        store_name=r.store_name or "—",
        account_id=str(r.account_id),
        account_name=r.account_name or "—",
        reason=r.reason or "—",
        items=r.items or [],
        store_type=r.store_type,
        created_at=r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "—",
        approved_by_name=r.approved_by_name,
    )


def build_history_summary(entry: HistoryEntry) -> str:
    """Текст summary для отображения записи из истории."""
    text = (
        f"📋 <b>Из истории</b> ({entry.created_at})\n"
        f"👤 <b>Автор:</b> {entry.employee_name}\n"
        f"🏬 <b>Склад:</b> {entry.store_name}\n"
        f"📂 <b>Счёт:</b> {entry.account_name}\n"
        f"📝 <b>Причина:</b> {entry.reason}\n"
    )
    if entry.items:
        text += "\n<b>Позиции:</b>"
        for i, item in enumerate(entry.items, 1):
            uq = item.get("user_quantity", item.get("quantity", 0))
            unit_label = item.get("unit_label", "шт")
            text += f"\n  {i}. {item.get('name', '?')} — {uq} {unit_label}"
    return text


async def _cleanup_old_records(telegram_id: int) -> None:
    """Удалить самые старые записи сверх лимита MAX_HISTORY_PER_USER."""
    async with async_session_factory() as session:
        count_stmt = (
            select(func.count(WriteoffHistory.pk))
            .where(WriteoffHistory.telegram_id == telegram_id)
        )
        total = (await session.execute(count_stmt)).scalar_one()

        if total <= MAX_HISTORY_PER_USER:
            return

        # Находим pk записей, которые нужно оставить (последние N)
        keep_stmt = (
            select(WriteoffHistory.pk)
            .where(WriteoffHistory.telegram_id == telegram_id)
            .order_by(desc(WriteoffHistory.created_at))
            .limit(MAX_HISTORY_PER_USER)
        )
        keep_result = await session.execute(keep_stmt)
        keep_pks = {row[0] for row in keep_result.all()}

        # Удаляем остальные
        from sqlalchemy import delete
        del_stmt = (
            delete(WriteoffHistory)
            .where(WriteoffHistory.telegram_id == telegram_id)
            .where(WriteoffHistory.pk.notin_(keep_pks))
        )
        result = await session.execute(del_stmt)
        await session.commit()
        logger.info(
            "[wo_history] Очистка: tg:%d, удалено %d старых записей",
            telegram_id, result.rowcount,
        )
