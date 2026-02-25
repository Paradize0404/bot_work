"""
Use-case: расчёт ФОТ (фонд оплаты труда) за текущий месяц → Google Sheets.

Единственный источник ставок — лист «История ставок» (salary_history).
Для каждой смены ставка берётся из истории НА ДАТУ ТОЙ СМЕНЫ, поэтому
если ставка менялась в середине месяца — начисления корректны.

Логика:
  1. Период = 1-е число текущего месяца … сегодня (по Калининграду)
  2. Загружаем явки из iiko (GET /resto/api/employees/attendance)
  3. Из «Истории ставок» (БД) берём ставки / тип / мотивацию
  4. Из БД берём сотрудников + должности
  5. Рассчитываем начисления по типу:
       - почасовая:    Σ(ставка_на_дату_смени × часов_в_смене)
       - посменная:    Σ(ставка_на_дату_смени)  за каждую смену
       - ежемесячная:  пропорционально дням с учётом смены ставки
  6. Группируем по подразделениям (из данных явок)
  7. Ниже — секция ежемесячных сотрудников (пропорциональная оплата)
  8. Создаём/обновляем вкладку «ФОТ {месяц} {год}»

Запуск: ежедневно в 07:00 из scheduler.py
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from adapters.google_sheets import sync_fot_sheet
from adapters.iiko_api import fetch_attendance
from use_cases.revenue_motivation import get_revenue_motivation_map
from use_cases.salary_history import (
    load_salary_history_index,
    get_rate_for_date,
    get_prorated_monthly,
)
from db.engine import async_session_factory
from db.models import Employee, EmployeeRole
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

# Названия месяцев (именительный падеж) для заголовка вкладки
_MONTH_NAMES_RU = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}


def _make_initials(first_name: str | None, middle_name: str | None) -> str:
    """Вернуть «И.О.» или «И.» или '' по имени/отчеству."""
    parts = []
    if first_name and first_name.strip():
        parts.append(first_name.strip()[0].upper() + ".")
    if middle_name and middle_name.strip():
        parts.append(middle_name.strip()[0].upper() + ".")
    return "".join(parts)


def _display_name(emp: Employee) -> str:
    """Фамилия И.О. — для отображения в ФОТ листе."""
    initials = _make_initials(emp.first_name, emp.middle_name)
    last = (emp.last_name or "").strip()
    if last and initials:
        return f"{last} {initials}"
    if last:
        return last
    # fallback: полное имя из поля name
    return (emp.name or "").strip()


def _full_name(emp: Employee) -> str:
    """Фамилия Имя Отчество — ключ для сопоставления с листом «Зарплаты»."""
    parts = [
        p for p in (emp.last_name, emp.first_name, emp.middle_name) if p and p.strip()
    ]
    return " ".join(parts) if parts else (emp.name or "").strip()


def _parse_dt(s: str | None) -> datetime | None:
    """Разобрать строчку датовремени из iiko API.
    Поддерживает форматы: ISO с таймзоной (+02:00), без таймзоны, с миллисекундами.
    """
    if not s:
        return None
    s = s.strip()
    # Python 3.7+ fromisoformat поддерживает +HH:MM offset (Python 3.11+ полностью)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _hours_from_attendance(rec: dict) -> float:
    """Вычислить кол-во рабочих часов из записи явки (dateFrom / dateTo)."""
    dt_from = _parse_dt(rec.get("dateFrom"))
    dt_to = _parse_dt(rec.get("dateTo"))
    if dt_from and dt_to and dt_to > dt_from:
        delta = dt_to - dt_from
        return delta.total_seconds() / 3600.0
    return 0.0


def _days_in_month(year: int, month: int) -> int:
    """Kол-во дней в месяце."""
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - datetime(year, month, 1)).days


async def update_fot_sheet(triggered_by: str | None = None) -> int:
    """
    Рассчитать ФОТ за текущий месяц и записать в Google Sheets.

    Возвращает суммарное количество строк сотрудников в листе.
    """
    t0 = time.monotonic()
    today = now_kgd().date()
    month_start = today.replace(day=1)

    date_from_str = month_start.strftime("%Y-%m-%d")
    date_to_str = today.strftime("%Y-%m-%d")

    month_name = _MONTH_NAMES_RU[today.month]
    tab_name = f"ФОТ {month_name} {today.year}"
    period_label = f"{month_start.strftime('%d.%m.%Y')} – {today.strftime('%d.%m.%Y')}"

    days_in_m = _days_in_month(today.year, today.month)
    days_passed = today.day  # сегодня включительно

    logger.info(
        "[payroll] Расчёт ФОТ: период %s – %s (%d/%d дней), вкладка «%s»",
        date_from_str,
        date_to_str,
        days_passed,
        days_in_m,
        tab_name,
    )

    # ── 1. Загружаем данные параллельно ──

    # Явки из iiko
    try:
        attendance_records = await fetch_attendance(
            date_from=date_from_str,
            date_to=date_to_str,
            with_payment_details=False,
        )
        logger.info("[payroll] Явки из iiko: %d записей", len(attendance_records))
    except Exception:
        logger.exception("[payroll] Ошибка загрузки явок из iiko")
        attendance_records = []

    # История ставок + данные из БД — параллельно
    # Сначала синхронизируем GSheet → БД (чтобы подхватить ручные изменения)
    try:
        from use_cases.salary_history import sync_salary_history

        await sync_salary_history(triggered_by=triggered_by or "fot")
    except Exception:
        logger.exception("[payroll] Ошибка синхронизации «Истории ставок» GSheet → БД")

    history_index, (employees_db, roles_db) = await asyncio.gather(
        load_salary_history_index(),
        _load_db_data(),
    )

    # ── 2. Индексы ──
    # employee_id (iiko UUID) → Employee DB
    emp_by_iiko_id: dict[str, Employee] = {}
    emp_by_full_name: dict[str, Employee] = {}
    for emp in employees_db:
        iiko_id = str(emp.id)
        emp_by_iiko_id[iiko_id] = emp
        fn = _full_name(emp)
        if fn:
            emp_by_full_name[fn] = emp

    # role_id (iiko UUID) → EmployeeRole DB
    role_by_iiko_id: dict[str, EmployeeRole] = {str(r.id): r for r in roles_db}

    # ── 3. Агрегация явок ──
    # emp_iiko_id → {dept_id: {"dept_name": str, "shifts": int, "hours": float}}
    emp_dept_stats: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"dept_name": "", "shifts": 0, "hours": 0.0})
    )
    # emp_iiko_id → суммарный заработок (каждая смена × ставка из истории на дату смены)
    emp_shift_earnings: dict[str, float] = defaultdict(float)
    # emp_iiko_id → суммарные часы (для отображения в Ед.)
    emp_total_hours: dict[str, float] = defaultdict(float)

    # iiko_id → full_name (для доступа к history_index)
    _iiko_to_fn: dict[str, str] = {
        iiko_id: _full_name(emp) for iiko_id, emp in emp_by_iiko_id.items()
    }

    for rec in attendance_records:
        emp_id = (rec.get("employeeId") or "").strip()
        dept_id = (rec.get("departmentId") or "").strip()
        dept_name = (rec.get("departmentName") or "").strip() or dept_id or "Без цеха"
        if not emp_id:
            continue
        emp_dept_stats[emp_id][dept_id]["dept_name"] = dept_name
        emp_dept_stats[emp_id][dept_id]["shifts"] += 1
        hours = _hours_from_attendance(rec)
        emp_dept_stats[emp_id][dept_id]["hours"] += hours
        emp_total_hours[emp_id] += hours

        # Начисление за смену: ставка из «Истории ставок» на дату смены
        fn = _iiko_to_fn.get(emp_id)
        if fn:
            history = history_index.get(fn, [])
            shift_dt = _parse_dt(rec.get("dateFrom"))
            if shift_dt and history:
                active = get_rate_for_date(history, shift_dt.date())
                if active:
                    shift_rate = float(active["rate"])
                    if active["sal_type"] == "посменная":
                        emp_shift_earnings[emp_id] += shift_rate
                    elif active["sal_type"] == "почасовая":
                        emp_shift_earnings[emp_id] += shift_rate * hours

    logger.info("[payroll] Уникальных сотрудников с явками: %d", len(emp_dept_stats))

    # ── 3b. Расчёт мотивации «от выручки» ──
    emp_iiko_to_fullname: dict[str, str] = {
        iiko_id: _full_name(emp) for iiko_id, emp in emp_by_iiko_id.items()
    }
    motivation_map: dict[str, float] = await get_revenue_motivation_map(
        attendance_records=attendance_records,
        emp_iiko_to_fullname=emp_iiko_to_fullname,
        date_from=month_start,
        date_to=today,
        history_index=history_index,
    )
    logger.info(
        "[payroll] Мотивация «от выручки»: %d сотрудников с ненулёвой суммой",
        len(motivation_map),
    )

    # ── 4. Расчёт начислений ──
    # dept_id → {"dept_name": str, "employees": [emp_row]}
    dept_sections_map: dict[str, dict] = {}
    # Имена сотрудников с ежемесячной ставкой, у которых есть явки (они НЕ попадают в monthly_section)
    monthly_with_attendance: set[str] = set()

    for emp_iiko_id, dept_map in emp_dept_stats.items():
        emp = emp_by_iiko_id.get(emp_iiko_id)
        if emp is None:
            logger.debug("[payroll] Сотрудник %s не найден в БД", emp_iiko_id)
            continue

        fn = _full_name(emp)
        # Сначала ищем в истории ставок, затем фолбэк на плоские настройки
        history = history_index.get(fn, [])
        active = get_rate_for_date(history, today)
        if active:
            sal_type = active["sal_type"]
            rate = float(active["rate"])
        else:
            sal_type = ""
            rate = 0.0

        # Должность из iiko: ищем роль по role_id (из Employee или из явки)
        role_name = _get_role_name(emp, emp_dept_stats[emp_iiko_id], role_by_iiko_id)

        # Суммарные данные по всем подразделениям
        total_shifts = sum(d["shifts"] for d in dept_map.values())
        total_hours = sum(d["hours"] for d in dept_map.values())

        # Начисление (ставка берётся из «Истории ставок» на дату каждой смены)
        if sal_type == "почасовая":
            units = round(emp_total_hours.get(emp_iiko_id, total_hours), 2)
            total = round(emp_shift_earnings.get(emp_iiko_id, 0.0), 2)
            # Эффективная ставка для отображения = total / hours (средневзвешенная)
            rate = round(total / units) if units else 0
        elif sal_type == "посменная":
            units = total_shifts
            total = round(emp_shift_earnings.get(emp_iiko_id, 0.0), 2)
        elif sal_type == "ежемесячная":
            # Ежемесячные с явками → в monthly_section (не в dept-секции)
            monthly_with_attendance.add(fn)
            # Сохраняем историю для расчёта в monthly_section
            continue
        else:
            # Тип не задан — показываем но не считаем
            units = total_shifts
            total = 0.0

        # Основное подразделение = то, где больше всего смен
        primary_dept_id = max(dept_map, key=lambda d: dept_map[d]["shifts"])
        primary_dept_name = dept_map[primary_dept_id]["dept_name"]

        if primary_dept_id not in dept_sections_map:
            dept_sections_map[primary_dept_id] = {
                "dept_name": primary_dept_name,
                "employees": [],
            }

        dept_sections_map[primary_dept_id]["employees"].append(
            {
                "name": _display_name(emp),
                "role": role_name,
                "sal_type": sal_type or "—",
                "rate": rate,
                "units": units,
                "total": total,
                "motivation": motivation_map.get(fn, 0.0),
            }
        )

    # ── 5. Секция ежемесячных (без явок + с явками) ──
    monthly_section: list[dict] = []

    # Собираем всех ежемесячных: из истории + фолбэк из flat-настроек
    monthly_names: set[str] = set()
    # Из истории
    for fn, hist in history_index.items():
        active = get_rate_for_date(hist, today)
        if active and active["sal_type"] == "ежемесячная":
            monthly_names.add(fn)
    # Источник — только История ставок

    for fn in monthly_names:
        # Ставка: из истории или flat-настроек
        history = history_index.get(fn, [])
        active = get_rate_for_date(history, today)
        if active:
            rate = float(active["rate"])
        else:
            rate = 0.0

        emp = emp_by_full_name.get(fn)
        if emp is None:
            # Не нашли в БД — пропускаем
            logger.debug("[payroll] Ежемесячный сотрудник «%s» не найден в БД", fn)
            continue

        role_name = ""
        if emp.role_id and str(emp.role_id) in role_by_iiko_id:
            role_name = role_by_iiko_id[str(emp.role_id)].name or ""

        # Пропорциональный расчёт с учётом смены ставки внутри месяца
        if history:
            total = get_prorated_monthly(history, month_start, today, days_in_m)
        else:
            total = round(rate / days_in_m * days_passed, 2) if days_in_m else 0.0
        units = days_passed  # дней прошло (включительно)

        monthly_section.append(
            {
                "name": _display_name(emp),
                "role": role_name,
                "sal_type": "ежемесячная",
                "rate": rate,
                "units": units,
                "total": total,
                "motivation": motivation_map.get(fn, 0.0),
            }
        )

    # Сортируем внутри каждой секции по имени
    for sec in dept_sections_map.values():
        sec["employees"].sort(key=lambda e: e["name"].lower())
    monthly_section.sort(key=lambda e: e["name"].lower())

    dept_sections = sorted(
        dept_sections_map.values(),
        key=lambda s: s["dept_name"].lower(),
    )

    logger.info(
        "[payroll] Итог: %d подразделений, %d ежемесячных",
        len(dept_sections),
        len(monthly_section),
    )

    # ── 6. Запись в GSheets ──
    count = await sync_fot_sheet(
        dept_sections=dept_sections,
        monthly_section=monthly_section,
        tab_name=tab_name,
        period_label=period_label,
    )

    elapsed = time.monotonic() - t0
    logger.info(
        "[payroll] ФОТ «%s» записан: %d сотрудников за %.1f сек",
        tab_name,
        count,
        elapsed,
    )
    return count


# ─────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────


async def _load_db_data() -> tuple[list[Employee], list[EmployeeRole]]:
    """Загрузить сотрудников и роли из БД."""
    async with async_session_factory() as session:
        emps = (await session.execute(select(Employee))).scalars().all()
        roles = (await session.execute(select(EmployeeRole))).scalars().all()
    return list(emps), list(roles)


def _get_role_name(
    emp: Employee,
    dept_map: dict[str, dict],
    role_by_iiko_id: dict[str, EmployeeRole],
) -> str:
    """
    Получить название должности для сотрудника.
    Берём main role из Employee.role_id (из БД).
    """
    if emp.role_id:
        role = role_by_iiko_id.get(str(emp.role_id))
        if role and role.name:
            return role.name
    return ""
