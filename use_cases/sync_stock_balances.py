"""
Use-case: синхронизация остатков по складам → PostgreSQL.

Источник: GET /resto/api/v2/reports/balance/stores?timestamp=...
Ответ: [{store: UUID, product: UUID, amount: float, sum: float}, ...]

Параметр timestamp:
  - Формат: yyyy-MM-dd'T'HH:mm:ss (учётная дата-время)
  - По умолчанию: datetime.now() (текущие остатки, как в UI iiko)
  - ВАЖНО: если передать только дату (yyyy-MM-dd), iiko интерпретирует
    как 00:00:00 = начало дня, и сегодняшние проводки НЕ будут учтены!

Паттерн: full-replace (DELETE + batch INSERT) в одной транзакции.
Имена складов и товаров резолвятся из iiko_store / iiko_product.

Фильтрация: заносим только строки с amount ≠ 0
(может быть < 0 — пересорт,  > 0 — обычный остаток).
"""

import logging
import time
import uuid as _uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete as sa_delete, select

from adapters import iiko_api
from db.engine import async_session_factory
from db.models import Product, Store, StockBalance, SyncLog

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
LABEL = "StockBalance"


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_uuid(v: Any) -> _uuid.UUID | None:
    if v is None:
        return None
    try:
        return _uuid.UUID(str(v))
    except (ValueError, AttributeError):
        return None


async def _load_name_maps(session) -> tuple[dict, dict]:
    """
    Загрузить справочники store_id→name и product_id→name из БД.
    Один round-trip на каждый справочник.
    """
    t0 = time.monotonic()

    store_rows = await session.execute(
        select(Store.id, Store.name).where(Store.deleted == False)  # noqa: E712
    )
    store_map = {row.id: row.name for row in store_rows.all()}

    product_rows = await session.execute(
        select(Product.id, Product.name).where(Product.deleted == False)  # noqa: E712
    )
    product_map = {row.id: row.name for row in product_rows.all()}

    logger.info(
        "[%s] Загружены справочники: %d складов, %d товаров за %.1f сек",
        LABEL, len(store_map), len(product_map), time.monotonic() - t0,
    )
    return store_map, product_map


# ═══════════════════════════════════════════════════════
# Batch INSERT (без ON CONFLICT — после full DELETE)
# ═══════════════════════════════════════════════════════

async def _batch_insert(session, rows: list[dict]) -> int:
    """Plain batch INSERT (быстрее upsert после TRUNCATE)."""
    if not rows:
        return 0
    table = StockBalance.__table__
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset : offset + BATCH_SIZE]
        await session.execute(table.insert(), batch)
        done = min(offset + BATCH_SIZE, len(rows))
        logger.info("[%s] Insert batch: %d / %d", LABEL, done, len(rows))
    return len(rows)


# ═══════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════

async def sync_stock_balances(
    triggered_by: str | None = None,
    timestamp: str | None = None,
) -> int:
    """
    Полная синхронизация остатков:
      1. GET /v2/reports/balance/stores  (iiko API)
         timestamp по умолчанию = datetime.now() (текущий момент)
      2. SELECT store/product names      (БД, 2 запроса)
      3. Фильтрация (amount ≠ 0)
      4. DELETE all + batch INSERT       (одна транзакция)
      5. SyncLog

    Returns: количество записанных строк.
    """
    started = datetime.utcnow()
    t0 = time.monotonic()
    logger.info("[%s] Начинаю синхронизацию остатков (timestamp=%s)...", LABEL, timestamp or "now")

    try:
        # 1. Параллельно: API + справочники из БД (независимые операции)
        import asyncio as _aio

        async def _fetch_api():
            return await iiko_api.fetch_stock_balances(timestamp=timestamp)

        async def _fetch_names():
            async with async_session_factory() as s:
                return await _load_name_maps(s)

        items, (store_map, product_map) = await _aio.gather(
            _fetch_api(), _fetch_names(),
        )
        t_api = time.monotonic() - t0
        logger.info("[%s] API + справочники: %d строк за %.1f сек", LABEL, len(items), t_api)

        # 2–4. В одной сессии: map + delete + insert
        t1 = time.monotonic()
        async with async_session_factory() as session:

            now = datetime.utcnow()
            rows: list[dict] = []
            skipped = 0
            no_name = 0

            for item in items:
                amount = _safe_float(item.get("amount"))
                if amount is None or amount == 0:
                    skipped += 1
                    continue

                store_id = _safe_uuid(item.get("store"))
                product_id = _safe_uuid(item.get("product"))
                if not store_id or not product_id:
                    skipped += 1
                    continue

                store_name = store_map.get(store_id)
                product_name = product_map.get(product_id)
                if not store_name or not product_name:
                    no_name += 1
                    # Всё равно заносим — UUID есть, имя подтянется после sync складов/товаров
                    store_name = store_name or f"unknown:{store_id}"
                    product_name = product_name or f"unknown:{product_id}"

                rows.append({
                    "store_id": store_id,
                    "store_name": store_name,
                    "product_id": product_id,
                    "product_name": product_name,
                    "amount": amount,
                    "money": _safe_float(item.get("sum")),
                    "synced_at": now,
                    "raw_json": item,
                })

            logger.info(
                "[%s] После фильтрации: %d строк (пропущено %d c amount=0/невалид, "
                "%d без имени в справочнике)",
                LABEL, len(rows), skipped, no_name,
            )

            # DELETE all → INSERT
            result = await session.execute(sa_delete(StockBalance.__table__))
            deleted = result.rowcount
            logger.info("[%s] Удалено старых записей: %d", LABEL, deleted)

            count = await _batch_insert(session, rows)

            # SyncLog
            session.add(SyncLog(
                entity_type=LABEL,
                started_at=started,
                finished_at=datetime.utcnow(),
                status="success",
                records_synced=count,
                triggered_by=triggered_by,
            ))
            await session.commit()

        t_db = time.monotonic() - t1
        logger.info(
            "[%s] Готово: %d записей | удалено %d | API %.1f сек, БД %.1f сек | Итого %.1f сек",
            LABEL, count, deleted, t_api, t_db, time.monotonic() - t0,
        )
        return count

    except Exception as exc:
        logger.exception("[%s] ОШИБКА: %s", LABEL, exc)
        try:
            async with async_session_factory() as session:
                session.add(SyncLog(
                    entity_type=LABEL,
                    started_at=started,
                    finished_at=datetime.utcnow(),
                    status="error",
                    error_message=str(exc)[:2000],
                    triggered_by=triggered_by,
                ))
                await session.commit()
        except Exception:
            logger.exception("[%s] Не удалось записать ошибку в sync_log", LABEL)
        raise


# ═══════════════════════════════════════════════════════
# Query helpers (для бота / отчётов)
# ═══════════════════════════════════════════════════════

async def get_stock_by_store(store_name: str) -> list[dict[str, Any]]:
    """
    Остатки для конкретного склада (из БД).
    Быстро — без запросов к iiko API.
    """
    async with async_session_factory() as session:
        stmt = (
            select(
                StockBalance.product_name,
                StockBalance.amount,
                StockBalance.money,
            )
            .where(StockBalance.store_name == store_name)
            .order_by(StockBalance.product_name)
        )
        result = await session.execute(stmt)
        return [dict(row._mapping) for row in result.all()]


async def get_stores_with_stock() -> list[dict[str, Any]]:
    """Список складов, у которых есть остатки, с подсчётом позиций."""
    from sqlalchemy import func
    async with async_session_factory() as session:
        stmt = (
            select(
                StockBalance.store_id,
                StockBalance.store_name,
                func.count().label("product_count"),
                func.sum(StockBalance.money).label("total_money"),
            )
            .group_by(StockBalance.store_id, StockBalance.store_name)
            .order_by(StockBalance.store_name)
        )
        result = await session.execute(stmt)
        return [dict(row._mapping) for row in result.all()]


async def get_stock_summary() -> dict[str, Any]:
    """Сводка: сколько складов, позиций, общая сумма."""
    from sqlalchemy import func, distinct
    async with async_session_factory() as session:
        stmt = select(
            func.count(distinct(StockBalance.store_id)).label("stores"),
            func.count().label("products"),
            func.sum(StockBalance.money).label("total_money"),
        )
        row = (await session.execute(stmt)).one()
        return dict(row._mapping)
