"""
Use-case: проверка минимальных остатков по подразделениям (ресторанам).

Логика (v2 — суммирование по департаменту):
  1. Из iiko_product.raw_json берём storeBalanceLevels —
     {storeId, minBalanceLevel, maxBalanceLevel}.
  2. По storeId определяем department (Store.parent_id).
  3. **Суммируем** фактические остатки из iiko_stock_balance
     по ВСЕМ складам одного department для каждого продукта.
  4. Если один продукт имеет min на нескольких складах одного dept —
     берём MAX(minBalanceLevel) (обычно min задан только на одном,
     но на случай дублей).
  5. Сравниваем суммарный остаток с minBalanceLevel.
  6. Результат фильтруется по department_id пользователя.

Зачем суммировать:
  - Молоко может приходоваться на кухню, а списываться с бара.
  - minBalanceLevel задан только на баре, а товар лежит на обоих складах.
  - Только суммирование показывает реальную картину.

Зависимости (таблицы):
  - iiko_product       — raw_json содержит storeBalanceLevels
  - iiko_stock_balance — фактические остатки по store/product
  - iiko_store         — parent_id → department
  - iiko_department    — имя ресторана

Для актуальных результатов нужна свежая синхронизация:
  - sync_products (номенклатура с raw_json)
  - sync_stock_balances (текущие остатки)
"""

import logging
import time
import uuid as _uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from db.engine import async_session_factory
from db.models import Store, StockBalance, Department

logger = logging.getLogger(__name__)

LABEL = "MinStockCheck"


# ═══════════════════════════════════════════════════════
# Основная проверка
# ═══════════════════════════════════════════════════════

async def check_min_stock_levels(
    department_id: str | None = None,
) -> dict[str, Any]:
    """
    Проверяет товары ниже минимальных остатков.

    Остатки **суммируются по всем складам** в рамках одного department.
    Если department_id задан — возвращает только позиции этого ресторана.

    Returns:
        {
            "checked_at": datetime,
            "total_products": int,     # уникальных (dept, product) с min > 0
            "below_min_count": int,
            "department_name": str | None,
            "items": [
                {
                    "product_name": str,
                    "department_name": str,
                    "department_id": str,
                    "total_amount": float,   # сумма по всем складам dept
                    "min_level": float,
                    "max_level": float | None,
                    "deficit": float,
                },
                ...
            ],
        }
    """
    t0 = time.monotonic()
    logger.info("[%s] Начинаю проверку (department_id=%s)...", LABEL, department_id)

    async with async_session_factory() as session:

        # ── 1. Справочники: параллельно stores + departments ──
        import asyncio as _aio
        store_task = session.execute(
            select(Store.id, Store.name, Store.parent_id)
            .where(Store.deleted == False)  # noqa: E712
        )
        dept_task = session.execute(
            select(Department.id, Department.name)
        )
        store_result, dept_result = await _aio.gather(store_task, dept_task)
        store_rows = store_result.all()
        dept_rows = dept_result.all()

        store_dept_map: dict[_uuid.UUID, _uuid.UUID] = {}   # store_id → dept_id
        for row in store_rows:
            store_dept_map[row.id] = row.parent_id

        dept_names: dict[str, str] = {str(d.id): d.name for d in dept_rows}

        # ── 2. Продукты с minBalanceLevel > 0 из raw_json ──
        stmt = text("""
            SELECT
                p.id                                AS product_id,
                p.name                              AS product_name,
                elem->>'storeId'                    AS store_id_str,
                (elem->>'minBalanceLevel')::numeric  AS min_level,
                (elem->>'maxBalanceLevel')::numeric  AS max_level
            FROM iiko_product p,
                 jsonb_array_elements(p.raw_json->'storeBalanceLevels') elem
            WHERE p.deleted = false
              AND p.raw_json IS NOT NULL
              AND (elem->>'minBalanceLevel')::numeric > 0
            ORDER BY p.name
        """)
        limits = (await session.execute(stmt)).all()
        logger.info("[%s] Пар (product, store) с min > 0: %d", LABEL, len(limits))

        if not limits:
            return {
                "checked_at": datetime.utcnow(),
                "total_products": 0,
                "below_min_count": 0,
                "department_name": dept_names.get(department_id) if department_id else None,
                "items": [],
            }

        # ── 3. Дедупликация: (dept_id, product_id) → max(min_level), max_level ──
        # Если min задан на нескольких stores одного dept — берём MAX
        DeptProduct = tuple[str, _uuid.UUID]  # (dept_id_str, product_id)
        product_limits: dict[DeptProduct, dict] = {}

        for row in limits:
            store_id = _uuid.UUID(row.store_id_str)
            dept_id = store_dept_map.get(store_id)
            if not dept_id:
                continue
            dept_id_str = str(dept_id)

            # Фильтр по department если задан
            if department_id and dept_id_str != department_id:
                continue

            key: DeptProduct = (dept_id_str, row.product_id)
            existing = product_limits.get(key)
            min_level = float(row.min_level)
            max_level = float(row.max_level) if row.max_level is not None else None

            if existing is None or min_level > existing["min_level"]:
                product_limits[key] = {
                    "product_name": row.product_name,
                    "min_level": min_level,
                    "max_level": max_level,
                }

        logger.info(
            "[%s] Уникальных (dept, product) после дедупликации: %d",
            LABEL, len(product_limits),
        )

        # ── 4. Фактические остатки → (dept_id, product_id) → total ──
        balance_rows = (await session.execute(
            select(StockBalance.store_id, StockBalance.product_id, StockBalance.amount)
        )).all()

        dept_product_totals: dict[DeptProduct, float] = defaultdict(float)
        for br in balance_rows:
            dept_id = store_dept_map.get(br.store_id)
            if dept_id:
                dept_product_totals[(str(dept_id), br.product_id)] += float(br.amount)

        # ── 5. Сравниваем ──
        below_min: list[dict[str, Any]] = []
        for (dept_id_str, product_id), info in product_limits.items():
            total = dept_product_totals.get((dept_id_str, product_id), 0.0)
            min_level = info["min_level"]

            if total < min_level:
                below_min.append({
                    "product_name": info["product_name"],
                    "department_name": dept_names.get(dept_id_str, dept_id_str),
                    "department_id": dept_id_str,
                    "total_amount": round(total, 3),
                    "min_level": min_level,
                    "max_level": info["max_level"],
                    "deficit": round(min_level - total, 3),
                })

        # Сортировка: по дефициту убывание
        below_min.sort(key=lambda x: -x["deficit"])

        dept_name = dept_names.get(department_id) if department_id else None

        logger.info(
            "[%s] Готово: %d/%d ниже минимума за %.1f сек (department=%s)",
            LABEL, len(below_min), len(product_limits),
            time.monotonic() - t0, dept_name,
        )

        return {
            "checked_at": datetime.utcnow(),
            "total_products": len(product_limits),
            "below_min_count": len(below_min),
            "department_name": dept_name,
            "items": below_min,
        }


# ═══════════════════════════════════════════════════════
# Форматирование для Telegram
# ═══════════════════════════════════════════════════════

def format_min_stock_report(data: dict[str, Any]) -> str:
    """
    Форматирует результат check_min_stock_levels() в Telegram-сообщение.

    Группирует по department, если в данных несколько ресторанов.
    Ограничение Telegram ~4096 символов — обрезает при необходимости.
    """
    if data["below_min_count"] == 0:
        dept_info = f" ({data['department_name']})" if data.get("department_name") else ""
        return (
            f"✅ *Все товары выше минимальных остатков!*{dept_info}\n\n"
            f"Проверено позиций: {data['total_products']}"
        )

    dept_info = f" — {_escape_md(data['department_name'])}" if data.get("department_name") else ""
    lines = [
        f"⚠️ *Нужно заказать: {data['below_min_count']} поз.*{dept_info}\n"
        f"Проверено: {data['total_products']} позиций с минимумами\n"
    ]

    # Группируем по department
    by_dept: dict[str, list[dict]] = {}
    for item in data["items"]:
        by_dept.setdefault(item["department_name"], []).append(item)

    for dept_name, items in sorted(by_dept.items()):
        lines.append(f"\n📍 *{_escape_md(dept_name)}* ({len(items)} поз.)")
        for it in items:
            max_info = f" →{it['max_level']:.4g}" if it.get("max_level") else ""
            lines.append(
                f"  • {_escape_md(it['product_name'])}: "
                f"*{it['total_amount']:.4g}* / мин {it['min_level']:.4g}{max_info} "
                f"(−{it['deficit']:.4g})"
            )

    result = "\n".join(lines)

    if len(result) > 4000:
        result = result[:3950] + "\n\n_...обрезано (слишком много позиций)_"

    return result


def _escape_md(s: str) -> str:
    """Экранировать спецсимволы Markdown v1."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s
