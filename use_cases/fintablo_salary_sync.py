"""
Use-case: синхронизация ФОТ → FinTablo (ведомость месяца).

Схема распределения ФОТ по направлениям FinTablo
─────────────────────────────────────────────────
Каждая строка «Маппинга FinTablo» связывает iiko-сотрудника с одним
FinTablo-сотрудником через два поля:

  col A  →  iiko UUID  (идентификатор сотрудника в iiko)
  col B  →  FinTablo ID  (числовой ID в FinTablo)
  col D  →  Подразделение ФОТ  (секция в листе ФОТ; может быть пустой)

Правила начисления:

  1. Производственный персонал (почасовые/посменные)
     col D заполнен → берём ТОЧНУЮ сумму из соответствующей секции ФОТ.
     Пример: повар заработал 60 000 в «Клинической» и 40 000 в «Аксаковой».
     Маппинг:
       Повар (uuid) | FT_klin | | Клиническая | Клиническая  → fix=60 000
       Повар (uuid) | FT_aks  | | Аксакова    | Аксакова     → fix=40 000

  2. Административный / управляющий персонал (ежемесячные)
     col D пустой → общая сумма делится ПОРОВНУ между всеми такими строками.
     Пример: управляющий 80 000 → 4 направления → 20 000 каждому.
     Маппинг:
       Управляющий (uuid) | FT_klin | | | Клиническая  → fix=20 000
       Управляющий (uuid) | FT_aks  | | | Аксакова     → fix=20 000
       Управляющий (uuid) | FT_selm | | | Сельма       → fix=20 000
       Управляющий (uuid) | FT_prod | | | Производство → fix=20 000

  3. Смешанный: строки с col D берут свою секцию; строки без col D
     делят total поровну.

Delta-sync:
  GET текущие начисления → если отличается → PUT новые.

Поля:
  ФОТ «Ставка» (D)      → totalPay.fix
  ФОТ «Бонус»  (E)      → totalPay.percent
  ФОТ «Премия» (F)      → totalPay.bonus
  ФОТ «Удержания» (G)   → totalPay.forfeit

Запуск: после update_fot_sheet() в scheduler и по кнопке «Синхр. зарплату».
"""

import asyncio
import logging
import time

from adapters import fintablo_api
from adapters.google_sheets import (
    read_fintab_employee_mapping,
    read_fot_all_employees,
)
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

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

_ZERO_PAY: dict[str, float] = {
    "fix": 0.0,
    "percent": 0.0,
    "bonus": 0.0,
    "forfeit": 0.0,
}

# Поля ФОТ → поля FinTablo totalPay
_FOT_TO_FT = {
    "rate": "fix",
    "bonus": "percent",
    "premium": "bonus",
    "deductions": "forfeit",
}


def _fot_to_totalpay(fot: dict) -> dict[str, float]:
    """Конвертировать dict ФОТ {rate, bonus, premium, deductions} → totalPay."""
    return {
        ft_key: round(fot.get(fot_key, 0.0), 2)
        for fot_key, ft_key in _FOT_TO_FT.items()
    }


def _split_evenly(fot: dict, n: int) -> dict[str, float]:
    """Разделить суммы поровну на n частей."""
    if n <= 0:
        return dict(_ZERO_PAY)
    return {k: round(v / n, 2) for k, v in _fot_to_totalpay(fot).items()}


async def sync_fot_to_fintablo(
    triggered_by: str | None = None,
) -> dict:
    """
    Синхронизировать начисления из листа ФОТ в FinTablo.

    Возвращает статистику::

        {
            "total": int,    # записей обработано
            "updated": int,  # отправлено PUT
            "skipped": int,  # без изменений
            "errors": int,   # ошибки API
            "no_fot": int,   # iiko_id не найден в ФОТ
            "no_dept": int,  # col D заполнен, но секции нет в ФОТ
        }
    """
    t0 = time.monotonic()
    today = now_kgd().date()

    month_name = _MONTH_NAMES_RU[today.month]
    tab_name = f"ФОТ {month_name} {today.year}"
    date_mm_yyyy = f"{today.month:02d}.{today.year}"

    logger.info(
        "[fintablo_sync] Старт: вкладка «%s», период %s, triggered_by=%s",
        tab_name,
        date_mm_yyyy,
        triggered_by,
    )

    # 1. Читаем маппинг и ФОТ параллельно
    mapping_rows, fot_data = await asyncio.gather(
        read_fintab_employee_mapping(),
        read_fot_all_employees(tab_name),
    )

    if not mapping_rows:
        logger.info("[fintablo_sync] Маппинг пуст — пропускаем")
        return {
            "total": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "no_fot": 0,
            "no_dept": 0,
        }

    # 2. Группируем по iiko UUID → [{fintab_id, fintab_name, fot_dept}, …]
    grouped: dict[str, list[dict]] = {}
    for row in mapping_rows:
        grouped.setdefault(row["iiko_id"], []).append(
            {
                "fintab_id": row["fintab_id"],
                "fintab_name": row.get("fintab_name", ""),
                "fot_dept": row.get("fot_dept", ""),
            }
        )

    stats = {
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "no_fot": 0,
        "no_dept": 0,
    }

    # 3. Обрабатываем каждого сотрудника
    for iiko_id, entries in grouped.items():
        emp_data = fot_data.get(iiko_id)
        if not emp_data:
            logger.warning(
                "[fintablo_sync] iiko_id=%s есть в маппинге,"
                " но не найден в ФОТ «%s»",
                iiko_id,
                tab_name,
            )
            stats["no_fot"] += 1
            continue

        total_fot = emp_data["total"]
        by_dept = emp_data.get("by_dept", {})

        with_dept = [e for e in entries if e["fot_dept"]]
        without_dept = [e for e in entries if not e["fot_dept"]]

        # Записи с указанной секцией → точная сумма из этой секции ФОТ
        tasks: list[tuple[dict, dict[str, float]]] = []
        for entry in with_dept:
            dept_name = entry["fot_dept"]
            dept_fot = by_dept.get(dept_name)
            if dept_fot is None:
                logger.warning(
                    "[fintablo_sync] iiko_id=%s FT:%d — секция «%s» отсутствует"
                    " в ФОТ (есть: %s)",
                    iiko_id,
                    entry["fintab_id"],
                    dept_name,
                    ", ".join(by_dept.keys()) or "—",
                )
                stats["no_dept"] += 1
                tasks.append((entry, dict(_ZERO_PAY)))
            else:
                tasks.append((entry, _fot_to_totalpay(dept_fot)))

        # Записи без секции → делим total поровну
        n_no_dept = len(without_dept)
        for entry in without_dept:
            tasks.append((entry, _split_evenly(total_fot, n_no_dept)))

        # 4. Delta-sync
        for entry, desired in tasks:
            fintab_id = entry["fintab_id"]
            ft_name = entry["fintab_name"] or iiko_id
            stats["total"] += 1
            try:
                salary_items = await fintablo_api.get_salary(
                    employee_id=fintab_id,
                    date_mm_yyyy=date_mm_yyyy,
                )
                current = _extract_current_totalpay(salary_items, date_mm_yyyy)
                delta = {k: round(desired[k] - current.get(k, 0.0), 2) for k in desired}

                if all(abs(v) < 0.01 for v in delta.values()):
                    logger.debug(
                        "[fintablo_sync] «%s» (FT:%d) — без изменений",
                        ft_name,
                        fintab_id,
                    )
                    stats["skipped"] += 1
                    continue

                dept_label = entry["fot_dept"] or f"÷{n_no_dept}"
                logger.info(
                    "[fintablo_sync] «%s» (FT:%d секция=%s)"
                    " дельта fix=%+.0f percent=%+.0f bonus=%+.0f forfeit=%+.0f",
                    ft_name,
                    fintab_id,
                    dept_label,
                    delta["fix"],
                    delta["percent"],
                    delta["bonus"],
                    delta["forfeit"],
                )

                await fintablo_api.update_salary(
                    employee_id=fintab_id,
                    date_mm_yyyy=date_mm_yyyy,
                    total_pay=desired,
                )
                stats["updated"] += 1

            except Exception:
                logger.exception(
                    "[fintablo_sync] Ошибка: «%s» (FT:%d)",
                    ft_name,
                    fintab_id,
                )
                stats["errors"] += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "[fintablo_sync] Завершено за %.1f сек:"
        " total=%d updated=%d skipped=%d errors=%d no_fot=%d no_dept=%d",
        elapsed,
        stats["total"],
        stats["updated"],
        stats["skipped"],
        stats["errors"],
        stats["no_fot"],
        stats["no_dept"],
    )
    return stats


def _extract_current_totalpay(
    salary_items: list[dict],
    date_mm_yyyy: str,
) -> dict[str, float]:
    """
    Из списка записей GET /v1/salary извлечь totalPay за нужный месяц.

    Возвращает {'fix': float, 'percent': float, 'bonus': float, 'forfeit': float}.
    """
    for item in salary_items:
        if item.get("date") == date_mm_yyyy:
            tp = item.get("totalPay") or {}
            return {
                "fix": float(tp.get("fix") or 0),
                "percent": float(tp.get("percent") or 0),
                "bonus": float(tp.get("bonus") or 0),
                "forfeit": float(tp.get("forfeit") or 0),
            }
    return {"fix": 0.0, "percent": 0.0, "bonus": 0.0, "forfeit": 0.0}
