"""
Use-case: подписки на отчёты дня (по подразделениям).

Администратор настраивает из бота:
  - Кому приходят отчёты
  - По каким точкам (подразделениям)

При отправке отчёта дня, получателями становятся пользователи,
у которых есть подписка на подразделение автора отчёта.
"""

import logging

from sqlalchemy import select, delete

from db.engine import async_session_factory
from db.models import ReportSubscription, Department, Employee, GuestUser

logger = logging.getLogger(__name__)

LABEL = "ReportSub"


async def get_subscribers_for_department(department_id: str) -> list[int]:
    """
    Получить список telegram_id подписчиков для конкретного подразделения.
    """
    from uuid import UUID

    async with async_session_factory() as session:
        stmt = select(ReportSubscription.telegram_id).where(
            ReportSubscription.department_id == UUID(department_id)
        )
        result = await session.execute(stmt)
        tg_ids = [row[0] for row in result.all()]

    logger.info(
        "[%s] Подписчики для dept=%s: %d",
        LABEL,
        department_id,
        len(tg_ids),
    )
    return tg_ids


async def get_subscriptions_for_user(telegram_id: int) -> list[dict]:
    """
    Получить список подписок пользователя (department_id + department_name).
    """
    async with async_session_factory() as session:
        stmt = (
            select(
                ReportSubscription.department_id,
                Department.name.label("dept_name"),
            )
            .outerjoin(
                Department,
                ReportSubscription.department_id == Department.id,
            )
            .where(ReportSubscription.telegram_id == telegram_id)
            .order_by(Department.name)
        )
        result = await session.execute(stmt)
        return [
            {
                "department_id": str(row.department_id),
                "department_name": row.dept_name or str(row.department_id),
            }
            for row in result.all()
        ]


async def add_subscription(
    telegram_id: int,
    department_id: str,
    created_by: int | None = None,
) -> bool:
    """
    Добавить подписку пользователя на отчёты подразделения.
    Возвращает True если подписка создана, False если уже существует.
    """
    from uuid import UUID

    async with async_session_factory() as session:
        # Проверяем существование
        stmt = select(ReportSubscription).where(
            ReportSubscription.telegram_id == telegram_id,
            ReportSubscription.department_id == UUID(department_id),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            logger.info(
                "[%s] Подписка уже существует: tg:%d → dept=%s",
                LABEL,
                telegram_id,
                department_id,
            )
            return False

        sub = ReportSubscription(
            telegram_id=telegram_id,
            department_id=UUID(department_id),
            created_by=created_by,
        )
        session.add(sub)
        await session.commit()

    logger.info(
        "[%s] Подписка создана: tg:%d → dept=%s (by tg:%s)",
        LABEL,
        telegram_id,
        department_id,
        created_by,
    )
    return True


async def remove_subscription(telegram_id: int, department_id: str) -> bool:
    """
    Удалить подписку пользователя на отчёты подразделения.
    Возвращает True если подписка удалена, False если не найдена.
    """
    from uuid import UUID

    async with async_session_factory() as session:
        stmt = delete(ReportSubscription).where(
            ReportSubscription.telegram_id == telegram_id,
            ReportSubscription.department_id == UUID(department_id),
        )
        result = await session.execute(stmt)
        await session.commit()

    deleted = result.rowcount > 0
    logger.info(
        "[%s] Подписка удалена=%s: tg:%d → dept=%s",
        LABEL,
        deleted,
        telegram_id,
        department_id,
    )
    return deleted


async def get_all_subscriptions() -> list[dict]:
    """
    Получить все подписки (для отображения в настройках).
    Возвращает: [{telegram_id, user_name, department_id, department_name}, ...]
    """
    async with async_session_factory() as session:
        # Основной запрос
        stmt = (
            select(
                ReportSubscription.telegram_id,
                ReportSubscription.department_id,
                Department.name.label("dept_name"),
            )
            .outerjoin(
                Department,
                ReportSubscription.department_id == Department.id,
            )
            .order_by(Department.name, ReportSubscription.telegram_id)
        )
        result = await session.execute(stmt)
        rows = result.all()

        if not rows:
            return []

        # Собираем telegram_id для поиска имён
        tg_ids = list({r.telegram_id for r in rows})

        # Ищем имена в employees
        emp_stmt = select(Employee.telegram_id, Employee.name).where(
            Employee.telegram_id.in_(tg_ids)
        )
        emp_result = await session.execute(emp_stmt)
        emp_names = {r.telegram_id: r.name for r in emp_result.all()}

        # Ищем имена в гостях
        guest_stmt = select(GuestUser.telegram_id, GuestUser.full_name).where(
            GuestUser.telegram_id.in_(tg_ids)
        )
        guest_result = await session.execute(guest_stmt)
        guest_names = {
            r.telegram_id: f"{r.full_name} (гость)" for r in guest_result.all()
        }

    items = []
    for row in rows:
        name = emp_names.get(row.telegram_id) or guest_names.get(
            row.telegram_id, f"tg:{row.telegram_id}"
        )
        items.append(
            {
                "telegram_id": row.telegram_id,
                "user_name": name,
                "department_id": str(row.department_id),
                "department_name": row.dept_name or str(row.department_id),
            }
        )
    return items


async def get_all_authorized_users() -> list[dict]:
    """
    Получить всех авторизованных пользователей (сотрудники + гости).
    Для выбора при добавлении подписки.
    """
    async with async_session_factory() as session:
        emp_stmt = (
            select(Employee.telegram_id, Employee.name)
            .where(Employee.telegram_id.isnot(None))
            .where(Employee.deleted.is_(False))
            .order_by(Employee.name)
        )
        emp_result = await session.execute(emp_stmt)
        employees = emp_result.all()

        guest_stmt = select(GuestUser.telegram_id, GuestUser.full_name).order_by(
            GuestUser.full_name
        )
        guest_result = await session.execute(guest_stmt)
        guests = guest_result.all()

    users = [{"telegram_id": r.telegram_id, "name": r.name or "—"} for r in employees]
    for g in guests:
        users.append({"telegram_id": g.telegram_id, "name": f"{g.full_name} (гость)"})
    return users
