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
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.models import Employee, Department, EmployeeRole
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Dataclasses для результатов
# ═══════════════════════════════════════════════════════


class AuthStatus(Enum):
    """Статус авторизации пользователя."""

    AUTHORIZED = "authorized"  # полностью авторизован (есть department)
    NEEDS_DEPARTMENT = "needs_department"  # привязан, но нет ресторана
    NOT_AUTHORIZED = "not_authorized"  # не привязан


@dataclass(slots=True)
class AuthResult:
    """Результат проверки авторизации при /start."""

    status: AuthStatus
    first_name: str | None = None


@dataclass(slots=True)
class AuthSearchResult:
    """Результат поиска сотрудника по фамилии."""

    employees: list[dict]
    auto_bound_first_name: str | None = None  # если один сотрудник — уже привязан
    restaurants: list[dict] | None = None  # рестораны (если авто-привязка прошла)


@dataclass(slots=True)
class SelectionResult:
    """Результат выбора сотрудника из списка."""

    first_name: str
    restaurants: list[dict]


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
            .where(Employee.deleted.is_(False))
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
    logger.info(
        "[auth] Фамилия «%s» → найдено %d сотрудников за %.2f сек",
        last_name,
        len(found),
        time.monotonic() - t0,
    )
    return found


async def bind_telegram_id(employee_id: str, telegram_id: int) -> str:
    """
    Привязать telegram_id к сотруднику. Возвращает first_name.
    Если telegram_id уже привязан к другому сотруднику — отвязывает старого.
    """
    t0 = time.monotonic()
    logger.info(
        "[auth] Привязка telegram_id=%d к сотруднику %s...", telegram_id, employee_id
    )

    async with async_session_factory() as session:
        # Отвязываем telegram_id от старого сотрудника (если был)
        stmt_old = select(Employee).where(Employee.telegram_id == telegram_id)
        result_old = await session.execute(stmt_old)
        old_emp = result_old.scalar_one_or_none()
        if old_emp:
            logger.info(
                "[auth] Отвязываю telegram_id=%d от старого сотрудника %s (%s)",
                telegram_id,
                old_emp.id,
                old_emp.name,
            )
            old_emp.telegram_id = None
            await uctx.invalidate(telegram_id)  # инвалидируем кеш старого

        # Привязываем к новому
        stmt = select(Employee).where(Employee.id == UUID(employee_id))
        result = await session.execute(stmt)
        emp = result.scalar_one()
        emp.telegram_id = telegram_id

        # Определяем название должности (до commit — меньше round-tripов)
        role_name: str | None = None
        if emp.role_id:
            role_stmt = select(EmployeeRole.name).where(EmployeeRole.id == emp.role_id)
            role_result = await session.execute(role_stmt)
            role_name = role_result.scalar_one_or_none()

        await session.commit()

        first_name = emp.first_name or emp.name or "сотрудник"

        # Записываем в кеш (без ресторана — выберут на след. шаге)
        await uctx.set_context(
            telegram_id=telegram_id,
            employee_id=str(emp.id),
            employee_name=emp.name or "",
            first_name=first_name,
            role_name=role_name,
        )

        logger.info(
            "[auth] ✅ telegram_id=%d привязан к «%s» (%s) за %.2f сек",
            telegram_id,
            emp.name,
            employee_id,
            time.monotonic() - t0,
        )
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
            .where(Department.deleted.is_(False))
            .order_by(Department.name)
        )
        result = await session.execute(stmt)
        departments = result.scalars().all()

    items = [{"id": str(dep.id), "name": dep.name} for dep in departments]
    logger.info("[auth] Ресторанов: %d за %.2f сек", len(items), time.monotonic() - t0)
    return items


async def save_department(telegram_id: int, department_id: str) -> str:
    """
    Сохранить выбранный ресторан (department_id) для сотрудника.
    Возвращает название ресторана.
    """
    t0 = time.monotonic()
    logger.info(
        "[auth] Сохранение ресторана %s для telegram_id=%d...",
        department_id,
        telegram_id,
    )

    async with async_session_factory() as session:
        stmt = select(Employee).where(Employee.telegram_id == telegram_id)
        result = await session.execute(stmt)
        emp = result.scalar_one_or_none()
        if not emp:
            logger.warning(
                "[auth] ❌ Сотрудник с telegram_id=%d не найден!", telegram_id
            )
            raise ValueError("Сотрудник не найден. Пройдите авторизацию /start")

        emp.department_id = UUID(department_id)

        # Получаем название ресторана (до commit — меньше round-tripов)
        dept_stmt = select(Department.name).where(Department.id == UUID(department_id))
        dept_result = await session.execute(dept_stmt)
        dept_name = dept_result.scalar_one_or_none() or department_id

        await session.commit()

        logger.info(
            "[auth] ✅ Сотрудник «%s» (tg:%d) → ресторан «%s» за %.2f сек",
            emp.name,
            telegram_id,
            dept_name,
            time.monotonic() - t0,
        )
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


# ═══════════════════════════════════════════════════════
# Высокоуровневые use-case функции (тонкие handlers)
# ═══════════════════════════════════════════════════════


async def check_auth_status(telegram_id: int) -> AuthResult:
    """
    Проверить статус авторизации при /start.
    Возвращает AuthResult с enum-статусом.
    """
    ctx = await uctx.get_user_context(telegram_id)
    if ctx and ctx.department_id:
        return AuthResult(status=AuthStatus.AUTHORIZED, first_name=ctx.first_name)
    if ctx:
        return AuthResult(status=AuthStatus.NEEDS_DEPARTMENT, first_name=ctx.first_name)
    return AuthResult(status=AuthStatus.NOT_AUTHORIZED)


async def process_auth_by_lastname(telegram_id: int, lastname: str) -> AuthSearchResult:
    """
    Поиск сотрудника по фамилии + авто-привязка если найден ровно один.
    Возвращает AuthSearchResult.
    """
    employees = await find_employees_by_last_name(lastname)

    if not employees or len(employees) != 1:
        return AuthSearchResult(employees=employees)

    # Единственный сотрудник — привязываем сразу
    emp = employees[0]
    first_name = await bind_telegram_id(emp["id"], telegram_id)
    restaurants = await get_restaurants()
    return AuthSearchResult(
        employees=employees,
        auto_bound_first_name=first_name,
        restaurants=restaurants,
    )


async def complete_employee_selection(
    telegram_id: int, employee_id: str
) -> SelectionResult:
    """
    Привязать telegram_id к выбранному сотруднику и получить рестораны.
    """
    first_name = await bind_telegram_id(employee_id, telegram_id)
    restaurants = await get_restaurants()
    return SelectionResult(first_name=first_name, restaurants=restaurants)


async def complete_department_selection(
    telegram_id: int, department_id: str, employee_id: str | None = None
) -> str:
    """
    Сохранить выбранный ресторан и обновить кеш.
    Возвращает название ресторана.
    """
    dept_name = await save_department(telegram_id, department_id)

    # Обновляем кеш
    ctx = await uctx.get_user_context(telegram_id)
    if ctx:
        await uctx.update_department(telegram_id, department_id, dept_name)
    elif employee_id:
        # Первая авторизация — загрузим полный контекст из БД
        await uctx.get_user_context(telegram_id)
        await uctx.update_department(telegram_id, department_id, dept_name)

    return dept_name
