"""
Use-case: редактирование минимальных остатков продуктов в iiko
через Telegram-бот.

Логика:
  1. Пользователь ищет товар по подстроке названия.
  2. Выбирает товар из inline-кнопок.
  3. Вводит новый минимальный остаток.
  4. Бот находит все склады department пользователя,
     точечно обновляет storeBalanceLevels (остальные записи не трогает),
     отправляет в iiko API и обновляет raw_json в локальной БД.

Зависимости:
  - iiko_product       — raw_json содержит storeBalanceLevels
  - iiko_store         — склады подразделения (parent_id → department)
  - adapters/iiko_api  — update_product() для записи в iiko
"""

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import select, func, update

from db.engine import async_session_factory
from db.models import Product, Store

from adapters import iiko_api

logger = logging.getLogger(__name__)

LABEL = "EditMinStock"


# ═══════════════════════════════════════════════════════
# 1. Поиск товаров для редактирования
# ═══════════════════════════════════════════════════════

async def search_products_for_edit(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск товаров по подстроке названия (аналогично акту списания).
    Только GOODS и PREPARED, не удалённые.
    Возвращает [{id, name, product_type}, ...].
    """
    pattern = query.strip().lower()
    if not pattern:
        return []

    t0 = time.monotonic()
    logger.info("[%s] Поиск товаров по «%s»...", LABEL, pattern)

    async with async_session_factory() as session:
        stmt = (
            select(Product.id, Product.name, Product.product_type)
            .where(func.lower(Product.name).contains(pattern))
            .where(Product.product_type.in_(["GOODS", "PREPARED"]))
            .where(Product.deleted == False)  # noqa: E712
            .order_by(Product.name)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()

    items = [
        {"id": str(r.id), "name": r.name, "product_type": r.product_type}
        for r in rows
    ]
    logger.info(
        "[%s] Поиск «%s» → %d результатов за %.2f сек",
        LABEL, pattern, len(items), time.monotonic() - t0,
    )
    return items


# ═══════════════════════════════════════════════════════
# 2. Обновление мин. остатка по всем складам department
# ═══════════════════════════════════════════════════════

async def update_min_level(
    product_id: str,
    department_id: str,
    new_min: float,
) -> str:
    """
    Обновить minBalanceLevel для продукта на ВСЕХ складах department.

    Шаги:
      1. Параллельно: raw_json продукта + ID складов department из БД.
      2. В storeBalanceLevels точечно обновить/добавить записи для
         каждого склада department.  Записи других department — без изменений.
      3. Отправить полный storeBalanceLevels в iiko API.
      4. Обновить raw_json в локальной БД.

    Возвращает текстовый статус ("✅ ..." или "❌ ...").
    """
    t0 = time.monotonic()
    logger.info(
        "[%s] Обновляю min=%s для product=%s, dept=%s",
        LABEL, new_min, product_id, department_id,
    )

    # 1. Параллельно: продукт + склады department
    async with async_session_factory() as session:
        prod_task = session.execute(
            select(Product.name, Product.raw_json)
            .where(Product.id == UUID(product_id))
        )
        stores_task = session.execute(
            select(Store.id)
            .where(Store.parent_id == UUID(department_id))
            .where(Store.deleted == False)  # noqa: E712
        )
        prod_result, stores_result = await asyncio.gather(prod_task, stores_task)

    prod_row = prod_result.first()
    if not prod_row:
        return "❌ Товар не найден в БД"

    dept_store_ids: set[str] = {str(r.id) for r in stores_result.all()}
    if not dept_store_ids:
        return "❌ Не найдены склады для вашего ресторана"

    product_name = prod_row.name
    raw_json: dict = dict(prod_row.raw_json) if prod_row.raw_json else {}
    levels: list[dict] = list(raw_json.get("storeBalanceLevels", []))

    # 2. Точечное обновление: пройтись по существующим записям
    updated_store_ids: set[str] = set()
    old_mins: dict[str, float | None] = {}

    for item in levels:
        sid = item.get("storeId")
        if sid in dept_store_ids:
            old_mins[sid] = item.get("minBalanceLevel")
            item["minBalanceLevel"] = new_min
            updated_store_ids.add(sid)

    # Добавить записи для складов, которых ещё нет в массиве
    for sid in dept_store_ids - updated_store_ids:
        levels.append({
            "storeId": sid,
            "minBalanceLevel": new_min,
            "maxBalanceLevel": 0,
        })
        old_mins[sid] = None

    # 3. Отправить в iiko
    try:
        await iiko_api.update_product(
            product_id=product_id,
            fields={"storeBalanceLevels": levels},
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception(
            "[%s] ❌ Ошибка iiko API за %.2f сек: %s", LABEL, elapsed, exc,
        )
        return f"❌ Ошибка обновления в iiko: {exc}"

    # 4. Обновить raw_json в локальной БД
    raw_json["storeBalanceLevels"] = levels
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(Product)
                .where(Product.id == UUID(product_id))
                .values(raw_json=raw_json)
            )
            await session.commit()
    except Exception:
        logger.warning("[%s] raw_json не обновлён в БД (iiko OK)", LABEL, exc_info=True)

    elapsed = time.monotonic() - t0

    # Старый min — берём любой из обновлённых (обычно одинаковые)
    any_old = next(iter(old_mins.values()), None)
    old_str = f"{any_old:.4g}" if any_old is not None else "—"

    logger.info(
        "[%s] ✅ %s: min %s → %s (%d складов) за %.2f сек",
        LABEL, product_name, old_str, new_min, len(dept_store_ids), elapsed,
    )
    return (
        f"✅ *Минимальный остаток обновлён!*\n\n"
        f"📦 {_escape_md(product_name)}\n"
        f"Было: {old_str}\n"
        f"Стало: *{new_min:.4g}*\n"
        f"Складов обновлено: {len(dept_store_ids)}"
    )


def _escape_md(s: str) -> str:
    """Экранировать спецсимволы Markdown v1."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s
