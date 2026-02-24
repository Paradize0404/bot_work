"""
Use-case: расчёт мотивации «от выручки» через OLAP-отчёт и явки iiko.

Логика:
  1. Для каждого сотрудника с активной записью в «Истории ставок»
     где mot_base = "от выручки" и mot_pct > 0:
       a. Берём его явки за расчётный период (из attendance_records).
       b. Определяем уникальные пары (дата смены, подразделение).
       c. Для каждой пары берём выручку (DishDiscountSumInt) из OLAP-отчёта
          «Отчет по мотивации БОТ» (пресет df94cd59-…).
       d. Мотивация = Σ(выручка) × mot_pct / 100.
  2. Возвращаем dict: employee_full_name → motivation_amount (руб.)

OLAP-отчёт:
  Название:  «Отчет по мотивации БОТ»
  Id:        df94cd59-2cd8-4608-86bd-6267e1ea8cc6
  Метрика:   DishDiscountSumInt (сумма со скидкой, MONEY)
  Строки:    CloseTime (время закрытия, DATETIME)
  Столбцы:   Department (торговое предприятие, STRING)
"""

import logging
from collections import defaultdict
from datetime import date
from typing import Optional

from adapters.iiko_api import fetch_motivation_revenue_olap
from use_cases.salary_history import load_salary_history_index, get_rate_for_date

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Построение индекса выручки из OLAP
# ═══════════════════════════════════════════════════════


def _build_revenue_index(olap_rows: list[dict]) -> dict[tuple[str, str], float]:
    """
    Преобразовать строки OLAP-отчёта в индекс:
      (date_str "YYYY-MM-DD", dept_name) → DishDiscountSumInt (руб.)

    OLAP может вернуть несколько строк на одну пару (дата, подразделение)
    в случае разных часовых поясов или промежуточных итогов — суммируем.
    """
    index: dict[tuple[str, str], float] = defaultdict(float)
    for row in olap_rows:
        close_time_raw = str(row.get("CloseTime") or "").strip()
        dept = str(row.get("Department") or "").strip()
        revenue_raw = row.get("DishDiscountSumInt", 0)

        if not close_time_raw or not dept:
            continue

        # Берём только дату (yyyy-mm-dd) из CloseTime
        date_str = close_time_raw[:10]
        try:
            revenue = float(revenue_raw) if revenue_raw else 0.0
        except (ValueError, TypeError):
            revenue = 0.0

        if revenue > 0:
            index[(date_str, dept)] += revenue

    logger.debug(
        "[revenue_motivation] OLAP-индекс построен: %d уникальных (дата, подразд.)",
        len(index),
    )
    return dict(index)


# ═══════════════════════════════════════════════════════
# Основной расчёт
# ═══════════════════════════════════════════════════════


async def calculate_revenue_motivation(
    attendance_records: list[dict],
    emp_full_names: dict[str, str],  # iiko_id → full_name
    date_from: date,
    date_to: date,
    history_index: Optional[dict[str, list[dict]]] = None,
) -> dict[str, float]:
    """
    Рассчитать мотивацию «от выручки» для всех сотрудников за период.

    Параметры:
      attendance_records — список явок из fetch_attendance() за период
      emp_full_names     — iiko_id (str) → «Фамилия Имя Отчество» (для поиска в истории)
      date_from / date_to — расчётный период (date-объекты)
      history_index      — кэш истории ставок (если None — загружается из БД)

    Возвращает:
      { "Фамилия И.О.": <motivation_rubles>, ... }
      Только сотрудники с mot_base="от выручки" и mot_pct>0.
    """
    t_from_str = date_from.strftime("%Y-%m-%d")
    t_to_str = date_to.strftime("%Y-%m-%d")

    # ── 1. Загружаем OLAP-отчёт и историю ставок параллельно ──
    import asyncio

    if history_index is None:
        olap_rows, history_index = await asyncio.gather(
            fetch_motivation_revenue_olap(t_from_str, t_to_str),
            load_salary_history_index(),
        )
    else:
        olap_rows = await fetch_motivation_revenue_olap(t_from_str, t_to_str)

    logger.info(
        "[revenue_motivation] OLAP строк: %d, сотрудников в истории: %d",
        len(olap_rows),
        len(history_index),
    )

    # ── 2. Строим индекс выручки (дата, подразделение) → руб. ──
    revenue_index = _build_revenue_index(olap_rows)

    if not revenue_index:
        logger.warning(
            "[revenue_motivation] OLAP вернул пустой результат за %s — %s",
            t_from_str,
            t_to_str,
        )
        return {}

    # ── 3. Индексируем явки по iiko_id ──
    # emp_iiko_id → [(shift_date_str, dept_name), ...]
    emp_shifts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for rec in attendance_records:
        emp_id = (rec.get("employeeId") or "").strip()
        dept_name = (rec.get("departmentName") or "").strip()
        date_from_raw = (rec.get("dateFrom") or "").strip()
        if not emp_id or not dept_name or not date_from_raw:
            continue
        shift_date_str = date_from_raw[:10]  # "YYYY-MM-DD"
        emp_shifts[emp_id].add((shift_date_str, dept_name))

    # ── 4. Для каждого сотрудника рассчитываем мотивацию ──
    result: dict[str, float] = {}

    for emp_iiko_id, full_name in emp_full_names.items():
        if not full_name:
            continue

        history = history_index.get(full_name, [])
        if not history:
            continue

        # Получаем активную запись на конец периода (последняя ставка за период)
        # Если ставка менялась в течение периода — используем текущую (упрощение,
        # т.к. процент мотивации обычно не меняется внутри месяца).
        active = get_rate_for_date(history, date_to)
        if active is None:
            # Попробуем на дату начала периода
            active = get_rate_for_date(history, date_from)
        if active is None:
            continue

        mot_base = (active.get("mot_base") or "").strip()
        mot_pct = active.get("mot_pct")

        if mot_base != "от выручки" or not mot_pct or float(mot_pct) <= 0:
            continue

        mot_pct_val = float(mot_pct)

        # Явки сотрудника за период
        shifts = emp_shifts.get(emp_iiko_id, set())
        if not shifts:
            logger.debug(
                "[revenue_motivation] %s (id=%s): нет явок за период, мотивация=0",
                full_name,
                emp_iiko_id,
            )
            continue

        # Суммируем выручку по уникальным (дата, подразделение) смены сотрудника
        total_revenue = 0.0
        matched_pairs: list[tuple[str, str, float]] = []
        for shift_date, dept_name in sorted(shifts):
            # Прямое совпадение по имени подразделения
            rev = revenue_index.get((shift_date, dept_name), 0.0)
            if rev <= 0:
                # Попытка нечёткого поиска: ищем dept_name как подстроку ключей
                for (d, dep), v in revenue_index.items():
                    if d == shift_date and (
                        dept_name.lower() in dep.lower()
                        or dep.lower() in dept_name.lower()
                    ):
                        rev = v
                        break
            if rev > 0:
                total_revenue += rev
                matched_pairs.append((shift_date, dept_name, rev))

        if total_revenue <= 0:
            logger.debug(
                "[revenue_motivation] %s: выручка=0 за период "
                "(явок %d, подразд.: %s)",
                full_name,
                len(shifts),
                {d for _, d in shifts},
            )
            continue

        motivation = round(total_revenue * mot_pct_val / 100.0, 2)
        result[full_name] = motivation

        logger.info(
            "[revenue_motivation] %s: выручка=%.2f × %.2f%% = %.2f руб. "
            "(%d смен, %d дней совпало)",
            full_name,
            total_revenue,
            mot_pct_val,
            motivation,
            len(shifts),
            len(matched_pairs),
        )

    logger.info(
        "[revenue_motivation] Итог: мотивация «от выручки» для %d сотрудников",
        len(result),
    )
    return result


# ═══════════════════════════════════════════════════════
# Вспомогательный публичный метод для payroll
# ═══════════════════════════════════════════════════════


async def get_revenue_motivation_map(
    attendance_records: list[dict],
    emp_iiko_to_fullname: dict[str, str],
    date_from: date,
    date_to: date,
    history_index: Optional[dict[str, list[dict]]] = None,
) -> dict[str, float]:
    """
    Тонкая обёртка над calculate_revenue_motivation.

    Безопасно: при любой ошибке API возвращает {} (не ломает ФОТ).

    Возвращает: full_name → motivation_rubles
    """
    try:
        return await calculate_revenue_motivation(
            attendance_records=attendance_records,
            emp_full_names=emp_iiko_to_fullname,
            date_from=date_from,
            date_to=date_to,
            history_index=history_index,
        )
    except Exception:
        logger.exception(
            "[revenue_motivation] Ошибка при расчёте мотивации от выручки, "
            "возвращаю пустой результат"
        )
        return {}
