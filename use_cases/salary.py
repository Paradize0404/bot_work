"""
Use-case: зарплатная ведомость → Google Sheets.

Выгружает список реальных сотрудников из БД (синхронизированных из iiko)
в лист «Зарплаты» Google Таблицы.

Фильтрация:
  - удалённые (deleted=True) — исключаются
  - системные записи (raw_json->>'employee' != 'true') — исключаются
  - записи со словом «фриланс» в имени — исключаются

Сотрудники сортируются по имени (ФИО) в алфавитном порядке.
"""

import logging
import time

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import Employee
from adapters.google_sheets import sync_salary_sheet

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Фильтрация сотрудников
# ═══════════════════════════════════════════════════════

_SYSTEM_KEYWORDS = ("системн", "system", "admin", "админ", "тест", "test")
"""Подстроки в нижнем регистре, при наличии которых сотрудник считается системным."""


def _is_real_employee(emp: Employee) -> bool:
    """
    True — если это реальный (не системный, не удалённый, не фрилансер) сотрудник.

    Правила:
      1. deleted=False
      2. raw_json->>'employee' == 'true'  (флаг iiko: является сотрудником, не только поставщиком/клиентом)
      3. имя не содержит «фриланс»
      4. имя не содержит слова-маркеры системных учёток (системн, system, admin, …)
    """
    if emp.deleted:
        return False

    # Флаг «является сотрудником» из iiko XML → raw_json
    raw = emp.raw_json or {}
    employee_flag = str(raw.get("employee", "true")).strip().lower()
    if employee_flag == "false":
        return False

    name_lower = (emp.name or "").lower()

    if "фриланс" in name_lower:
        return False

    for kw in _SYSTEM_KEYWORDS:
        if kw in name_lower:
            return False

    return True


# ═══════════════════════════════════════════════════════
# Use-case
# ═══════════════════════════════════════════════════════


async def export_salary_sheet(triggered_by: str | None = None) -> int:
    """
    Выгрузить список сотрудников в лист «Зарплаты» Google Sheets.

    Работает с данными из локальной БД (синхронизированными из iiko).
    Для актуальности рекомендуется предварительно вызвать sync_employees().

    Возвращает количество сотрудников, записанных в таблицу.
    """
    t0 = time.monotonic()
    logger.info("[salary] Начинаю выгрузку зарплатной ведомости (by=%s)", triggered_by)

    # ── 1. Читаем всех сотрудников из БД ──
    async with async_session_factory() as session:
        rows = (await session.execute(select(Employee))).scalars().all()

    logger.info("[salary] Из БД получено %d записей сотрудников", len(rows))

    # ── 2. Фильтруем ──
    real = [emp for emp in rows if _is_real_employee(emp)]
    logger.info(
        "[salary] После фильтрации: %d реальных сотрудников (из %d в БД)",
        len(real),
        len(rows),
    )

    # ── 3. Формируем ФИО = Фамилия Имя Отчество ──
    employees_data: list[dict] = []
    for emp in real:
        parts = [
            p
            for p in (emp.last_name, emp.first_name, emp.middle_name)
            if p and p.strip()
        ]
        full_name = " ".join(parts) if parts else (emp.name or "").strip()
        if not full_name:
            continue
        employees_data.append({"name": full_name, "id": str(emp.id)})

    # ── 4. Сортируем по ФИО ──
    employees_data.sort(key=lambda x: x["name"].lower())

    logger.info(
        "[salary] Сортировка завершена, %d записей для выгрузки", len(employees_data)
    )

    # ── 5. Выгружаем в Google Sheets ──
    count = await sync_salary_sheet(employees_data)

    logger.info(
        "[salary] Выгрузка завершена: %d сотрудников за %.1f сек",
        count,
        time.monotonic() - t0,
    )
    return count
