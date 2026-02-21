"""
Use-case: синхронизация минимальных остатков (Google Таблица ↔ БД).

Две операции:
  1. sync_nomenclature_to_gsheet() — товары (GOODS+DISH) + подразделения → Google Таблицу
     (только из разрешённых корневых групп gsheet_export_group)
  2. sync_min_stock_from_gsheet() — Google Таблица → min_stock_level (БД)
     (UPSERT + mirror-delete)

Можно запускать:
  - Вручную (кнопка в боте)
  - По расписанию
"""

import logging
import time
from uuid import UUID

from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import async_session_factory
from db.models import (
    Product,
    ProductGroup,
    Department,
    MinStockLevel,
    GSheetExportGroup,
)

from adapters import google_sheets as gsheet

logger = logging.getLogger(__name__)

LABEL = "SyncMinStock"


# ═══════════════════════════════════════════════════════
# 1. Номенклатура → Google Таблица
# ═══════════════════════════════════════════════════════


async def sync_nomenclature_to_gsheet(triggered_by: str | None = None) -> int:
    """
    Выгрузить товары (GOODS) + подразделения (DEPARTMENT) в Google Таблицу.

    Сохраняет существующие min/max значения, добавляет новые товары.
    Все товары в алфавитном порядке.

    Возвращает кол-во товаров в таблице.
    """
    t0 = time.monotonic()
    logger.info("[%s] Номенклатура → GSheet (by=%s)...", LABEL, triggered_by)

    async with async_session_factory() as session:
        # ── 1. Корневые группы из БД (gsheet_export_group) ──
        root_rows = (await session.execute(select(GSheetExportGroup.group_id))).all()
        root_ids = [str(r.group_id) for r in root_rows]

        if not root_ids:
            logger.warning(
                "[%s] Нет корневых групп в gsheet_export_group — таблица будет пустой",
                LABEL,
            )
            return 0

        # ── 2. Построим дерево номенклатурных групп ──
        group_rows = (
            await session.execute(
                select(ProductGroup.id, ProductGroup.parent_id).where(
                    ProductGroup.deleted == False
                )  # noqa: E712
            )
        ).all()

        # parent_id → list[child_id]
        children_map: dict[str, list[str]] = {}
        for g in group_rows:
            pid = str(g.parent_id) if g.parent_id else None
            if pid:
                children_map.setdefault(pid, []).append(str(g.id))

        # BFS: все группы-потомки всех корневых (включая сами корневые)
        allowed_groups: set[str] = set()
        queue = list(root_ids)
        while queue:
            gid = queue.pop()
            if gid in allowed_groups:
                continue
            allowed_groups.add(gid)
            queue.extend(children_map.get(gid, []))

        logger.info(
            "[%s] Корневых групп: %d, всего в дереве: %d (из %d общих)",
            LABEL,
            len(root_ids),
            len(allowed_groups),
            len(group_rows),
        )

        # ── 3. Товары GOODS + DISH из разрешённых групп ──
        products_rows = (
            await session.execute(
                select(Product.id, Product.name, Product.parent_id)
                .where(Product.product_type.in_(["GOODS", "DISH"]))
                .where(Product.deleted == False)  # noqa: E712
                .order_by(Product.name)
            )
        ).all()

        # Фильтруем: parent_id товара должен быть в дереве разрешённых групп
        products_rows = [
            r
            for r in products_rows
            if r.parent_id and str(r.parent_id) in allowed_groups
        ]

        dept_rows = (
            await session.execute(
                select(Department.id, Department.name)
                .where(Department.department_type == "DEPARTMENT")
                .where(Department.deleted == False)  # noqa: E712
                .order_by(Department.name)
            )
        ).all()

    products = [{"id": str(r.id), "name": r.name} for r in products_rows]
    departments = [{"id": str(r.id), "name": r.name} for r in dept_rows]

    logger.info(
        "[%s] Из БД: %d товаров (GOODS+DISH), %d подразделений",
        LABEL,
        len(products),
        len(departments),
    )

    if not products:
        logger.warning("[%s] Нет товаров в БД — пропускаю", LABEL)
        return 0

    count = await gsheet.sync_products_to_sheet(products, departments)

    elapsed = time.monotonic() - t0
    logger.info("[%s] → GSheet: %d товаров за %.1f сек", LABEL, count, elapsed)
    return count


# ═══════════════════════════════════════════════════════
# 2. Google Таблица → min_stock_level (БД)
# ═══════════════════════════════════════════════════════


async def sync_min_stock_from_gsheet(triggered_by: str | None = None) -> int:
    """
    Синхронизировать мин/макс остатки: Google Таблица → min_stock_level (БД).

    UPSERT по (product_id, department_id) + mirror-delete.
    Возвращает количество записей в БД после синхронизации.
    """
    t0 = time.monotonic()
    logger.info("[%s] GSheet → БД (by=%s)...", LABEL, triggered_by)

    # 1. Прочитать все записи из Google Таблицы
    rows = await gsheet.read_all_levels()
    logger.info("[%s] Из GSheet: %d записей с min/max > 0", LABEL, len(rows))

    # 2. UPSERT в БД
    async with async_session_factory() as session:
        values = []
        for r in rows:
            try:
                values.append(
                    {
                        "product_id": UUID(r["product_id"]),
                        "product_name": r["product_name"],
                        "department_id": UUID(r["department_id"]),
                        "department_name": r["department_name"],
                        "min_level": r["min_level"],
                        "max_level": r["max_level"],
                    }
                )
            except (ValueError, KeyError) as exc:
                logger.warning("[%s] Пропускаю битую строку: %s", LABEL, exc)
                continue

        if values:
            BATCH = 500
            for i in range(0, len(values), BATCH):
                batch = values[i : i + BATCH]
                stmt = pg_insert(MinStockLevel).values(batch)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_min_stock_product_dept",
                    set_={
                        "product_name": stmt.excluded.product_name,
                        "department_name": stmt.excluded.department_name,
                        "min_level": stmt.excluded.min_level,
                        "max_level": stmt.excluded.max_level,
                    },
                )
                await session.execute(stmt)

        # 3. Mirror-delete: удаляем из БД записи, которых нет в Google Таблице
        gsheet_keys: set[tuple[UUID, UUID]] = set()
        for r in rows:
            try:
                gsheet_keys.add((UUID(r["product_id"]), UUID(r["department_id"])))
            except ValueError:
                continue

        if rows:  # Не удаляем если таблица пуста (защита от сбоя API)
            existing = (
                await session.execute(
                    select(MinStockLevel.product_id, MinStockLevel.department_id)
                )
            ).all()

            to_delete = [
                (row.product_id, row.department_id)
                for row in existing
                if (row.product_id, row.department_id) not in gsheet_keys
            ]

            if to_delete:
                # Batch DELETE — одним запросом вместо N
                DEL_BATCH = 500
                for i in range(0, len(to_delete), DEL_BATCH):
                    batch = to_delete[i : i + DEL_BATCH]
                    await session.execute(
                        delete(MinStockLevel).where(
                            tuple_(
                                MinStockLevel.product_id,
                                MinStockLevel.department_id,
                            ).in_(batch)
                        )
                    )
                logger.info(
                    "[%s] Удалено %d устаревших записей из БД", LABEL, len(to_delete)
                )

        await session.commit()

    elapsed = time.monotonic() - t0
    logger.info("[%s] → БД: %d записей за %.1f сек", LABEL, len(values), elapsed)
    return len(values)
