"""
Use-case: авторизация сотрудника через Telegram-бота.

Поток:
  1. Поиск сотрудника по фамилии (last_name) в iiko_employee
  2. Привязка telegram_id к найденному сотруднику
  3. Получение списка ресторанов (department_type = 'DEPARTMENT')
  4. Сохранение выбранного ресторана (department_id) у сотрудника
"""

import logging
import time
from uuid import UUID

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.models import Employee, Department, EmployeeRole
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)


async def find_employees_by_last_name(last_name: str) -> list[dict]:
    """
    Поиск сотрудников по фамилии (case-insensitive, точное совпадение).
    Возвращает список dict с id, name, first_name, last_name.
    """
    t0 = time.monotonic()
    logger.info("[auth] Поиск сотрудника по фамилии «%s»...", last_name)

    async with async_session_factory() as session:
        stmt = (
            select(Employee)
            .where(func.lower(Employee.last_name) == last_name.strip().lower())
            .where(Employee.deleted == False)
        )
        result = await session.execute(stmt)
        employees = result.scalars().all()

    found = [
        {
            "id": str(emp.id),
            "name": emp.name,
            "first_name": emp.first_name,
            "last_name": emp.last_name,
        }
        for emp in employees
    ]
    logger.info("[auth] Фамилия «%s» → найдено %d сотрудников за %.2f сек",
                last_name, len(found), time.monotonic() - t0)
    return found


async def bind_telegram_id(employee_id: str, telegram_id: int) -> str:
    """
    Привязать telegram_id к сотруднику. Возвращает first_name.
    Если telegram_id уже привязан к другому сотруднику — отвязывает старого.
    """
    t0 = time.monotonic()
    logger.info("[auth] Привязка telegram_id=%d к сотруднику %s...", telegram_id, employee_id)

    async with async_session_factory() as session:
        # Отвязываем telegram_id от старого сотрудника (если был)
        stmt_old = select(Employee).where(Employee.telegram_id == telegram_id)
        result_old = await session.execute(stmt_old)
        old_emp = result_old.scalar_one_or_none()
        if old_emp:
            logger.info("[auth] Отвязываю telegram_id=%d от старого сотрудника %s (%s)",
                        telegram_id, old_emp.id, old_emp.name)
            old_emp.telegram_id = None
            uctx.invalidate(telegram_id)  # инвалидируем кеш старого

        # Привязываем к новому
        stmt = select(Employee).where(Employee.id == UUID(employee_id))
        result = await session.execute(stmt)
        emp = result.scalar_one()
        emp.telegram_id = telegram_id
        await session.commit()

        first_name = emp.first_name or emp.name or "сотрудник"

        # Определяем название должности
        role_name: str | None = None
        if emp.role_id:
            role_stmt = select(EmployeeRole.name).where(EmployeeRole.id == emp.role_id)
            role_result = await session.execute(role_stmt)
            role_name = role_result.scalar_one_or_none()

        # Записываем в кеш (без ресторана — выберут на след. шаге)
        uctx.set_context(
            telegram_id=telegram_id,
            employee_id=str(emp.id),
            employee_name=emp.name or "",
            first_name=first_name,
            role_name=role_name,
        )

        logger.info("[auth] ✅ telegram_id=%d привязан к «%s» (%s) за %.2f сек",
                    telegram_id, emp.name, employee_id, time.monotonic() - t0)
        return first_name


async def get_restaurants() -> list[dict]:
    """
    Получить все подразделения с department_type = 'DEPARTMENT' (рестораны).
    Возвращает список dict с id и name.
    """
    t0 = time.monotonic()
    logger.info("[auth] Загружаю список ресторанов (department_type=DEPARTMENT)...")

    async with async_session_factory() as session:
        stmt = (
            select(Department)
            .where(func.upper(Department.department_type) == "DEPARTMENT")
            .where(Department.deleted == False)
            .order_by(Department.name)
        )
        result = await session.execute(stmt)
        departments = result.scalars().all()

    items = [
        {"id": str(dep.id), "name": dep.name}
        for dep in departments
    ]
    logger.info("[auth] Ресторанов: %d за %.2f сек", len(items), time.monotonic() - t0)
    return items


async def save_department(telegram_id: int, department_id: str) -> str:
    """
    Сохранить выбранный ресторан (department_id) для сотрудника.
    Возвращает название ресторана.
    """
    t0 = time.monotonic()
    logger.info("[auth] Сохранение ресторана %s для telegram_id=%d...", department_id, telegram_id)

    async with async_session_factory() as session:
        stmt = select(Employee).where(Employee.telegram_id == telegram_id)
        result = await session.execute(stmt)
        emp = result.scalar_one_or_none()
        if not emp:
            logger.warning("[auth] ❌ Сотрудник с telegram_id=%d не найден!", telegram_id)
            raise ValueError("Сотрудник не найден. Пройдите авторизацию /start")

        emp.department_id = UUID(department_id)
        await session.commit()

        # Получаем название ресторана
        dept_stmt = select(Department).where(Department.id == UUID(department_id))
        dept_result = await session.execute(dept_stmt)
        dept = dept_result.scalar_one_or_none()
        dept_name = dept.name if dept else department_id

        logger.info("[auth] ✅ Сотрудник «%s» (tg:%d) → ресторан «%s» за %.2f сек",
                    emp.name, telegram_id, dept_name, time.monotonic() - t0)
        return dept_name


async def get_employee_by_telegram_id(telegram_id: int) -> dict | None:
    """
    Получить сотрудника по telegram_id. None если не авторизован.
    """
    async with async_session_factory() as session:
        stmt = select(Employee).where(Employee.telegram_id == telegram_id)
        result = await session.execute(stmt)
        emp = result.scalar_one_or_none()
        if not emp:
            logger.debug("[auth] telegram_id=%d не авторизован", telegram_id)
            return None
        logger.debug("[auth] telegram_id=%d → «%s»", telegram_id, emp.name)
        return {
            "id": str(emp.id),
            "name": emp.name,
            "first_name": emp.first_name,
            "last_name": emp.last_name,
            "department_id": str(emp.department_id) if emp.department_id else None,
        }
