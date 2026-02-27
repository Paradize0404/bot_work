"""
Use-case: синхронизация ФОТ → FinTablo (ведомость месяца).

Логика дельта-синхронизации:
  1. Читаем «Маппинг FinTablo» — постоянный маппинг сотрудников iiko → FinTablo ID
  2. Читаем лист ФОТ текущего месяца — итоговые начисления по каждому сотруднику
     (суммируются по всем секциям, если сотрудник встречается в нескольких)
  3. Для каждого iiko-сотрудника из маппинга:
       - Берём N строк маппинга для него (N ≥ 1)
       - Каждому FinTablo-ID отправляем salary_field / N
  4. Delta-sync:
       a. GET текущие начисления из FinTablo
       b. Если нужное значение ≠ текущее — PUT новое (= нужное)
  5. Поля:
       ФОТ «Ставка» (D)    → totalPay.fix
       ФОТ «Бонус»  (E)    → totalPay.percent
       ФОТ «Премия» (F)    → totalPay.bonus
       ФОТ «Удержания» (G) → totalPay.forfeit

Запуск: после update_fot_sheet() в scheduler и по кнопке «Синхр. зарплату».
"""

import asyncio
import logging
import time
from datetime import datetime

from adapters import fintablo_api
from adapters.google_sheets import (
    read_fintab_employee_mapping,
    read_fot_all_employees,
)
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

# Названия месяцев (именительный падеж) для имени вкладки
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


async def sync_fot_to_fintablo(
    triggered_by: str | None = None,
) -> dict:
    """
    Синхронизировать начисления из листа ФОТ в FinTablo.

    Использует постоянный маппинг из вкладки «Маппинг FinTablo».
    Сотрудники с несколькими строками в маппинге получают salary ÷ N.

    Возвращает статистику::

        {
            "total": int,       # записей PUT (FinTablo ID × месяц)
            "updated": int,     # у кого была дельта → PUT
            "skipped": int,     # дельта = 0
            "errors": int,      # ошибки API
            "no_fot": int,      # в маппинге, но нет строки в ФОТ
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
        return {"total": 0, "updated": 0, "skipped": 0, "errors": 0, "no_fot": 0}

    # 2. Группируем маппинг по iiko-имени: {iiko_name: [fintab_id, ...]}
    grouped: dict[str, list[int]] = {}
    for row in mapping_rows:
        grouped.setdefault(row["iiko_name"], []).append(row["fintab_id"])

    stats = {"total": 0, "updated": 0, "skipped": 0, "errors": 0, "no_fot": 0}

    # 3. Обрабатываем каждого iiko-сотрудника
    for iiko_name, ft_ids in grouped.items():
        emp_fot = fot_data.get(iiko_name)
        if not emp_fot:
            logger.warning(
                "[fintablo_sync] «%s» есть в маппинге, но не найден в ФОТ «%s»",
                iiko_name,
                tab_name,
            )
            stats["no_fot"] += 1
            continue

        n = len(ft_ids)  # делитель для splitting (для admin > 1)

        desired_full = {
            "fix": round(emp_fot["rate"], 2),
            "percent": round(emp_fot["bonus"], 2),
            "bonus": round(emp_fot["premium"], 2),
            "forfeit": round(emp_fot["deductions"], 2),
        }
        # Каждый FT-ID получает 1/N
        desired_split = {k: round(v / n, 2) for k, v in desired_full.items()}

        for fintab_id in ft_ids:
            stats["total"] += 1
            try:
                salary_items = await fintablo_api.get_salary(
                    employee_id=fintab_id,
                    date_mm_yyyy=date_mm_yyyy,
                )
                current = _extract_current_totalpay(salary_items, date_mm_yyyy)
                delta = {
                    k: round(desired_split[k] - current.get(k, 0.0), 2)
                    for k in desired_split
                }

                if all(abs(v) < 0.01 for v in delta.values()):
                    logger.debug(
                        "[fintablo_sync] %s (FT:%d) — без изменений",
                        iiko_name,
                        fintab_id,
                    )
                    stats["skipped"] += 1
                    continue

                logger.info(
                    "[fintablo_sync] %s (FT:%d, доля 1/%d) "
                    "дельта: fix=%+.0f, percent=%+.0f, bonus=%+.0f, forfeit=%+.0f",
                    iiko_name,
                    fintab_id,
                    n,
                    delta["fix"],
                    delta["percent"],
                    delta["bonus"],
                    delta["forfeit"],
                )

                await fintablo_api.update_salary(
                    employee_id=fintab_id,
                    date_mm_yyyy=date_mm_yyyy,
                    total_pay=desired_split,
                )
                stats["updated"] += 1

            except Exception:
                logger.exception(
                    "[fintablo_sync] Ошибка: «%s» (FT:%d)",
                    iiko_name,
                    fintab_id,
                )
                stats["errors"] += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "[fintablo_sync] Завершено за %.1f сек: "
        "total=%d, updated=%d, skipped=%d, errors=%d, no_fot=%d",
        elapsed,
        stats["total"],
        stats["updated"],
        stats["skipped"],
        stats["errors"],
        stats["no_fot"],
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
