"""
Use-case: расходная накладная (outgoing invoice) — шаблоны + данные.

Бизнес-логика:
  1. Поиск контрагентов (suppliers) по подстроке имени
  2. Получение счёта «реализация на точки» (Account)
  3. Получение складов подразделения (бар / кухня)
  4. Поиск номенклатуры по дереву gsheet_export_group (GOODS + DISH)
  5. CRUD шаблонов (InvoiceTemplate)
  6. Прогрев кеша при входе в раздел
"""

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import select, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import async_session_factory
from db.models import (
    Supplier,
    Entity,
    Store,
    Product,
    ProductGroup,
    GSheetExportGroup,
    InvoiceTemplate,
    PriceProduct,
    PriceSupplierColumn,
    PriceSupplierPrice,
)
from use_cases import invoice_cache as _cache

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Контрагенты (suppliers)
# ═══════════════════════════════════════════════════════


async def load_all_suppliers() -> list[dict]:
    """
    Загрузить всех поставщиков (не удалённых) в кеш.
    Возвращает [{id, name}, ...].
    """
    cached = _cache.get_suppliers()
    if cached is not None:
        return cached

    t0 = time.monotonic()
    logger.info("[invoice] Загрузка поставщиков...")

    async with async_session_factory() as session:
        stmt = (
            select(Supplier.id, Supplier.name)
            .where(Supplier.deleted == False)  # noqa: E712
            .order_by(Supplier.name)
        )
        result = await session.execute(stmt)
        rows = result.all()

    items = [{"id": str(r.id), "name": r.name} for r in rows]
    _cache.set_suppliers(items)
    logger.info(
        "[invoice] Поставщиков: %d за %.2f сек",
        len(items),
        time.monotonic() - t0,
    )
    return items


async def search_suppliers(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск поставщиков по подстроке имени.
    Сначала по кешу (in-memory), fallback — БД.
    Возвращает [{id, name}, ...].
    """
    pattern = query.strip().lower()
    if not pattern:
        return []

    t0 = time.monotonic()

    # Попробуем в кеше
    cached = _cache.get_suppliers()
    if cached is not None:
        results = [s for s in cached if s["name"] and pattern in s["name"].lower()][
            :limit
        ]
        logger.info(
            "[invoice] Поиск поставщиков «%s» → %d результатов за %.4f сек (кеш)",
            pattern,
            len(results),
            time.monotonic() - t0,
        )
        return results

    # Fallback: БД
    logger.debug("[invoice] Кеш поставщиков пуст, ищу в БД...")
    async with async_session_factory() as session:
        stmt = (
            select(Supplier.id, Supplier.name)
            .where(Supplier.deleted == False)  # noqa: E712
            .where(func.lower(Supplier.name).contains(pattern))
            .order_by(Supplier.name)
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.all()

    items = [{"id": str(r.id), "name": r.name} for r in rows]
    logger.info(
        "[invoice] Поиск поставщиков «%s» → %d результатов за %.2f сек (БД)",
        pattern,
        len(items),
        time.monotonic() - t0,
    )
    return items


# ═══════════════════════════════════════════════════════
# Счёт реализации
# ═══════════════════════════════════════════════════════


async def get_revenue_account() -> dict | None:
    """
    Получить счёт «реализация на точки» из iiko_entity (Account).
    Возвращает {id, name} или None.
    """
    cached = _cache.get_revenue_account()
    if cached is not None:
        return cached

    t0 = time.monotonic()
    logger.info("[invoice] Поиск счёта реализации...")

    async with async_session_factory() as session:
        stmt = (
            select(Entity.id, Entity.name)
            .where(Entity.root_type == "Account")
            .where(Entity.deleted == False)  # noqa: E712
            .where(func.lower(Entity.name).contains("реализация на точки"))
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.first()

    if not row:
        logger.warning("[invoice] Счёт «реализация на точки» не найден!")
        return None

    account = {"id": str(row.id), "name": row.name}
    _cache.set_revenue_account(account)
    logger.info(
        "[invoice] Счёт реализации: %s (%s) за %.2f сек",
        account["name"],
        account["id"],
        time.monotonic() - t0,
    )
    return account


# ═══════════════════════════════════════════════════════
# Склады подразделения (бар / кухня)
# ═══════════════════════════════════════════════════════


async def get_stores_for_department(department_id: str) -> list[dict]:
    """
    Получить склады, привязанные к подразделению,
    у которых в названии есть 'бар' или 'кухня'.
    Возвращает [{id, name}, ...].
    """
    cached = _cache.get_stores(department_id)
    if cached is not None:
        logger.debug("[invoice] Склады из кеша (%d шт)", len(cached))
        return cached

    t0 = time.monotonic()
    logger.info("[invoice] Загрузка складов для подразделения %s...", department_id)

    async with async_session_factory() as session:
        stmt = (
            select(Store)
            .where(Store.parent_id == UUID(department_id))
            .where(Store.deleted == False)  # noqa: E712
            .where(
                or_(
                    func.lower(Store.name).contains("бар"),
                    func.lower(Store.name).contains("кухня"),
                )
            )
            .order_by(Store.name)
        )
        result = await session.execute(stmt)
        stores = result.scalars().all()

    items = [{"id": str(s.id), "name": s.name} for s in stores]
    _cache.set_stores(department_id, items)
    logger.info(
        "[invoice] Склады для %s: %d шт за %.2f сек",
        department_id,
        len(items),
        time.monotonic() - t0,
    )
    return items


# ═══════════════════════════════════════════════════════
# Номенклатура по дереву gsheet_export_group (GOODS + DISH)
# ═══════════════════════════════════════════════════════


async def preload_products_tree() -> None:
    """
    Загрузить все GOODS+DISH из дерева gsheet_export_group в кеш.
    BFS по iiko_product_group → фильтр по parent_id.
    Используется для поиска товаров при создании шаблона.
    """
    if _cache.get_products() is not None:
        return  # уже прогрет

    t0 = time.monotonic()
    async with async_session_factory() as session:
        # 1. Корневые группы
        root_rows = (await session.execute(select(GSheetExportGroup.group_id))).all()
        root_ids = [str(r.group_id) for r in root_rows]

        if not root_ids:
            logger.warning("[invoice] Нет корневых групп в gsheet_export_group")
            _cache.set_products([])
            return

        # 2. BFS по дереву номенклатурных групп
        group_rows = (
            await session.execute(
                select(ProductGroup.id, ProductGroup.parent_id).where(
                    ProductGroup.deleted == False
                )  # noqa: E712
            )
        ).all()

        children_map: dict[str, list[str]] = {}
        for g in group_rows:
            pid = str(g.parent_id) if g.parent_id else None
            if pid:
                children_map.setdefault(pid, []).append(str(g.id))

        allowed_groups: set[str] = set()
        queue = list(root_ids)
        while queue:
            gid = queue.pop()
            if gid in allowed_groups:
                continue
            allowed_groups.add(gid)
            queue.extend(children_map.get(gid, []))

        # 3. Товары GOODS + DISH из разрешённых групп + единицы измерения
        products_rows = (
            await session.execute(
                select(
                    Product.id,
                    Product.name,
                    Product.parent_id,
                    Product.product_type,
                    Product.main_unit,
                )
                .where(Product.product_type.in_(["GOODS", "DISH"]))
                .where(Product.deleted == False)  # noqa: E712
                .order_by(Product.name)
            )
        ).all()

        # Фильтруем по дереву
        products_rows = [
            r
            for r in products_rows
            if r.parent_id and str(r.parent_id) in allowed_groups
        ]

        # Батч-резолв единиц измерения
        unit_ids = {r.main_unit for r in products_rows if r.main_unit}
        unit_map: dict[str, str] = {}
        if unit_ids:
            unit_stmt = (
                select(Entity.id, Entity.name)
                .where(Entity.id.in_(list(unit_ids)))
                .where(Entity.root_type == "MeasureUnit")
            )
            unit_result = await session.execute(unit_stmt)
            for row in unit_result.all():
                unit_map[str(row.id)] = row.name or "шт"

    # Формируем список
    items: list[dict] = []
    for pid, pname, p_parent, ptype, pmain_unit in products_rows:
        uid_str = str(pmain_unit) if pmain_unit else None
        uname = unit_map.get(uid_str, "шт") if uid_str else "шт"
        items.append(
            {
                "id": str(pid),
                "name": pname,
                "name_lower": pname.lower() if pname else "",
                "product_type": ptype,
                "unit_name": uname,
                "main_unit": uid_str,
            }
        )

    _cache.set_products(items)
    logger.info(
        "[invoice] Прогрев кеша номенклатуры (дерево): %d товаров за %.2f сек",
        len(items),
        time.monotonic() - t0,
    )


async def search_products_tree(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск товаров по дереву gsheet_export_group.
    Сначала в кеше (in-memory), fallback — прогреть кеш + повторить.
    Возвращает [{id, name, product_type, unit_name}, ...].
    """
    pattern = query.strip().lower()
    if not pattern:
        return []

    t0 = time.monotonic()

    cached = _cache.get_products()
    if cached is None:
        await preload_products_tree()
        cached = _cache.get_products()

    if not cached:
        return []

    results = [
        {k: v for k, v in p.items() if k != "name_lower"}
        for p in cached
        if pattern in p["name_lower"]
    ][:limit]

    logger.info(
        "[invoice] Поиск товаров «%s» → %d результатов за %.4f сек",
        pattern,
        len(results),
        time.monotonic() - t0,
    )
    return results


# ═══════════════════════════════════════════════════════
# CRUD шаблонов
# ═══════════════════════════════════════════════════════


async def save_template(
    *,
    name: str,
    created_by: int,
    department_id: str,
    counteragent_id: str,
    counteragent_name: str,
    account_id: str,
    account_name: str,
    store_id: str,
    store_name: str,
    items: list[dict],
) -> int:
    """
    Сохранить шаблон расходной накладной в БД.
    Возвращает pk шаблона.
    """
    t0 = time.monotonic()
    logger.info(
        "[invoice] Сохранение шаблона «%s» (by=%d, items=%d)...",
        name,
        created_by,
        len(items),
    )

    # Шаблон хранит только позиции (без цен — они подтягиваются динамически)
    clean_items = [
        {k: v for k, v in it.items() if k not in ("cost_price", "sell_price", "price")}
        for it in items
    ]

    async with async_session_factory() as session:
        tmpl = InvoiceTemplate(
            name=name,
            created_by=created_by,
            department_id=UUID(department_id),
            counteragent_id=UUID(counteragent_id),
            counteragent_name=counteragent_name,
            account_id=UUID(account_id),
            account_name=account_name,
            store_id=UUID(store_id),
            store_name=store_name,
            items=clean_items,
        )
        session.add(tmpl)
        await session.commit()
        pk = tmpl.pk

    logger.info(
        "[invoice] Шаблон pk=%d сохранён за %.2f сек",
        pk,
        time.monotonic() - t0,
    )
    return pk


async def get_templates_for_department(department_id: str) -> list[dict]:
    """
    Получить все шаблоны для подразделения.
    Возвращает [{pk, name, counteragent_name, store_name, items_count, created_at}, ...].
    """
    t0 = time.monotonic()
    async with async_session_factory() as session:
        stmt = (
            select(InvoiceTemplate)
            .where(InvoiceTemplate.department_id == UUID(department_id))
            .order_by(InvoiceTemplate.created_at.desc())
        )
        result = await session.execute(stmt)
        templates = result.scalars().all()

    items = []
    for t in templates:
        items.append(
            {
                "pk": t.pk,
                "name": t.name,
                "counteragent_name": t.counteragent_name,
                "store_name": t.store_name,
                "account_name": t.account_name,
                "items_count": len(t.items) if t.items else 0,
                "created_at": t.created_at,
            }
        )

    logger.info(
        "[invoice] Шаблоны для dept=%s: %d шт за %.2f сек",
        department_id,
        len(items),
        time.monotonic() - t0,
    )
    return items


async def get_template_by_pk(pk: int) -> dict | None:
    """
    Получить полный шаблон по pk.
    Возвращает dict со всеми полями или None.
    """
    async with async_session_factory() as session:
        stmt = select(InvoiceTemplate).where(InvoiceTemplate.pk == pk)
        result = await session.execute(stmt)
        t = result.scalar_one_or_none()

    if not t:
        return None

    return {
        "pk": t.pk,
        "name": t.name,
        "created_by": t.created_by,
        "department_id": str(t.department_id),
        "counteragent_id": str(t.counteragent_id),
        "counteragent_name": t.counteragent_name,
        "account_id": str(t.account_id),
        "account_name": t.account_name,
        "store_id": str(t.store_id),
        "store_name": t.store_name,
        "items": t.items or [],
        "created_at": t.created_at,
    }


async def delete_template(pk: int) -> bool:
    """Удалить шаблон. Возвращает True если удалён."""
    async with async_session_factory() as session:
        stmt = select(InvoiceTemplate).where(InvoiceTemplate.pk == pk)
        result = await session.execute(stmt)
        t = result.scalar_one_or_none()
        if not t:
            return False
        await session.delete(t)
        await session.commit()
    logger.info("[invoice] Шаблон pk=%d удалён", pk)
    return True


# ═══════════════════════════════════════════════════════
# Прогрев кеша
# ═══════════════════════════════════════════════════════


async def preload_for_invoice(department_id: str) -> None:
    """
    Прогрев кеша при входе в раздел «Накладные».
    Запускается фоново.
    """
    t0 = time.monotonic()
    try:
        await asyncio.gather(
            load_all_suppliers(),
            get_revenue_account(),
            get_stores_for_department(department_id),
            preload_products_tree(),
        )
        logger.info(
            "[invoice] Прогрев кеша за %.2f сек",
            time.monotonic() - t0,
        )
    except Exception:
        logger.warning("[invoice] Ошибка прогрева кеша", exc_info=True)


# ═══════════════════════════════════════════════════════
# Себестоимости: товары по последнему приходу + техкарты
# ═══════════════════════════════════════════════════════


async def calculate_goods_cost_prices(
    days_back: int = 90,
    store_ids: set[str] | None = None,
) -> dict[str, float]:
    """
    Себестоимость GOODS по средневзвешенной цене остатка (СЦС).

    Основной источник: GET /resto/api/v2/reports/balance/stores
    Для каждого продукта: СЦС = sum(sum_по_складам) / sum(amount_по_складам).

    store_ids — если передан, учитываются только остатки этих складов
                (склады выбранного подразделения из Настроек).
                None = все склады (поведение по умолчанию).

    Fallback (для товаров с нулевым/отсутствующим остатком):
    последняя цена из приходных накладных за `days_back` дней.

    Возвращает {product_id_lower: cost_price}.
    """
    from datetime import timedelta
    from adapters import iiko_api
    from use_cases._helpers import now_kgd

    today = now_kgd()
    t0 = time.monotonic()
    if store_ids:
        logger.info(
            "[invoice] Расчёт себестоимости GOODS: СЦС по %d складам подразделения",
            len(store_ids),
        )
    else:
        logger.info(
            "[invoice] Расчёт себестоимости GOODS: СЦС из остатков всех складов"
        )

    # ── 1. Остатки склада → СЦС ─────────────────────────────────────────────
    balances = await iiko_api.fetch_stock_balances()

    # Нормализуем store_ids к lowercase для сравнения
    store_ids_lower: set[str] | None = (
        {sid.lower() for sid in store_ids} if store_ids else None
    )

    def _build_ccs(entries, filter_stores=None) -> dict[str, float]:
        p_sum: dict[str, float] = {}
        p_amt: dict[str, float] = {}
        for entry in entries:
            sid = (entry.get("store") or "").lower().strip()
            if filter_stores and sid not in filter_stores:
                continue
            pid = (entry.get("product") or "").lower().strip()
            amt = float(entry.get("amount") or 0)
            sm = float(entry.get("sum") or 0)
            if pid and amt > 0 and sm > 0:
                p_sum[pid] = p_sum.get(pid, 0.0) + sm
                p_amt[pid] = p_amt.get(pid, 0.0) + amt
        return {pid: p_sum[pid] / p_amt[pid] for pid in p_sum if p_amt[pid] > 0}

    # Всегда считаем СЦС по ВСЕМ складам (используется как fallback-уровень 1)
    cost_map_all = _build_ccs(balances)

    if store_ids_lower:
        # Считаем СЦС только по складам подразделения
        cost_map_dept = _build_ccs(balances, store_ids_lower)
        # Начинаем с цен подразделения, дополняем all-stores для отсутствующих
        cost_map = dict(cost_map_dept)
        all_stores_fallback = 0
        for pid, price in cost_map_all.items():
            if pid not in cost_map:
                cost_map[pid] = price
                all_stores_fallback += 1
        logger.info(
            "[invoice] СЦС из остатков: %d товаров "
            "(подразделение: %d, all-stores fallback: %d) из %d записей за %.2f сек",
            len(cost_map),
            len(cost_map_dept),
            all_stores_fallback,
            len(balances),
            time.monotonic() - t0,
        )
    else:
        cost_map = cost_map_all
        logger.info(
            "[invoice] СЦС из остатков: %d товаров из %d записей за %.2f сек",
            len(cost_map),
            len(balances),
            time.monotonic() - t0,
        )

    # ── 2. Fallback: последняя накладная для товаров без остатка ─────────────
    date_to = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    logger.info("[invoice] Fallback: приходные накладные %s..%s", date_from, date_to)
    invoices = await iiko_api.fetch_incoming_invoices(date_from, date_to)

    def _parse_date(inv: dict) -> str:
        return inv.get("dateIncoming") or ""

    invoices.sort(key=_parse_date)

    fallback_map: dict[str, float] = {}
    for inv in invoices:
        for item in inv.get("items", []):
            pid = item.get("productId", "").strip().lower()
            price_str = str(item.get("price") or "").strip()
            if not pid or not price_str:
                continue
            try:
                price = float(price_str)
            except ValueError:
                continue
            if price > 0:
                fallback_map[pid] = price

    # Применяем fallback только там, где нет СЦС
    fallback_applied = 0
    for pid, price in fallback_map.items():
        if pid not in cost_map:
            cost_map[pid] = price
            fallback_applied += 1

    logger.info(
        "[invoice] Себестоимость GOODS итого: %d товаров "
        "(СЦС: %d, fallback-накладные: %d) за %.2f сек",
        len(cost_map),
        len(cost_map) - fallback_applied,
        fallback_applied,
        time.monotonic() - t0,
    )
    return cost_map


async def calculate_dish_cost_prices(
    goods_costs: dict[str, float],
) -> dict[str, float]:
    """
    Себестоимость DISH (блюд) по технологическим картам.

    API возвращает два массива, оба используют одинаковое поле количества:
      assemblyCharts  — DISH-рецепты,    поле количества = "amountOut"
      preparedCharts  — PREPARED (п/ф),  поле количества = "amountOut"

    Себестоимость на ЕДИНИЦУ = sum(ингредиент × цена) / assembledAmount.
    assembledAmount — сколько единиц выходного продукта производит рецепт.

    Алгоритм:
      1. Строим общую карту техкарт из обоих массивов
      2. Итеративно считаем себестоимость (сначала PREPARED, потом DISH)
      3. Возвращаем только DISH {product_id: cost_price}
    """
    from adapters import iiko_api
    from use_cases._helpers import now_kgd
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product

    today = now_kgd().strftime("%Y-%m-%d")
    t0 = time.monotonic()
    logger.info("[invoice] Расчёт себестоимости DISH по техкартам (дата=%s)...", today)

    chart_data = await iiko_api.fetch_assembly_charts(
        today, today, include_prepared=True
    )
    assembly_charts = (
        chart_data.get("assemblyCharts") or []
    )  # DISH,     amount = amountOut
    prepared_charts = (
        chart_data.get("preparedCharts") or []
    )  # PREPARED, amount = amountOut (same!)

    # Общая карта: {assembledProductId: (chart, amount_field)}
    # Для каждого продукта берём самую свежую действующую версию (dateFrom ≤ today, dateTo is null или ≥ today)
    def _pick_effective(
        charts: list[dict], amount_field: str
    ) -> dict[str, tuple[dict, str]]:
        best: dict[str, tuple[str, dict]] = {}  # pid → (dateFrom_str, chart)
        for c in charts:
            pid = (c.get("assembledProductId") or "").lower()  # нормализуем к lowercase
            if not pid:
                continue
            date_from = (c.get("dateFrom") or "")[:10]
            date_to = (c.get("dateTo") or "")[:10]
            # датаTo = null значит бессрочно; если указана — должна быть ≥ today
            if date_to and date_to < today:
                continue
            # Берём самую позднюю dateFrom ≤ today
            if date_from > today:
                continue
            if pid not in best or date_from > best[pid][0]:
                best[pid] = (date_from, c)
        return {pid: (entry[1], amount_field) for pid, entry in best.items()}

    chart_map: dict[str, tuple[dict, str]] = {}
    chart_map.update(_pick_effective(prepared_charts, "amountOut"))
    chart_map.update(_pick_effective(assembly_charts, "amountOut"))

    # Получаем типы продуктов из БД (ключи lowercase)
    product_types: dict[str, str] = {}
    async with get_session() as sess:
        result = await sess.execute(select(Product.id, Product.product_type))
        for row in result.all():
            product_types[str(row.id).lower()] = row.product_type

    # Итеративный расчёт: начинаем с goods_costs, добавляем PREPARED, потом DISH
    # Исключаем DISH-продукты из начального словаря — их себестоимость должна
    # считаться по техкарте, а не по остаткам склада (иначе блюда, которые
    # одновременно хранятся на складе, получат СЦС вместо рецептурной цены).
    all_costs: dict[str, float] = {
        pid: cost
        for pid, cost in goods_costs.items()
        if product_types.get(pid) != "DISH"
    }

    max_iterations = 10  # защита от циклических зависимостей
    for iteration in range(max_iterations):
        changes = False

        for dish_id, (chart, amount_field) in chart_map.items():
            if dish_id in all_costs:
                continue  # уже рассчитано

            items = chart.get("items") or []
            if not items:
                continue

            total_cost = 0.0
            all_ingredients_ready = True

            for item in items:
                ingredient_id = (item.get("productId") or "").lower()  # нормализуем
                if not ingredient_id:
                    continue
                try:
                    amount = float(item.get(amount_field, 0))
                except (TypeError, ValueError):
                    amount = 0.0

                ingr_cost = all_costs.get(ingredient_id)
                if ingr_cost is None:
                    if ingredient_id in chart_map:
                        # ингредиент — п/ф, ещё не рассчитан → ждём следующей итерации
                        all_ingredients_ready = False
                        break
                    # нет ни цены, ни техкарты — пропускаем ингредиент
                    continue

                total_cost += amount * ingr_cost

            if all_ingredients_ready and total_cost > 0:
                # Делим на assembledAmount: рецепт может производить != 1 единицу
                # (например, 0.165 кг гренок из 140 г хлеба + 25 г масла)
                assembled_amount = float(chart.get("assembledAmount") or 1.0)
                if assembled_amount <= 0:
                    assembled_amount = 1.0
                all_costs[dish_id] = round(total_cost / assembled_amount, 4)
                changes = True

        if not changes:
            break

    # Возвращаем только DISH (PREPARED остаётся в all_costs для расчёта, в таблицу не идёт)
    dish_costs: dict[str, float] = {
        pid: cost for pid, cost in all_costs.items() if product_types.get(pid) == "DISH"
    }

    logger.info(
        "[invoice] DISH: %d блюд с себестоимостью (%d assembly + %d prepared техкарт, итераций=%d)",
        len(dish_costs),
        len(assembly_charts),
        len(prepared_charts),
        iteration + 1,
    )

    return dish_costs


async def sync_price_sheet(days_back: int = 90, **kwargs) -> str:
    """
    Полная синхронизация прайс-листа:
    1. Загружает дерево товаров (GOODS + DISH из gsheet_export_group)
    2. Рассчитывает себестоимость GOODS по приходным накладным
    3. Рассчитывает себестоимость DISH по техкартам
    4. Записывает в Google Sheet (сохраняя ручные цены)

    Возвращает описание результата.
    """
    from adapters import google_sheets as gs

    t0 = time.monotonic()
    logger.info("[invoice] Полная синхронизация прайс-листа...")

    # 1. Прогреваем дерево товаров
    await preload_products_tree()
    products = _cache.get_products() or []
    if not products:
        logger.warning("[invoice] Нет товаров для прайс-листа")
        return "0 товаров"

    # 1b. Склады выбранного подразделения (из Настроек GSheet)
    dept_store_ids: set[str] = set()
    stores_for_dropdown: list[dict[str, str]] = []
    try:
        from use_cases.product_request import get_request_stores
        from db.engine import async_session_factory
        from db.models import Store
        from sqlalchemy import select as sa_select

        selected = await get_request_stores()  # [{id, name}] — 0 или 1
        if selected:
            dept_uuid = selected[0]["id"]
            async with async_session_factory() as sess:
                res = await sess.execute(
                    sa_select(Store.id, Store.name)
                    .where(Store.deleted.is_(False))
                    .where(Store.parent_id == dept_uuid)
                    .order_by(Store.name)
                )
                stores_for_dropdown = [
                    {"id": str(r.id), "name": r.name} for r in res.all()
                ]
            dept_store_ids = {s["id"] for s in stores_for_dropdown}
            logger.info(
                "[invoice] Склады заведения '%s': %d — СЦС будет по ним",
                selected[0]["name"],
                len(dept_store_ids),
            )
        else:
            logger.warning(
                "[invoice] Заведение для заявок не выбрано — СЦС по всем складам"
            )
    except Exception:
        logger.warning("[invoice] Ошибка загрузки складов подразделения", exc_info=True)

    # 2. Себестоимость GOODS (только по складам подразделения, если настроено)
    goods_costs = await calculate_goods_cost_prices(
        days_back=days_back,
        store_ids=dept_store_ids or None,
    )

    # 3. Себестоимость DISH (ингредиенты из goods_costs)
    dish_costs = await calculate_dish_cost_prices(goods_costs)

    # 4. Объединяем
    all_costs: dict[str, float] = {}
    all_costs.update(goods_costs)
    all_costs.update(dish_costs)

    # 5. Подготовка списка продуктов: сначала блюда (DISH), потом товары (GOODS)
    dish_products = []
    goods_products = []
    other_products = []

    for p in products:
        product_dict = {
            "id": p["id"],
            "name": p["name"],
            "product_type": p.get("product_type", ""),
            "main_unit": p.get("main_unit"),
            "unit_name": p.get("unit_name", "шт"),
        }
        product_type = p.get("product_type", "")
        if not product_type:
            logger.debug("[invoice] Товар без типа: %s", p.get("name"))
            other_products.append(product_dict)
        elif product_type == "DISH":
            dish_products.append(product_dict)
        elif product_type == "GOODS":
            goods_products.append(product_dict)
        else:
            logger.debug(
                "[invoice] Неизвестный тип '%s' у товара: %s",
                product_type,
                p.get("name"),
            )
            other_products.append(product_dict)

    # Сначала блюда, потом товары, потом остальное
    products_for_sheet = dish_products + goods_products + other_products

    logger.info(
        "[invoice] Прайс-лист: разделение на блоки — %d блюд, %d товаров, %d прочих",
        len(dish_products),
        len(goods_products),
        len(other_products),
    )

    # 6. Загружаем поставщиков для dropdown в Google Sheet
    suppliers = await load_all_suppliers()
    # stores_for_dropdown уже загружены в шаге 1b

    # 7. Записываем в Google Sheet
    count = await gs.sync_invoice_prices_to_sheet(
        products_for_sheet,
        all_costs,
        suppliers,
        stores_for_dropdown=stores_for_dropdown,
    )

    # 8. Читаем обратно из GSheet (вкл. ручные цены поставщиков) и пишем в БД
    try:
        store_id_map = {s["name"]: s["id"] for s in stores_for_dropdown}
        gsheet_data = await gs.read_invoice_prices(store_id_map=store_id_map)
        db_count = await _sync_prices_to_db(
            products_for_sheet,
            all_costs,
            gsheet_data,
            suppliers,
        )
        logger.info("[invoice] Прайс-лист → БД: %d товаров, prices synced", db_count)
    except Exception:
        logger.warning("[invoice] Ошибка синхронизации прайс-листа в БД", exc_info=True)

    elapsed = time.monotonic() - t0
    logger.info(
        "[invoice] Прайс-лист: %d товаров, %d GOODS + %d DISH себестоимостей за %.1f сек",
        count,
        len(goods_costs),
        len(dish_costs),
        elapsed,
    )
    return f"{count} товаров ({len(goods_costs)} GOODS + {len(dish_costs)} DISH себестоимостей)"


# ═══════════════════════════════════════════════════════
# Синхронизация прайс-листа → БД
# ═══════════════════════════════════════════════════════


async def _sync_prices_to_db(
    products: list[dict],
    cost_prices: dict[str, float],
    gsheet_data: list[dict],
    suppliers: list[dict],
) -> int:
    """
    Записать прайс-лист из GSheet в 3 таблицы БД:
      price_product, price_supplier_column, price_supplier_price.

    products:     [{id, name, product_type, main_unit, unit_name}, ...]
    cost_prices:  {product_id: cost_price}
    gsheet_data:  [{product_id, product_name, store_id, store_name, cost_price, supplier_prices: {sid: price}}, ...]
    suppliers:    [{id, name}, ...]
    """
    t0 = time.monotonic()
    supplier_name_map = {s["id"]: s["name"] for s in suppliers}

    # Собираем активных поставщиков (у которых есть хоть одна цена)
    active_supplier_ids: set[str] = set()
    for item in gsheet_data:
        active_supplier_ids.update(item.get("supplier_prices", {}).keys())

    # product info lookup
    prod_info = {p["id"]: p for p in products}

    CHUNK = 500

    async with async_session_factory() as session:
        # ── 1. Batch upsert price_product ──
        product_rows: list[dict] = []
        for item in gsheet_data:
            pid = item["product_id"]
            info = prod_info.get(pid, {})
            product_type = info.get("product_type", "")

            # Для DISH берём ТОЛЬКО расчётную себестоимость (无 fallback из GSheet)
            # Для GOODS берём расчётную, а если нет — fallback из GSheet (ручное значение)
            if product_type == "DISH":
                cost = cost_prices.get(pid)  # None если не рассчитано
            else:
                cost = cost_prices.get(pid, item.get("cost_price"))

            main_unit_str = info.get("main_unit")
            store_id_str = item.get("store_id", "")
            product_rows.append(
                {
                    "product_id": UUID(pid),
                    "product_name": item.get("product_name") or info.get("name", ""),
                    "product_type": info.get("product_type", ""),
                    "cost_price": cost,
                    "main_unit": UUID(main_unit_str) if main_unit_str else None,
                    "unit_name": info.get("unit_name", "шт"),
                    "store_id": UUID(store_id_str) if store_id_str else None,
                    "store_name": item.get("store_name", "") or None,
                }
            )
        for i in range(0, len(product_rows), CHUNK):
            chunk = product_rows[i : i + CHUNK]
            stmt = pg_insert(PriceProduct).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["product_id"],
                set_={
                    "product_name": stmt.excluded.product_name,
                    "product_type": stmt.excluded.product_type,
                    "cost_price": stmt.excluded.cost_price,
                    "main_unit": stmt.excluded.main_unit,
                    "unit_name": stmt.excluded.unit_name,
                    "store_id": stmt.excluded.store_id,
                    "store_name": stmt.excluded.store_name,
                },
            )
            await session.execute(stmt)

        # ── 2. Replace price_supplier_column ──
        await session.execute(PriceSupplierColumn.__table__.delete())
        if active_supplier_ids:
            sup_rows = [
                {
                    "supplier_id": UUID(sid),
                    "supplier_name": supplier_name_map.get(sid, ""),
                    "column_index": idx,
                }
                for idx, sid in enumerate(sorted(active_supplier_ids))
            ]
            await session.execute(pg_insert(PriceSupplierColumn).values(sup_rows))

        # ── 3. Replace price_supplier_price ──
        await session.execute(PriceSupplierPrice.__table__.delete())
        price_rows: list[dict] = []
        for item in gsheet_data:
            pid = item["product_id"]
            for sid, price in item.get("supplier_prices", {}).items():
                if price and price > 0:
                    price_rows.append(
                        {
                            "product_id": UUID(pid),
                            "supplier_id": UUID(sid),
                            "price": price,
                        }
                    )
        price_count = len(price_rows)
        for i in range(0, len(price_rows), CHUNK):
            chunk = price_rows[i : i + CHUNK]
            await session.execute(pg_insert(PriceSupplierPrice).values(chunk))

        await session.commit()

    logger.info(
        "[invoice] Прайс-лист → БД: %d товаров, %d поставщиков, %d цен за %.2f сек",
        len(gsheet_data),
        len(active_supplier_ids),
        price_count,
        time.monotonic() - t0,
    )
    return len(gsheet_data)


# ═══════════════════════════════════════════════════════
# Запросы к прайс-листу из БД
# ═══════════════════════════════════════════════════════


async def get_price_list_suppliers() -> list[dict]:
    """
    Поставщики из прайс-листа (те, у кого назначены столбцы в GSheet).
    Возвращает [{id, name}, ...] отсортированные по имени.
    """
    async with async_session_factory() as session:
        stmt = select(
            PriceSupplierColumn.supplier_id, PriceSupplierColumn.supplier_name
        ).order_by(PriceSupplierColumn.supplier_name)
        rows = (await session.execute(stmt)).all()
    return [{"id": str(r.supplier_id), "name": r.supplier_name} for r in rows]


async def search_price_products(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск товаров в прайс-листе БД по подстроке имени.
    Возвращает [{id, name, cost_price, unit_name, main_unit, product_type}, ...].
    """
    pattern = query.strip().lower()
    if not pattern:
        return []

    async with async_session_factory() as session:
        stmt = (
            select(PriceProduct)
            .where(func.lower(PriceProduct.product_name).contains(pattern))
            .order_by(PriceProduct.product_name)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()

    return [
        {
            "id": str(r.product_id),
            "name": r.product_name,
            "cost_price": float(r.cost_price) if r.cost_price else 0.0,
            "unit_name": r.unit_name or "шт",
            "main_unit": str(r.main_unit) if r.main_unit else None,
            "product_type": r.product_type or "",
            "store_id": str(r.store_id) if r.store_id else "",
            "store_name": r.store_name or "",
        }
        for r in rows
    ]


async def get_supplier_prices(supplier_id: str) -> dict[str, float]:
    """
    Все цены поставщика из БД.
    Возвращает {product_id: price}.
    """
    async with async_session_factory() as session:
        stmt = select(PriceSupplierPrice.product_id, PriceSupplierPrice.price).where(
            PriceSupplierPrice.supplier_id == UUID(supplier_id)
        )
        rows = (await session.execute(stmt)).all()
    return {str(r.product_id): float(r.price) for r in rows}


async def get_cost_prices_bulk(product_ids: list[str]) -> dict[str, float]:
    """
    Получить себестоимости (cost_price) из PriceProduct для списка product_id.
    Возвращает {product_id: cost_price}.
    """
    if not product_ids:
        return {}
    uuids = []
    for pid in product_ids:
        try:
            uuids.append(UUID(pid))
        except (ValueError, AttributeError):
            continue
    if not uuids:
        return {}

    async with async_session_factory() as session:
        stmt = select(PriceProduct.product_id, PriceProduct.cost_price).where(
            PriceProduct.product_id.in_(uuids)
        )
        rows = (await session.execute(stmt)).all()

    return {
        str(r.product_id): float(r.cost_price)
        for r in rows
        if r.cost_price and float(r.cost_price) > 0
    }


async def get_supplier_prices_by_store(target_store_name: str) -> dict[str, float]:
    """
    Найти столбец поставщика по имени целевого склада и вернуть все цены.

    PriceSupplierColumn.supplier_name = iiko_supplier.name (= контрагент).
    Матчинг: exact → partial (contains).

    Возвращает {product_id: price} для всех товаров этого поставщика.
    """
    if not target_store_name:
        return {}
    name_lower = target_store_name.strip().lower()

    async with async_session_factory() as session:
        # Точное совпадение
        stmt = select(PriceSupplierColumn.supplier_id).where(
            func.lower(PriceSupplierColumn.supplier_name) == name_lower
        )
        row = (await session.execute(stmt)).first()
        if not row:
            # Частичное: supplier_name содержит store_name
            stmt = (
                select(PriceSupplierColumn.supplier_id)
                .where(
                    func.lower(PriceSupplierColumn.supplier_name).contains(name_lower)
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
        if not row:
            # Обратное: store_name содержит supplier_name
            stmt = select(
                PriceSupplierColumn.supplier_id,
                PriceSupplierColumn.supplier_name,
            )
            rows_all = (await session.execute(stmt)).all()
            for r in rows_all:
                if r.supplier_name and r.supplier_name.lower() in name_lower:
                    row = r
                    break
        if not row:
            logger.debug(
                "[invoice] Supplier column not found for store '%s'",
                target_store_name,
            )
            return {}

        # Получаем все цены этого поставщика
        stmt = select(PriceSupplierPrice.product_id, PriceSupplierPrice.price).where(
            PriceSupplierPrice.supplier_id == row.supplier_id
        )
        price_rows = (await session.execute(stmt)).all()

    result = {
        str(r.product_id): float(r.price)
        for r in price_rows
        if r.price and float(r.price) > 0
    }
    logger.debug(
        "[invoice] Supplier prices for store '%s': %d products",
        target_store_name,
        len(result),
    )
    return result


async def get_supplier_price_for_product(
    product_id: str,
    target_store_name: str,
) -> float | None:
    """
    Получить цену товара из столбца поставщика, соответствующего целевому складу.

    Возвращает float если цена найдена, иначе None.
    Используется при добавлении одного товара в заявку.
    """
    if not target_store_name or not product_id:
        return None
    name_lower = target_store_name.strip().lower()
    try:
        prod_uuid = UUID(product_id)
    except (ValueError, AttributeError):
        return None

    async with async_session_factory() as session:
        # Найти supplier_id из PriceSupplierColumn
        stmt = select(PriceSupplierColumn.supplier_id).where(
            func.lower(PriceSupplierColumn.supplier_name) == name_lower
        )
        row = (await session.execute(stmt)).first()
        if not row:
            stmt = (
                select(PriceSupplierColumn.supplier_id)
                .where(
                    func.lower(PriceSupplierColumn.supplier_name).contains(name_lower)
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
        if not row:
            stmt = select(
                PriceSupplierColumn.supplier_id,
                PriceSupplierColumn.supplier_name,
            )
            rows_all = (await session.execute(stmt)).all()
            for r in rows_all:
                if r.supplier_name and r.supplier_name.lower() in name_lower:
                    row = r
                    break
        if not row:
            return None

        # Получить цену для конкретного товара
        stmt = select(PriceSupplierPrice.price).where(
            PriceSupplierPrice.product_id == prod_uuid,
            PriceSupplierPrice.supplier_id == row.supplier_id,
        )
        price_row = (await session.execute(stmt)).first()
        if price_row and price_row.price and float(price_row.price) > 0:
            return float(price_row.price)
        return None


async def get_product_store_map(product_ids: list[str]) -> dict[str, dict[str, str]]:
    """
    Получить назначения складов для товаров из прайс-листа.
    Возвращает {product_id: {store_id, store_name}}.
    """
    if not product_ids:
        return {}
    uuids = []
    for pid in product_ids:
        try:
            uuids.append(UUID(pid))
        except (ValueError, AttributeError):
            continue
    if not uuids:
        return {}
    async with async_session_factory() as session:
        stmt = (
            select(
                PriceProduct.product_id, PriceProduct.store_id, PriceProduct.store_name
            )
            .where(PriceProduct.product_id.in_(uuids))
            .where(PriceProduct.store_id.isnot(None))
        )
        rows = (await session.execute(stmt)).all()
    return {
        str(r.product_id): {
            "store_id": str(r.store_id),
            "store_name": r.store_name or "",
        }
        for r in rows
    }


async def get_product_price_for_supplier(
    product_id: str,
    supplier_id: str,
) -> float | None:
    """Цена конкретного товара у конкретного поставщика."""
    async with async_session_factory() as session:
        stmt = (
            select(PriceSupplierPrice.price)
            .where(PriceSupplierPrice.product_id == UUID(product_id))
            .where(PriceSupplierPrice.supplier_id == UUID(supplier_id))
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
    return float(row) if row is not None else None


# ═══════════════════════════════════════════════════════
# Расходная накладная → iiko
# ═══════════════════════════════════════════════════════


async def get_product_containers(product_ids: list[str]) -> dict[str, str]:
    """
    Получить containerId для каждого продукта из iiko_product.raw_json->'containers'.
    Возвращает {product_id: container_id} для первого неудалённого контейнера.
    """
    if not product_ids:
        return {}

    from db.models import Product as IikoProduct

    uuids = []
    for pid in product_ids:
        try:
            uuids.append(UUID(pid))
        except (ValueError, AttributeError):
            continue

    t0 = time.monotonic()
    result: dict[str, str] = {}
    async with async_session_factory() as session:
        stmt = select(IikoProduct.id, IikoProduct.raw_json).where(
            IikoProduct.id.in_(uuids)
        )
        rows = (await session.execute(stmt)).all()

    for row in rows:
        pid_str = str(row.id)
        raw = row.raw_json or {}
        containers = raw.get("containers") or []
        # Первый не-deleted контейнер
        for c in containers:
            if not c.get("deleted", False):
                cid = c.get("id")
                if cid:
                    result[pid_str] = str(cid)
                    break

    elapsed = time.monotonic() - t0
    logger.info(
        "[invoice][containers] Найдено %d/%d контейнеров за %.2f сек",
        len(result),
        len(product_ids),
        elapsed,
    )
    for pid, cid in result.items():
        logger.debug("[invoice][containers]   prod=%s → container=%s", pid, cid)
    missing = set(product_ids) - set(result.keys())
    if missing:
        logger.warning("[invoice][containers] Без контейнеров: %s", missing)
    return result


def build_outgoing_invoice_document(
    *,
    store_id: str,
    counteragent_id: str,
    account_id: str,
    items: list[dict],
    containers: dict[str, str] | None = None,
    comment: str = "",
) -> dict:
    """
    Построить JSON-документ расходной накладной для iiko API.

    items: [{product_id, amount, price, main_unit}, ...]
    containers: {product_id: container_id} — тара из iiko_product.raw_json
    Фильтрует позиции с amount == 0.
    """
    from use_cases._helpers import now_kgd

    containers = containers or {}
    non_zero = [i for i in items if i.get("amount", 0) > 0]
    skipped = len(items) - len(non_zero)
    if skipped:
        logger.info("[invoice][build] Пропущено позиций с amount=0: %d", skipped)

    doc = {
        "dateIncoming": now_kgd().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "PROCESSED",
        "comment": comment,
        "storeId": store_id,
        "counteragentId": counteragent_id,
        "accountId": account_id,
        "items": [
            {
                "productId": item["product_id"],
                "amount": item["amount"],
                "price": item["price"],
                "sum": round(item["amount"] * item["price"], 2),
                "measureUnitId": item.get("main_unit", ""),
                "containerId": containers.get(item["product_id"], ""),
            }
            for item in non_zero
            if item.get("main_unit")
        ],
    }
    no_unit = sum(1 for i in non_zero if not i.get("main_unit"))
    if no_unit:
        logger.warning("[invoice][build] Пропущено позиций без main_unit: %d", no_unit)
    logger.info(
        "[invoice][build] Документ: date=%s, status=%s, store=%s, "
        "counteragent=%s, account=%s, items=%d (of %d input), comment='%s'",
        doc["dateIncoming"],
        doc["status"],
        store_id,
        counteragent_id,
        account_id,
        len(doc["items"]),
        len(items),
        comment[:80],
    )
    for idx, di in enumerate(doc["items"], 1):
        logger.debug(
            "[invoice][build]   #%d prod=%s amt=%.4g price=%.2f sum=%.2f unit=%s container=%s",
            idx,
            di["productId"],
            di["amount"],
            di["price"],
            di["sum"],
            di["measureUnitId"],
            di.get("containerId", ""),
        )
    return doc


async def send_outgoing_invoice_document(document: dict) -> str:
    """
    Отправить расходную накладную в iiko через adapter.
    Возвращает текстовый результат.
    """
    from adapters import iiko_api

    n_items = len(document.get("items", []))
    if n_items == 0:
        logger.warning(
            "[invoice][send] Документ пустой — все позиции с количеством 0 или без unit"
        )
        return "⚠️ Документ пустой (все позиции с количеством 0)"

    t0 = time.monotonic()
    logger.info(
        "[invoice] Отправка расходной накладной: store=%s, counteragent=%s, items=%d",
        document.get("storeId"),
        document.get("counteragentId"),
        n_items,
    )
    try:
        result = await iiko_api.send_outgoing_invoice(document)
        elapsed = time.monotonic() - t0
        if not result.get("ok"):
            err = result.get("error", "неизвестная ошибка")
            doc_num = result.get("documentNumber", "?")
            logger.error(
                "[invoice] ❌ Валидация iiko отклонила документ %s за %.2f сек: %s",
                doc_num,
                elapsed,
                err,
            )
            return f"❌ Ошибка валидации iiko: {err[:300]}"
        resp_info = result.get("response", "")
        logger.info(
            "[invoice] ✅ Расходная накладная отправлена за %.2f сек, resp=%s",
            elapsed,
            resp_info[:200],
        )
        return "✅ Расходная накладная создана в iiko (статус: Проведена)"
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception("[invoice] ❌ Ошибка отправки расходной за %.2f сек", elapsed)
        return f"❌ Ошибка отправки: {exc}"
