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

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import delete as sa_delete, select

from adapters import iiko_api
from db.engine import async_session_factory
from db.models import Product, Store, StockBalance, SyncLog
from use_cases._helpers import safe_float, safe_uuid, now_kgd

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
LABEL = "StockBalance"


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════


async def _load_name_maps() -> tuple[dict, dict]:
    """
    Загрузить справочники store_id→name и product_id→name из БД.
    Два запроса параллельно через asyncio.gather.
    """
    t0 = time.monotonic()

    async def _stores():
        async with async_session_factory() as s:
            rows = await s.execute(
                select(Store.id, Store.name).where(Store.deleted == False)  # noqa: E712
            )
            return {r.id: r.name for r in rows.all()}

    async def _products():
        async with async_session_factory() as s:
            rows = await s.execute(
                select(Product.id, Product.name).where(
                    Product.deleted == False
                )  # noqa: E712
            )
            return {r.id: r.name for r in rows.all()}

    store_map, product_map = await asyncio.gather(_stores(), _products())

    logger.info(
        "[%s] Загружены справочники: %d складов, %d товаров за %.1f сек",
        LABEL,
        len(store_map),
        len(product_map),
        time.monotonic() - t0,
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
    started = now_kgd()
    t0 = time.monotonic()
    logger.info(
        "[%s] Начинаю синхронизацию остатков (timestamp=%s)...",
        LABEL,
        timestamp or "now",
    )

    try:
        # 1. Параллельно: API + справочники из БД (независимые операции)
        items, (store_map, product_map) = await asyncio.gather(
            iiko_api.fetch_stock_balances(timestamp=timestamp),
            _load_name_maps(),
        )
        t_api = time.monotonic() - t0
        logger.info(
            "[%s] API + справочники: %d строк за %.1f сек", LABEL, len(items), t_api
        )

        # 2–4. В одной сессии: map + delete + insert
        t1 = time.monotonic()
        async with async_session_factory() as session:

            now = now_kgd()
            rows: list[dict] = []
            skipped = 0
            no_name = 0

            for item in items:
                amount = safe_float(item.get("amount"))
                if amount is None or amount == 0:
                    skipped += 1
                    continue

                store_id = safe_uuid(item.get("store"))
                product_id = safe_uuid(item.get("product"))
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

                rows.append(
                    {
                        "store_id": store_id,
                        "store_name": store_name,
                        "product_id": product_id,
                        "product_name": product_name,
                        "amount": amount,
                        "money": safe_float(item.get("sum")),
                        "synced_at": now,
                        "raw_json": item,
                    }
                )

            logger.info(
                "[%s] После фильтрации: %d строк (пропущено %d c amount=0/невалид, "
                "%d без имени в справочнике)",
                LABEL,
                len(rows),
                skipped,
                no_name,
            )

            # DELETE all → INSERT
            result = await session.execute(sa_delete(StockBalance.__table__))
            deleted = result.rowcount
            logger.info("[%s] Удалено старых записей: %d", LABEL, deleted)

            count = await _batch_insert(session, rows)

            # SyncLog
            session.add(
                SyncLog(
                    entity_type=LABEL,
                    started_at=started,
                    finished_at=now_kgd(),
                    status="success",
                    records_synced=count,
                    triggered_by=triggered_by,
                )
            )
            await session.commit()

        t_db = time.monotonic() - t1
        logger.info(
            "[%s] Готово: %d записей | удалено %d | API %.1f сек, БД %.1f сек | Итого %.1f сек",
            LABEL,
            count,
            deleted,
            t_api,
            t_db,
            time.monotonic() - t0,
        )
        return count

    except Exception as exc:
        logger.exception("[%s] ОШИБКА: %s", LABEL, exc)
        try:
            async with async_session_factory() as session:
                session.add(
                    SyncLog(
                        entity_type=LABEL,
                        started_at=started,
                        finished_at=now_kgd(),
                        status="error",
                        error_message=str(exc)[:2000],
                        triggered_by=triggered_by,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("[%s] Не удалось записать ошибку в sync_log", LABEL)
        raise
