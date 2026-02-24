"""
Use-case: зарплатная ведомость → Google Sheets.

Выгружает список сотрудников из БД (синхронизированных из iiko)
в лист «Зарплаты» Google Таблицы.

Фильтрация:
  - удалённые (deleted=True) — исключаются автоматически
  - вручную исключённые через бот (таблица salary_exclusions) — исключаются

Сотрудники сортируются по имени (ФИО) в алфавитном порядке.
"""

import logging
import time
from datetime import date, datetime

from sqlalchemy import select, delete

from db.engine import async_session_factory
from db.models import Employee, SalaryExclusion
from adapters.google_sheets import sync_salary_sheet

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Управление списком исключений
# ═══════════════════════════════════════════════════════


async def load_salary_exclusions() -> set[str]:
    """Вернуть set[employee_id] (iiko UUID) всех вручную исключённых сотрудников."""
    async with async_session_factory() as session:
        rows = (await session.execute(select(SalaryExclusion))).scalars().all()
    return {r.employee_id for r in rows}


async def toggle_salary_exclusion(
    employee_id: str, excluded_by: str | None = None
) -> bool:
    """
    Переключить статус исключения сотрудника.

    Возвращает True если сотрудник теперь исключён, False если снова включён.
    """
    async with async_session_factory() as session:
        existing = await session.get(SalaryExclusion, employee_id)
        if existing:
            await session.delete(existing)
            await session.commit()
            logger.info(
                "[salary] Сотрудник %s возвращён в список ФОТ (by=%s)",
                employee_id,
                excluded_by,
            )
            return False
        else:
            session.add(
                SalaryExclusion(
                    employee_id=employee_id,
                    excluded_by=excluded_by,
                    excluded_at=datetime.utcnow(),
                )
            )
            await session.commit()

    logger.info(
        "[salary] Сотрудник %s исключён из списка ФОТ (by=%s)", employee_id, excluded_by
    )

    # Закрываем активную запись в «Истории ставок» сегодняшней датой
    try:
        from use_cases.salary_history import delete_history_for_employee

        await delete_history_for_employee(employee_id)
    except Exception:
        logger.exception("[salary] Ошибка при удалении истории для %s", employee_id)

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

    # ── 2. Фильтруем: только не удалённые, затем убираем вручную исключённых ──
    exclusions = await load_salary_exclusions()
    real = [emp for emp in rows if not emp.deleted and str(emp.id) not in exclusions]
    logger.info(
        "[salary] После фильтрации: %d сотрудников (из %d в БД, %d исключений)",
        len(real),
        len(rows),
        len(exclusions),
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

    # ── 4b. Подгружаем актуальные ставки из «Истории ставок» как fallback ──
    # (используются только если ячейка в листе «Зарплаты» пустая)
    try:
        from use_cases.salary_history import (
            load_salary_history_index,
            get_rate_for_date,
        )

        history_index = await load_salary_history_index()
        today = date.today()
        for emp in employees_data:
            history = history_index.get(emp["name"], [])
            active = get_rate_for_date(history, today)
            if active:
                emp["hint_sal_type"] = active["sal_type"]
                emp["hint_rate"] = (
                    str(int(active["rate"]))
                    if active["rate"] == int(active["rate"])
                    else str(active["rate"])
                )
        logger.info("[salary] История ставок загружена для fallback")
    except Exception:
        logger.exception("[salary] Ошибка загрузки истории ставок для fallback")

    # ── 5. Выгружаем в Google Sheets ──
    count = await sync_salary_sheet(employees_data)

    # ── 6. Обновляем дропдаун сотрудников в «История ставок» + первичное заполнение ──
    try:
        from use_cases.salary_history import (
            refresh_history_sheet_dropdowns,
            bootstrap_salary_history_sheet,
        )

        emp_names = [e["name"] for e in employees_data]
        await refresh_history_sheet_dropdowns(emp_names)
        await bootstrap_salary_history_sheet()
    except Exception:
        logger.exception("[salary] Ошибка обновления дропдаунов в «История ставок»")

    logger.info(
        "[salary] Выгрузка завершена: %d сотрудников за %.1f сек",
        count,
        time.monotonic() - t0,
    )
    return count
