"""
Use-case: управление администраторами бота.

Админы определяются через Google Таблицу (столбец «👑 Админ» на листе «Права доступа»).
Таблица bot_admin в БД сохраняется как fallback но НЕ используется для проверки is_admin.

Функции:
  1. get_employees_with_telegram() — список сотрудников у которых есть telegram_id
  2. is_admin(telegram_id) — проверка (из GSheet кеша)
  3. get_admin_ids() — list[int] telegram_id всех админов (из GSheet кеша)
  4. format_admin_list() — HTML-текст со списком текущих админов
"""

import logging
import time

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Employee

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Проверка админов — делегируем в permissions (GSheet)
# ═══════════════════════════════════════════════════════

async def get_admin_ids() -> list[int]:
    """Список telegram_id всех админов (из GSheet кеша)."""
    from use_cases import permissions as perm_uc
    return await perm_uc.get_admin_ids()


async def alert_admins(bot, message: str) -> None:
    """Отправить алерт всем админам (или сисадминам). Fire-and-forget."""
    from use_cases import permissions as perm_uc
    
    # Пытаемся найти сисадминов
    sys_admins = await perm_uc.get_users_with_role("🔧 Сис.Админ")
    
    # Если сисадминов нет, шлем всем обычным админам
    if not sys_admins:
        sys_admins = await perm_uc.get_admin_ids()
        
    for admin_id in sys_admins:
        try:
            await bot.send_message(admin_id, f"🚨 ALERT\n\n{message[:4000]}")
        except Exception as e:
            logger.warning("[alert_admins] Failed to send alert to %s: %s", admin_id, e)


async def is_admin(telegram_id: int) -> bool:
    """Проверить, является ли пользователь админом (из GSheet кеша)."""
    from use_cases import permissions as perm_uc
    return await perm_uc.is_admin(telegram_id)


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
