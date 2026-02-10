"""
Use-case: управление администраторами бота.

Флоу:
  1. get_employees_with_telegram() — список сотрудников у которых есть telegram_id
  2. add_admin(telegram_id, employee_id, employee_name, added_by) — добавить в bot_admin
  3. remove_admin(telegram_id) — удалить из bot_admin
  4. list_admins() — текущие админы
  5. get_admin_ids() — list[int] telegram_id всех админов (для быстрого доступа)
  6. is_admin(telegram_id) — проверка
"""

import logging
import time
from uuid import UUID

from sqlalchemy import select, delete

from db.engine import async_session_factory
from db.models import Employee, BotAdmin

logger = logging.getLogger(__name__)

# ─── In-memory кеш admin_ids (перезагружается при add/remove) ───
_admin_ids_cache: list[int] | None = None


async def get_admin_ids() -> list[int]:
    """
    Список telegram_id всех админов из БД.
    Кешируется в памяти; инвалидируется при add/remove.
    """
    global _admin_ids_cache
    if _admin_ids_cache is not None:
        return _admin_ids_cache

    async with async_session_factory() as session:
        stmt = select(BotAdmin.telegram_id)
        result = await session.execute(stmt)
        ids = [row[0] for row in result.all()]

    _admin_ids_cache = ids
    logger.info("[admin] Загружено %d админов из БД", len(ids))
    return ids


def _invalidate_cache() -> None:
    global _admin_ids_cache
    _admin_ids_cache = None


async def is_admin(telegram_id: int) -> bool:
    """Проверить, является ли пользователь админом."""
    ids = await get_admin_ids()
    return telegram_id in ids


async def get_employees_with_telegram() -> list[dict]:
    """
    Все сотрудники у которых есть telegram_id (авторизованы в боте).
    Возвращает: [{id, name, last_name, first_name, telegram_id}, ...]
    """
    t0 = time.monotonic()
    async with async_session_factory() as session:
        stmt = (
            select(Employee)
            .where(Employee.telegram_id.isnot(None))
            .where(Employee.deleted == False)
            .order_by(Employee.last_name, Employee.first_name)
        )
        result = await session.execute(stmt)
        employees = result.scalars().all()

    items = [
        {
            "id": str(emp.id),
            "name": emp.name or f"{emp.last_name} {emp.first_name}",
            "last_name": emp.last_name or "",
            "first_name": emp.first_name or "",
            "telegram_id": emp.telegram_id,
        }
        for emp in employees
    ]
    logger.info("[admin] Сотрудников с telegram_id: %d (%.2f сек)",
                len(items), time.monotonic() - t0)
    return items


async def list_admins() -> list[dict]:
    """
    Текущие администраторы.
    Возвращает: [{telegram_id, employee_name, added_at}, ...]
    """
    async with async_session_factory() as session:
        stmt = select(BotAdmin).order_by(BotAdmin.added_at)
        result = await session.execute(stmt)
        admins = result.scalars().all()

    return [
        {
            "telegram_id": a.telegram_id,
            "employee_id": str(a.employee_id),
            "employee_name": a.employee_name or "—",
            "added_at": a.added_at.strftime("%d.%m.%Y %H:%M") if a.added_at else "—",
        }
        for a in admins
    ]


async def add_admin(
    telegram_id: int,
    employee_id: str,
    employee_name: str,
    added_by: int | None = None,
) -> bool:
    """
    Добавить администратора. Возвращает True если добавлен, False если уже есть.
    """
    async with async_session_factory() as session:
        # Проверяем, нет ли уже
        exists = await session.execute(
            select(BotAdmin).where(BotAdmin.telegram_id == telegram_id)
        )
        if exists.scalar_one_or_none():
            logger.info("[admin] telegram_id=%d уже админ", telegram_id)
            return False

        admin = BotAdmin(
            telegram_id=telegram_id,
            employee_id=UUID(employee_id),
            employee_name=employee_name,
            added_by=added_by,
        )
        session.add(admin)
        await session.commit()

    _invalidate_cache()
    logger.info("[admin] ✅ Добавлен админ: %s (tg:%d), добавил tg:%s",
                employee_name, telegram_id, added_by)
    return True


async def remove_admin(telegram_id: int) -> bool:
    """Удалить администратора. Возвращает True если удалён, False если не был."""
    async with async_session_factory() as session:
        stmt = delete(BotAdmin).where(BotAdmin.telegram_id == telegram_id)
        result = await session.execute(stmt)
        await session.commit()
        removed = result.rowcount > 0

    if removed:
        _invalidate_cache()
        logger.info("[admin] ❌ Удалён админ tg:%d", telegram_id)
    return removed
