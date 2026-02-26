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
from use_cases.revenue_motivation import (
    get_revenue_motivation_map,
    get_department_revenue_totals,
    get_pastry_invoice_motivation_map,
)
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

    # ── 3. Агрегация явок ПО ПОДРАЗДЕЛЕНИЯМ ──
    # emp_iiko_id → {dept_id: {"dept_name": str, "shifts": int, "hours": float}}
    emp_dept_stats: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"dept_name": "", "shifts": 0, "hours": 0.0})
    )
    # (emp_iiko_id, dept_id) → заработок за смены в этом подразделении
    emp_dept_earnings: dict[tuple[str, str], float] = defaultdict(float)

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
                        emp_dept_earnings[(emp_id, dept_id)] += shift_rate
                    elif active["sal_type"] == "почасовая":
                        emp_dept_earnings[(emp_id, dept_id)] += shift_rate * hours

    logger.info("[payroll] Уникальных сотрудников с явками: %d", len(emp_dept_stats))

    # ── 3b. Расчёт мотивации «от выручки» (по подразделениям) ──
    emp_iiko_to_fullname: dict[str, str] = {
        iiko_id: _full_name(emp) for iiko_id, emp in emp_by_iiko_id.items()
    }
    # motivation_by_dept: {full_name: {dept_name: руб.}}
    # dept_revenue_totals: {dept_name: total_revenue}
    # pastry_motivation: {full_name: {dept_name: руб.}}
    motivation_by_dept, dept_revenue_totals, pastry_result = await asyncio.gather(
        get_revenue_motivation_map(
            attendance_records=attendance_records,
            emp_iiko_to_fullname=emp_iiko_to_fullname,
            date_from=month_start,
            date_to=today,
            history_index=history_index,
        ),
        get_department_revenue_totals(
            date_from=month_start,
            date_to=today,
        ),
        get_pastry_invoice_motivation_map(
            attendance_records=attendance_records,
            emp_iiko_to_fullname=emp_iiko_to_fullname,
            date_from=month_start,
            date_to=today,
            history_index=history_index,
        ),
    )
    pastry_motivation, pastry_revenue_total = pastry_result
    logger.info(
        "[payroll] Мотивация «от выручки»: %d сотрудников с ненулёвой суммой",
        len(motivation_by_dept),
    )

    # Объединяем мотивацию «от накладных кондитерки» в motivation_by_dept
    pastry_dept_names: set[str] = set()
    for fn, depts in pastry_motivation.items():
        if fn not in motivation_by_dept:
            motivation_by_dept[fn] = {}
        for dept_name, amount in depts.items():
            motivation_by_dept[fn][dept_name] = (
                motivation_by_dept[fn].get(dept_name, 0.0) + amount
            )
            pastry_dept_names.add(dept_name)
    if pastry_motivation:
        logger.info(
            "[payroll] Мотивация «от накладных кондитерки»: "
            "%d сотрудников с ненулёвой суммой, подразделения: %s",
            len(pastry_motivation),
            pastry_dept_names,
        )
    logger.info(
        "[payroll] Выручка по подразделениям: %s",
        {k: f"{v:,.0f}" for k, v in dept_revenue_totals.items()},
    )

    # ── 4. Построение секций по подразделениям ──
    # Источник сотрудников — «История ставок».
    # Каждый сотрудник, у которого есть активная запись:
    #   • ежемесячная → секция «Администрация»
    #   • почасовая БЕЗ смен в текущем месяце → пропускаем
    #   • остальные → по подразделениям (если смены есть, иначе в «unassigned»)
    dept_sections_map: dict[str, dict] = {}

    # full_name → iiko_id (обратный индекс)
    _fn_to_iiko: dict[str, str] = {
        _full_name(emp): iiko_id
        for iiko_id, emp in emp_by_iiko_id.items()
        if _full_name(emp)
    }

    # Множество имён, которые уже обработаны в dept-секции (не дублировать в monthly)
    processed_in_dept: set[str] = set()

    for fn, history in history_index.items():
        active = get_rate_for_date(history, today)
        if not active:
            continue
        sal_type = active["sal_type"]

        if sal_type == "ежемесячная":
            continue  # → секция «Администрация»

        emp_iiko_id = _fn_to_iiko.get(fn)
        emp = emp_by_full_name.get(fn)
        if emp is None:
            continue

        dept_map = emp_dept_stats.get(emp_iiko_id, {}) if emp_iiko_id else {}
        has_shifts = bool(dept_map)

        # Без смен — не выводим (почасовая и посменная)
        if not has_shifts:
            continue

        role_name = _get_role_name(emp, dept_map, role_by_iiko_id)
        processed_in_dept.add(fn)

        # Разбиваем по подразделениям
        for dept_id, dept_info in dept_map.items():
            dept_name = dept_info["dept_name"]
            earnings = round(
                emp_dept_earnings.get((emp_iiko_id, dept_id), 0.0),
                2,
            )
            bonus = round(
                (motivation_by_dept.get(fn) or {}).get(dept_name, 0.0),
                2,
            )

            if dept_id not in dept_sections_map:
                dept_sections_map[dept_id] = {
                    "dept_name": dept_name,
                    "employees": [],
                }

            dept_sections_map[dept_id]["employees"].append(
                {
                    "name": _display_name(emp),
                    "role": role_name,
                    "rate_total": earnings,
                    "bonus": bonus,
                }
            )

    # ── 5. Секция «Администрация» (ежемесячные сотрудники) ──
    monthly_section: list[dict] = []
    monthly_names: set[str] = set()
    for fn, hist in history_index.items():
        active = get_rate_for_date(hist, today)
        if active and active["sal_type"] == "ежемесячная":
            monthly_names.add(fn)

    for fn in monthly_names:
        history = history_index.get(fn, [])
        active = get_rate_for_date(history, today)
        if active:
            rate = float(active["rate"])
        else:
            rate = 0.0

        emp = emp_by_full_name.get(fn)
        if emp is None:
            logger.debug("[payroll] Ежемесячный сотрудник «%s» не найден в БД", fn)
            continue

        role_name = ""
        if emp.role_id and str(emp.role_id) in role_by_iiko_id:
            role_name = role_by_iiko_id[str(emp.role_id)].name or ""

        if history:
            total = get_prorated_monthly(history, month_start, today, days_in_m)
        else:
            total = round(rate / days_in_m * days_passed, 2) if days_in_m else 0.0

        bonus = round(sum((motivation_by_dept.get(fn) or {}).values()), 2)

        monthly_section.append(
            {
                "name": _display_name(emp),
                "role": role_name,
                "rate_total": round(total, 2),
                "bonus": bonus,
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
        dept_revenue=dept_revenue_totals,
        pastry_revenue=pastry_revenue_total,
        pastry_dept_names=pastry_dept_names,
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
