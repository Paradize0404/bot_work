"""
In-memory кеш контекста авторизованного пользователя.

Хранит {telegram_id: UserContext} — employee_id, name, department_id, department_name.
Загружается лениво (первый запрос → БД → кеш), инвалидируется при смене ресторана.
При рестарте бота кеш пуст — подтягивается автоматически.

~10 КБ RAM на 57 сотрудников. Redis / файлы не нужны.
"""

import logging
import time
import json
from dataclasses import dataclass, asdict

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Employee, Department, EmployeeRole
from use_cases.redis_cache import get_cached_or_fetch, set_cache, invalidate_key

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserContext:
    """Контекст авторизованного пользователя."""

    employee_id: str  # UUID строкой
    employee_name: str  # ФИО
    first_name: str  # Имя (для приветствий)
    department_id: str | None  # UUID строкой
    department_name: str | None
    role_name: str | None = None  # Название должности (из iiko_employee_role)

    @classmethod
    def from_dict(cls, data: dict) -> "UserContext":
        return cls(**data)


# ─────────────────────────────────────────────────────
# Redis cache with TTL
# ─────────────────────────────────────────────────────

_CACHE_TTL: int = 30 * 60  # 30 минут


def _get_cache_key(telegram_id: int) -> str:
    return f"user_ctx:{telegram_id}"


async def get_user_context(telegram_id: int) -> UserContext | None:
    """
    Получить контекст пользователя.
    Сначала проверяет Redis кеш, при промахе — загружает из БД и кеширует.
    Возвращает None если пользователь не авторизован.
    """

    async def _fetch() -> dict | None:
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
        logger.info(
            "[user_ctx] Загружен из БД: tg:%d → «%s», ресторан «%s» за %.2f сек",
            telegram_id,
            ctx.employee_name,
            ctx.department_name,
            time.monotonic() - t0,
        )
        return asdict(ctx)

    data = await get_cached_or_fetch(
        _get_cache_key(telegram_id),
        _fetch,
        ttl_seconds=_CACHE_TTL,
        serializer=json.dumps,
        deserializer=json.loads,
    )

    if data:
        return UserContext.from_dict(data)
    return None


async def set_context(
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
    await set_cache(_get_cache_key(telegram_id), asdict(ctx), ttl_seconds=_CACHE_TTL)
    logger.info(
        "[user_ctx] Кеш обновлён: tg:%d → «%s», ресторан «%s»",
        telegram_id,
        employee_name,
        department_name,
    )
    return ctx


async def update_department(
    telegram_id: int, department_id: str, department_name: str
) -> None:
    """Обновить только ресторан в кеше (при смене ресторана)."""
    ctx = await get_user_context(telegram_id)
    if ctx:
        ctx.department_id = department_id
        ctx.department_name = department_name
        await set_cache(
            _get_cache_key(telegram_id), asdict(ctx), ttl_seconds=_CACHE_TTL
        )
        logger.info(
            "[user_ctx] Ресторан обновлён в кеше: tg:%d → «%s»",
            telegram_id,
            department_name,
        )
    else:
        logger.warning(
            "[user_ctx] update_department: tg:%d не в кеше, будет загружен при следующем запросе",
            telegram_id,
        )


async def invalidate(telegram_id: int) -> None:
    """Удалить пользователя из кеша (при перепривязке к другому сотруднику)."""
    await invalidate_key(_get_cache_key(telegram_id))
    logger.info("[user_ctx] Кеш инвалидирован: tg:%d", telegram_id)
