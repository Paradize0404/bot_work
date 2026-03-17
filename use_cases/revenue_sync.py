"""
Use-case: синхронизация выручки (OLAP продажи → FinTablo PnL).

Логика:
  1. Загрузить OLAP-отчёт продаж из iiko (пресет «Выручка себестоимость бот»)
  2. Считать маппинг из Google Sheets:
     - Тип оплаты → Статья FinTablo (колонки J-K) — приоритет 1
     - Место приготовления → Статья FinTablo (колонки H-I) — приоритет 2
     - Подразделение → Направление (колонки D-E)
  3. Для каждой строки OLAP:
     a. Определить direction по Department (D-E)
     b. Если PayType замаплен (J-K) → использовать эту категорию
     c. Иначе если CookingPlaceType замаплен (H-I) → использовать эту
     d. Иначе → unmapped
  4. Агрегировать по (ft_pnl_category_id, direction_id)
  5. Синхронизировать в FinTablo (бот-записи помечены comment="iiko-bot-revenue")
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from adapters import fintablo_api
from adapters.iiko_api import fetch_olap_by_preset
from adapters.google_sheets import read_fintab_all_mappings
from db.engine import async_session_factory as async_session
from db.ft_models import FTDirection
from use_cases._helpers import now_kgd
from use_cases.pnl_sync import _resolve_department_direction

logger = logging.getLogger(__name__)

# Пресет «Выручка себестоимость бот» (тот же, что в day_report)
SALES_PRESET_ID = "96df1c31-a77f-4b7c-94db-55db656aae6a"

# Метка для PnL-записей выручки, созданных ботом
BOT_COMMENT_REVENUE = "iiko-bot-revenue"


# ═══════════════════════════════════════════════════════
# Вспомогательные функции
# ═══════════════════════════════════════════════════════


async def _get_direction_map() -> dict[str, int]:
    """Вернуть {direction_name: direction_id} из ft_direction."""
    async with async_session() as session:
        stmt = select(FTDirection.name, FTDirection.id).where(
            FTDirection.name.isnot(None)
        )
        rows = (await session.execute(stmt)).all()
    return {name: did for name, did in rows if name}


# ═══════════════════════════════════════════════════════
# Основная логика: обновление выручки в FinTablo
# ═══════════════════════════════════════════════════════


async def update_revenue(
    *,
    triggered_by: str | None = None,
    target_date: datetime | None = None,
) -> dict:
    """
    Обновить выручку в FinTablo по данным продаж из iiko.

    Алгоритм:
      1. Определить месяц (target_date или текущий)
      2. Загрузить OLAP-отчёт продаж из iiko за весь месяц
      3. Считать маппинг из Google Sheets (J-K для типов оплат, H-I для мест, D-E для направлений)
      4. Для каждой строки:
         - Определить direction по Department (D-E)
         - Если PayType замаплен (J-K) → использовать эту категорию
         - Иначе если CookingPlaceType замаплен (H-I) → использовать эту
         - Иначе → unmapped
      5. Агрегировать по (ft_pnl_category_id, direction_id)
      6. Синхронизировать в FinTablo

    Возвращает::

        {
            "updated": int,
            "skipped": int,
            "errors": int,
            "details": list[str],
            "unmapped_keys": list[str],
            "elapsed": float,
        }
    """
    t0 = time.monotonic()
    now = target_date or now_kgd()
    date_mm_yyyy = now.strftime("%m.%Y")
    trigger_label = triggered_by or "manual"

    logger.info(
        "[revenue_sync] Старт выручки за %s (triggered_by=%s)",
        date_mm_yyyy,
        trigger_label,
    )

    # ── 1. Период: весь месяц ──
    first_day = now.replace(day=1)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    date_from = first_day.strftime("%Y-%m-%dT00:00:00")
    date_to = next_month.strftime("%Y-%m-%dT00:00:00")

    # ── 2. Загрузить OLAP данные продаж ──
    olap_rows = await fetch_olap_by_preset(SALES_PRESET_ID, date_from, date_to)

    # Парсинг строк
    parsed: list[dict] = []
    for row in olap_rows:
        raw_amount = row.get("DishDiscountSumInt", 0) or 0
        try:
            amount = float(raw_amount)
        except (ValueError, TypeError):
            amount = 0.0
        if amount <= 0:
            continue

        pay_type = (row.get("PayTypes") or "").strip()
        cooking_place = (row.get("CookingPlaceType") or "").strip()
        department = (row.get("Department") or "").strip()

        if not pay_type and not cooking_place:
            continue

        parsed.append(
            {
                "pay_type": pay_type,
                "cooking_place": cooking_place,
                "department": department,
                "amount": amount,
            }
        )

    total_incoming = sum(r["amount"] for r in parsed)
    logger.info(
        "[revenue_sync] OLAP: %d строк, total=%.2f",
        len(parsed),
        total_incoming,
    )

    # ── 3. Маппинг из GSheets ──
    all_maps = await read_fintab_all_mappings()
    revenue_map = all_maps.get("revenue", [])  # H-I: CookingPlaceType → FT
    pay_type_map = all_maps.get("pay_type", [])  # J-K: PayType → FT
    dept_dir_map = all_maps.get("dept_direction", [])

    direction_map = await _get_direction_map()

    # Индексы маппинга
    pt_index: dict[str, tuple[int, str]] = {
        m["pay_type_name"]: (m["ft_pnl_category_id"], m["ft_pnl_category_name"])
        for m in pay_type_map
    }
    cp_index: dict[str, tuple[int, str]] = {
        m["cooking_place_type"]: (m["ft_pnl_category_id"], m["ft_pnl_category_name"])
        for m in revenue_map
    }

    if not pt_index and not cp_index:
        logger.warning("[revenue_sync] Маппинг выручки пуст (нет J-K и H-I)")
        return {
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "details": ["Маппинг выручки пуст"],
            "unmapped_keys": [],
            "unmapped_sums": {},
            "total_incoming": round(total_incoming, 2),
            "total_allocated": 0,
            "total_unmapped": round(total_incoming, 2),
            "elapsed": round(time.monotonic() - t0, 1),
            "month": date_mm_yyyy,
        }

    # ── 4. Агрегация ──
    ft_totals: dict[tuple[int, int | None], float] = defaultdict(float)
    ft_names: dict[int, str] = {}
    unmapped_keys: set[str] = set()
    unmapped_sums: dict[str, float] = defaultdict(float)

    for row in parsed:
        pay_type = row["pay_type"]
        cooking_place = row["cooking_place"]
        department = row["department"]
        amount = row["amount"]

        # Direction
        dir_name = (
            _resolve_department_direction(department, dept_dir_map)
            if department
            else None
        )
        dir_id = direction_map.get(dir_name) if dir_name else None

        # Приоритет: PayType (J-K) > CookingPlaceType (H-I)
        cat_id: int | None = None
        cat_name: str = ""

        if pay_type and pay_type in pt_index:
            cat_id, cat_name = pt_index[pay_type]
        elif cooking_place and cooking_place in cp_index:
            cat_id, cat_name = cp_index[cooking_place]

        if cat_id is None:
            key = pay_type or cooking_place or "empty"
            unmapped_keys.add(key)
            unmapped_sums[key] += amount
            continue

        ft_totals[(cat_id, dir_id)] += amount
        ft_names[cat_id] = cat_name

    # Округление
    for key in ft_totals:
        ft_totals[key] = round(ft_totals[key], 2)
    ft_totals = {k: v for k, v in ft_totals.items() if v > 0.005}

    total_allocated = sum(ft_totals.values())
    total_unmapped = sum(unmapped_sums.values())

    logger.info(
        "[revenue_sync] Агрегация: %d записей FT, allocated=%.2f, unmapped=%.2f (%d keys)",
        len(ft_totals),
        total_allocated,
        total_unmapped,
        len(unmapped_keys),
    )

    # ── 5. Синхронизация в FinTablo (параллельно) ──
    updated = 0
    skipped = 0
    errors = 0
    details: list[str] = []

    tasks: list[tuple[int, int | None, float, str]] = []
    for (cat_id, direction_id), total in ft_totals.items():
        cat_name = ft_names.get(cat_id, f"ID:{cat_id}")
        dir_label = ""
        if direction_id:
            dir_label = next(
                (n for n, d in direction_map.items() if d == direction_id), ""
            )
        display_name = f"{cat_name} / {dir_label}" if dir_label else cat_name
        tasks.append((cat_id, direction_id, total, display_name))

    async def _do_sync(
        cat_id: int,
        direction_id: int | None,
        iiko_total: float,
        display_name: str,
    ) -> tuple[str, str]:
        """Возвращает (status, display_name)."""
        try:
            result = await _sync_one_revenue_category(
                cat_id,
                display_name,
                iiko_total,
                date_mm_yyyy,
                direction_id=direction_id,
            )
            return (result, display_name if result != "skipped" else "")
        except Exception:
            logger.exception(
                "[revenue_sync] Ошибка для %s (id=%d, dir=%s)",
                display_name,
                cat_id,
                direction_id,
            )
            return ("error", display_name)

    results = await asyncio.gather(
        *[_do_sync(cid, did, total, dn) for cid, did, total, dn in tasks]
    )

    for (cid, did, total, dn), (status, _) in zip(tasks, results):
        if status == "updated":
            updated += 1
            details.append(f"✅ {dn}: {total:.2f}")
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors += 1
            details.append(f"❌ {dn}: ошибка")
        else:
            details.append(f"ℹ️ {dn}: {status}")

    elapsed = time.monotonic() - t0

    logger.info(
        "[revenue_sync] Выручка за %s завершена: upd=%d, skip=%d, err=%d, %.1f сек",
        date_mm_yyyy,
        updated,
        skipped,
        errors,
        elapsed,
    )

    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "details": details,
        "unmapped_keys": sorted(unmapped_keys),
        "unmapped_sums": dict(unmapped_sums),
        "total_incoming": round(total_incoming, 2),
        "total_allocated": round(total_allocated, 2),
        "total_unmapped": round(total_unmapped, 2),
        "elapsed": round(elapsed, 1),
        "month": date_mm_yyyy,
    }


# ═══════════════════════════════════════════════════════
# Синхронизация одной FT-категории
# ═══════════════════════════════════════════════════════


async def _sync_one_revenue_category(
    cat_id: int,
    cat_name: str,
    iiko_total: float,
    date_mm_yyyy: str,
    *,
    direction_id: int | None = None,
) -> str:
    """
    Синхронизировать одну FT PnL-категорию для выручки.

    Логика (аналогична pnl_sync._sync_one_category):
      - Получить текущие PnL-записи за месяц (+ direction)
      - Подсчитать сумму бот-записей (comment=BOT_COMMENT_REVENUE)
      - Рассчитать дельту: desired = iiko_total - other_total
      - Удалить старые бот-записи, создать новую

    Возвращает: "updated", "skipped" или текст описания.
    """
    existing_items = await fintablo_api.fetch_pnl_items(
        category_id=cat_id,
        date_mm_yyyy=date_mm_yyyy,
        direction_id=direction_id,
    )

    bot_items = [
        it
        for it in existing_items
        if (it.get("comment") or "").startswith(BOT_COMMENT_REVENUE)
    ]
    other_items = [
        it
        for it in existing_items
        if not (it.get("comment") or "").startswith(BOT_COMMENT_REVENUE)
    ]

    ft_bot_total = sum(float(it.get("value", 0)) for it in bot_items)
    ft_other_total = sum(float(it.get("value", 0)) for it in other_items)

    desired_bot_value = round(iiko_total - ft_other_total, 2)

    if desired_bot_value <= 0 and not bot_items:
        logger.debug(
            "[revenue_sync] %s: FT other=%.2f >= iiko=%.2f, пропуск",
            cat_name,
            ft_other_total,
            iiko_total,
        )
        return "skipped"

    if abs(round(ft_bot_total, 2) - desired_bot_value) < 0.01:
        logger.debug("[revenue_sync] %s: delta=0, пропуск", cat_name)
        return "skipped"

    logger.info(
        "[revenue_sync] %s: iiko=%.2f, ft_other=%.2f, ft_bot=%.2f → desired=%.2f",
        cat_name,
        iiko_total,
        ft_other_total,
        ft_bot_total,
        desired_bot_value,
    )

    # Удаляем старые бот-записи
    for item in bot_items:
        item_id = item.get("id")
        if item_id:
            await fintablo_api.delete_pnl_item(int(item_id))
            logger.debug("[revenue_sync] Удалена бот-запись id=%s", item_id)

    # Создаём новую бот-запись (если сумма > 0)
    if desired_bot_value > 0:
        await fintablo_api.create_pnl_item(
            category_id=cat_id,
            value=desired_bot_value,
            date_mm_yyyy=date_mm_yyyy,
            comment=BOT_COMMENT_REVENUE,
            direction_id=direction_id,
        )
        logger.info(
            "[revenue_sync] %s: создана запись %.2f за %s (dir=%s)",
            cat_name,
            desired_bot_value,
            date_mm_yyyy,
            direction_id,
        )

    return "updated"
