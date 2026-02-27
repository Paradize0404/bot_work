"""
Use-case: синхронизация ФОТ → FinTablo (ведомость месяца).

Логика дельта-синхронизации:
  1. Читаем лист ФОТ текущего месяца (Google Sheets)
  2. Для каждого сотрудника с заполненным FinTabID:
     a. GET текущие начисления из FinTablo (GET /v1/salary?employeeId=…&date=мм.гггг)
     b. Вычисляем дельту: нужное_значение − текущее_значение_в_FinTablo
     c. Если дельта ≠ 0 — PUT новое значение = текущее + дельта (т.е. нужное)
  3. Маппинг полей:
       ФОТ «Ставка» (D)    → totalPay.fix
       ФОТ «Бонус»  (E)    → totalPay.percent
       ФОТ «Премия» (F)    → totalPay.bonus
       ФОТ «Удержания» (G) → totalPay.forfeit

Запуск: после update_fot_sheet() в scheduler и по кнопке.
"""

import asyncio
import logging
import time
from datetime import datetime

from adapters import fintablo_api
from adapters.google_sheets import read_fot_fintab_mapping
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

    Возвращает статистику::

        {
            "total": int,       # сотрудников с FinTabID
            "updated": int,     # у кого была дельта → PUT
            "skipped": int,     # дельта = 0
            "errors": int,      # ошибки API
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

    # 1. Читаем маппинг из ФОТ
    employees = await read_fot_fintab_mapping(tab_name)
    if not employees:
        logger.info("[fintablo_sync] Нет сотрудников с FinTabID — пропускаем")
        return {"total": 0, "updated": 0, "skipped": 0, "errors": 0}

    stats = {"total": len(employees), "updated": 0, "skipped": 0, "errors": 0}

    # 2. Обработка каждого сотрудника
    for emp in employees:
        fintab_id = emp["fintab_id"]
        name = emp["name"]

        try:
            # Получаем текущие начисления из FinTablo
            salary_items = await fintablo_api.get_salary(
                employee_id=fintab_id,
                date_mm_yyyy=date_mm_yyyy,
            )

            # Ищем запись за нужный месяц
            current = _extract_current_totalpay(salary_items, date_mm_yyyy)

            # Желаемые значения из нашего ФОТ
            desired = {
                "fix": round(emp["rate"], 2),
                "percent": round(emp["bonus"], 2),
                "bonus": round(emp["premium"], 2),
                "forfeit": round(emp["deductions"], 2),
            }

            # Дельта
            delta = {k: round(desired[k] - current.get(k, 0.0), 2) for k in desired}

            if all(abs(v) < 0.01 for v in delta.values()):
                logger.debug(
                    "[fintablo_sync] %s (FT:%d) — без изменений", name, fintab_id
                )
                stats["skipped"] += 1
                continue

            # Новые значения = текущие + дельта = desired
            new_pay = {
                "fix": desired["fix"],
                "percent": desired["percent"],
                "bonus": desired["bonus"],
                "forfeit": desired["forfeit"],
            }

            logger.info(
                "[fintablo_sync] %s (FT:%d) дельта: fix=%+.0f, percent=%+.0f, "
                "bonus=%+.0f, forfeit=%+.0f",
                name,
                fintab_id,
                delta["fix"],
                delta["percent"],
                delta["bonus"],
                delta["forfeit"],
            )

            await fintablo_api.update_salary(
                employee_id=fintab_id,
                date_mm_yyyy=date_mm_yyyy,
                total_pay=new_pay,
            )
            stats["updated"] += 1

        except Exception:
            logger.exception(
                "[fintablo_sync] Ошибка синхронизации %s (FT:%d)", name, fintab_id
            )
            stats["errors"] += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "[fintablo_sync] Завершено за %.1f сек: "
        "total=%d, updated=%d, skipped=%d, errors=%d",
        elapsed,
        stats["total"],
        stats["updated"],
        stats["skipped"],
        stats["errors"],
    )
    return stats


def _extract_current_totalpay(
    salary_items: list[dict],
    date_mm_yyyy: str,
) -> dict[str, float]:
    """
    Из списка записей GET /v1/salary извлечь totalPay за нужный месяц.

    Возвращает {'fix': float, 'percent': float, 'bonus': float, 'forfeit': float}.
    Если записи нет — нули.
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
    # Нет записи — нули (FinTablo тоже вернёт нули)
    return {"fix": 0.0, "percent": 0.0, "bonus": 0.0, "forfeit": 0.0}
