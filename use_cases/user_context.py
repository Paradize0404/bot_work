"""
In-memory кеш контекста авторизованного пользователя.

Хранит {telegram_id: UserContext} — employee_id, name, department_id, department_name.
Загружается лениво (первый запрос → БД → кеш), инвалидируется при смене ресторана.
При рестарте бота кеш пуст — подтягивается автоматически.

~10 КБ RAM на 57 сотрудников. Redis / файлы не нужны.
"""

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Employee, Department, EmployeeRole

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserContext:
    """Контекст авторизованного пользователя."""
    employee_id: str          # UUID строкой
    employee_name: str        # ФИО
    first_name: str           # Имя (для приветствий)
    department_id: str | None # UUID строкой
    department_name: str | None
    role_name: str | None = None  # Название должности (из iiko_employee_role)


# ─────────────────────────────────────────────────────
# In-memory cache with TTL
# ─────────────────────────────────────────────────────

_CACHE_TTL: float = 30 * 60  # 30 минут

_cache: dict[int, tuple[UserContext, float]] = {}   # {tg_id: (ctx, timestamp)}


def get_cached(telegram_id: int) -> UserContext | None:
    """Получить контекст из кеша (без обращения в БД). Уважает TTL."""
    entry = _cache.get(telegram_id)
    if entry is None:
        return None
    ctx, ts = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _cache.pop(telegram_id, None)
        return None
    return ctx


async def get_user_context(telegram_id: int) -> UserContext | None:
    """
    Получить контекст пользователя.
    Сначала проверяет кеш, при промахе — загружает из БД и кеширует.
    Возвращает None если пользователь не авторизован.
    """
    # 1) Кеш-хит
    ctx = get_cached(telegram_id)
    if ctx is not None:
        return ctx

    # 2) Промах — 1 JOIN-запрос вместо 3 последовательных
    t0 = time.monotonic()
    async with async_session_factory() as session:
        from sqlalchemy.orm import aliased
        dept_alias = aliased(Department)
        role_alias = aliased(EmployeeRole)
        stmt = (
            select(
                Employee,
                dept_alias.name.label("dept_name"),
                role_alias.name.label("role_name"),
            )
            .outerjoin(dept_alias, Employee.department_id == dept_alias.id)
            .outerjoin(role_alias, Employee.role_id == role_alias.id)
            .where(Employee.telegram_id == telegram_id)
        )
        row = (await session.execute(stmt)).first()
        if not row:
            logger.debug("[user_ctx] telegram_id=%d не авторизован", telegram_id)
            return None

        emp = row[0]
        dept_name = row.dept_name
        role_name = row.role_name

    ctx = UserContext(
        employee_id=str(emp.id),
        employee_name=emp.name or "",
        first_name=emp.first_name or emp.name or "сотрудник",
        department_id=str(emp.department_id) if emp.department_id else None,
        department_name=dept_name,
        role_name=role_name,
    )
    _cache[telegram_id] = (ctx, time.monotonic())
    logger.info("[user_ctx] Загружен из БД: tg:%d → «%s», ресторан «%s» за %.2f сек",
                telegram_id, ctx.employee_name, ctx.department_name, time.monotonic() - t0)
    return ctx


def set_context(
    telegram_id: int,
    employee_id: str,
    employee_name: str,
    first_name: str,
    department_id: str | None = None,
    department_name: str | None = None,
    role_name: str | None = None,
) -> UserContext:
    """Записать/обновить контекст в кеш (вызывается при авторизации/смене ресторана)."""
    ctx = UserContext(
        employee_id=employee_id,
        employee_name=employee_name,
        first_name=first_name,
        department_id=department_id,
        department_name=department_name,
        role_name=role_name,
    )
    _cache[telegram_id] = (ctx, time.monotonic())
    logger.info("[user_ctx] Кеш обновлён: tg:%d → «%s», ресторан «%s»",
                telegram_id, employee_name, department_name)
    return ctx


def update_department(telegram_id: int, department_id: str, department_name: str) -> None:
    """Обновить только ресторан в кеше (при смене ресторана)."""
    entry = _cache.get(telegram_id)
    if entry:
        ctx, _ = entry
        ctx.department_id = department_id
        ctx.department_name = department_name
        _cache[telegram_id] = (ctx, time.monotonic())  # обновляем TTL
        logger.info("[user_ctx] Ресторан обновлён в кеше: tg:%d → «%s»",
                    telegram_id, department_name)
    else:
        logger.warning("[user_ctx] update_department: tg:%d не в кеше, будет загружен при следующем запросе",
                       telegram_id)


def invalidate(telegram_id: int) -> None:
    """Удалить пользователя из кеша (при перепривязке к другому сотруднику)."""
    removed = _cache.pop(telegram_id, None)
    if removed:
        logger.info("[user_ctx] Кеш инвалидирован: tg:%d", telegram_id)
