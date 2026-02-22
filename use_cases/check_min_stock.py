"""
Use-case: проверка минимальных остатков по подразделениям (ресторанам).

Логика (v3 — Google Таблица как источник уровней):
  1. Из min_stock_level (БД) берём (product_id, department_id, min/max).
     Эта таблица синхронизируется из Google Таблицы.
  2. Из iiko_stock_balance берём фактические остатки.
  3. По store_id определяем department (Store.parent_id).
  4. **Суммируем** фактические остатки по ВСЕМ складам department.
  5. Сравниваем суммарный остаток с min_level из min_stock_level.

Зависимости (таблицы):
  - min_stock_level    — мин/макс остатки (из Google Таблицы)
  - iiko_stock_balance — фактические остатки по store/product
  - iiko_store         — parent_id → department
  - iiko_department    — имя ресторана
"""

import logging
import time
import uuid as _uuid
from typing import Any

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.models import Store, StockBalance, Department, MinStockLevel
from use_cases._helpers import now_kgd
from bot._utils import escape_md as _escape_md

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
            "total_products": int,
            "below_min_count": int,
            "department_name": str | None,
            "items": [
                {
                    "product_name": str,
                    "department_name": str,
                    "department_id": str,
                    "total_amount": float,
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

        # ── 1. Мин/макс уровни из min_stock_level ──
        levels_stmt = select(
            MinStockLevel.product_id,
            MinStockLevel.product_name,
            MinStockLevel.department_id,
            MinStockLevel.department_name,
            MinStockLevel.min_level,
            MinStockLevel.max_level,
        ).where(MinStockLevel.min_level > 0)

        if department_id:
            levels_stmt = levels_stmt.where(
                MinStockLevel.department_id == _uuid.UUID(department_id)
            )

        limits = (await session.execute(levels_stmt)).all()
        logger.info("[%s] Позиций с min > 0: %d", LABEL, len(limits))

        if not limits:
            dept_stmt = (
                select(Department.name).where(
                    Department.id == _uuid.UUID(department_id)
                )
                if department_id
                else None
            )
            dept_name = (
                (await session.execute(dept_stmt)).scalar_one_or_none()
                if dept_stmt
                else None
            )
            return {
                "checked_at": now_kgd(),
                "total_products": 0,
                "below_min_count": 0,
                "department_name": dept_name,
                "items": [],
            }

        # ── 2. Справочники: store → department mapping ──
        store_rows = (
            await session.execute(
                select(Store.id, Store.parent_id).where(
                    Store.deleted.is_(False)
                )
            )
        ).all()

        store_dept_map: dict[_uuid.UUID, _uuid.UUID] = {
            row.id: row.parent_id for row in store_rows
        }

        dept_rows = (
            await session.execute(select(Department.id, Department.name))
        ).all()
        dept_names: dict[str, str] = {str(d.id): d.name for d in dept_rows}

        # ── 3. Фактические остатки: SQL агрегация (store_id, product_id) → SUM(amount) ──
        # Вместо загрузки всех строк в Python → агрегируем на стороне БД
        balance_agg = (
            await session.execute(
                select(
                    StockBalance.store_id,
                    StockBalance.product_id,
                    func.sum(StockBalance.amount).label("total"),
                ).group_by(StockBalance.store_id, StockBalance.product_id)
            )
        ).all()

        # Пересчитываем по dept: (dept_id_str, product_id) → total_amount
        dept_product_totals: dict[tuple[str, _uuid.UUID], float] = {}
        for br in balance_agg:
            dept_id = store_dept_map.get(br.store_id)
            if dept_id:
                key = (str(dept_id), br.product_id)
                dept_product_totals[key] = dept_product_totals.get(key, 0.0) + float(
                    br.total
                )

        # ── 4. Сравниваем ──
        below_min: list[dict[str, Any]] = []
        for row in limits:
            dept_id_str = str(row.department_id)
            total = dept_product_totals.get((dept_id_str, row.product_id), 0.0)
            min_level = float(row.min_level)
            max_level = float(row.max_level) if row.max_level else None

            if total < min_level:
                below_min.append(
                    {
                        "product_name": row.product_name,
                        "department_name": row.department_name
                        or dept_names.get(dept_id_str, dept_id_str),
                        "department_id": dept_id_str,
                        "total_amount": round(total, 3),
                        "min_level": min_level,
                        "max_level": max_level,
                        "deficit": round(min_level - total, 3),
                    }
                )

        # Сортировка: по дефициту убывание
        below_min.sort(key=lambda x: -x["deficit"])

        dept_name = dept_names.get(department_id) if department_id else None

        logger.info(
            "[%s] Готово: %d/%d ниже минимума за %.1f сек (department=%s)",
            LABEL,
            len(below_min),
            len(limits),
            time.monotonic() - t0,
            dept_name,
        )

        return {
            "checked_at": now_kgd(),
            "total_products": len(limits),
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
    """
    if data["below_min_count"] == 0:
        dept_info = (
            f" ({data['department_name']})" if data.get("department_name") else ""
        )
        return (
            f"✅ *Все товары выше минимальных остатков!*{dept_info}\n\n"
            f"Проверено позиций: {data['total_products']}"
        )

    dept_info = (
        f" — {_escape_md(data['department_name'])}"
        if data.get("department_name")
        else ""
    )
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
