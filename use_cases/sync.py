"""
Use-cases: синхронизация справочников iiko → PostgreSQL.

Архитектура:
  _run_sync()     — единый шаблон: fetch API → map → batch upsert → sync_log
  _batch_upsert() — generic INSERT … ON CONFLICT DO UPDATE батчами по BATCH_SIZE
  _map_*()        — маппинг dict из API → dict для таблицы
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Coroutine

from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from adapters import iiko_api
from db.engine import async_session_factory
from db.models import (
    ENTITY_ROOT_TYPES,
    Department,
    Employee,
    EmployeeRole,
    Entity,
    GroupDepartment,
    Product,
    Store,
    Supplier,
    SyncLog,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

RowMapper = Callable[[dict, datetime], dict | None]


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

def _safe_uuid(v: Any) -> uuid.UUID | None:
    if v is None:
        return None
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError):
        return None


def _safe_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() == "true" if isinstance(v, str) else False


def _safe_decimal(v: Any):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════
# Generic batch upsert
# ═══════════════════════════════════════════════════════

async def _batch_upsert(
    table,
    rows: list[dict],
    conflict_target: list[str] | str,
    label: str,
    session: AsyncSession,
) -> int:
    """
    INSERT ... VALUES (...),(...) ON CONFLICT DO UPDATE
    батчами по BATCH_SIZE.

    conflict_target:
      - list[str]  → index_elements  (простой PK/unique index)
      - str        → constraint name (составной unique constraint)

    SET-clause строится автоматически из ключей rows (минус conflict-колонки).
    """
    if not rows:
        return 0

    # Определяем exclude-колонки для SET
    if isinstance(conflict_target, str):
        # constraint name — exclude не нужен, PG сам разберётся
        conflict_kw = {"constraint": conflict_target}
        exclude = set()   # обновляем всё кроме суррогатного pk
    else:
        conflict_kw = {"index_elements": conflict_target}
        exclude = set(conflict_target)

    # Колонки, которые НЕ нужно обновлять
    skip = exclude | {"pk"}

    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset : offset + BATCH_SIZE]
        stmt = pg_insert(table).values(batch)
        stmt = stmt.on_conflict_do_update(
            **conflict_kw,
            set_={k: getattr(stmt.excluded, k) for k in batch[0] if k not in skip},
        )
        await session.execute(stmt)
        done = min(offset + BATCH_SIZE, len(rows))
        logger.info("[%s] Batch: %d / %d", label, done, len(rows))

    return len(rows)


async def _mirror_delete(
    table,
    id_column: str,
    valid_ids: set,
    label: str,
    session: AsyncSession,
    extra_filters: dict[str, Any] | None = None,
) -> int:
    """
    Зеркальная очистка: удалить из БД записи, которых больше нет в API.
    DELETE FROM table WHERE id_column NOT IN (valid_ids)
    [AND extra_filter_col = val ...]

    Безопасность: если valid_ids пуст (API вернул 0) — пропускаем,
    чтобы не удалить всё при сбое API.
    """
    if not valid_ids:
        logger.warning("[%s] Mirror-delete пропущен: пустой набор ID из API", label)
        return 0

    col = table.c[id_column]
    stmt = sa_delete(table).where(col.notin_(list(valid_ids)))
    if extra_filters:
        for col_name, val in extra_filters.items():
            stmt = stmt.where(table.c[col_name] == val)

    result = await session.execute(stmt)
    count = result.rowcount
    if count:
        logger.info("[%s] Mirror-delete: удалено %d записей (нет в API)", label, count)
    else:
        logger.debug("[%s] Mirror-delete: удалять нечего", label)
    return count


# ═══════════════════════════════════════════════════════
# Generic sync runner
# ═══════════════════════════════════════════════════════

async def _run_sync(
    label: str,
    fetch_coro: Coroutine,
    table,
    mapper: RowMapper,
    conflict_target: list[str] | str,
    triggered_by: str | None = None,
    pk_column: str = "id",
    mirror_scope: dict[str, Any] | None = None,
) -> int:
    """
    Единый шаблон синхронизации:
      1. await fetch_coro      — получить данные из iiko API
      2. mapper()              — dict API → dict БД  (None = пропустить)
      3. _batch_upsert()       — batch INSERT ON CONFLICT
      4. SyncLog               — в той же сессии (0 лишних round-trip)
    """
    started = datetime.utcnow()
    t0 = time.monotonic()
    logger.info("[%s] Начинаю синхронизацию...", label)

    try:
        items = await fetch_coro
        t_api = time.monotonic() - t0
        logger.info("[%s] API: %d записей за %.1f сек", label, len(items), t_api)

        now = datetime.utcnow()
        rows = [r for item in items if (r := mapper(item, now)) is not None]
        skipped = len(items) - len(rows)
        if skipped:
            logger.warning("[%s] Пропущено %d (невалидный UUID)", label, skipped)

        t1 = time.monotonic()
        async with async_session_factory() as session:
            count = await _batch_upsert(table, rows, conflict_target, label, session)
            # Mirror-delete: удалить записи, которых больше нет в API
            valid_ids = {r[pk_column] for r in rows if r.get(pk_column) is not None}
            deleted = await _mirror_delete(
                table, pk_column, valid_ids, label, session, mirror_scope,
            )
            # sync_log в той же сессии — экономим 1 round-trip
            session.add(SyncLog(
                entity_type=label,
                started_at=started,
                finished_at=datetime.utcnow(),
                status="success",
                records_synced=count,
                triggered_by=triggered_by,
            ))
            await session.commit()

        logger.info("[%s] БД: upsert %d, удалено %d за %.1f сек | Итого %.1f сек",
                    label, count, deleted, time.monotonic() - t1, time.monotonic() - t0)
        return count

    except Exception as exc:
        logger.exception("[%s] ОШИБКА: %s", label, exc)
        try:
            async with async_session_factory() as session:
                session.add(SyncLog(
                    entity_type=label,
                    started_at=started,
                    finished_at=datetime.utcnow(),
                    status="error",
                    error_message=str(exc)[:2000],
                    triggered_by=triggered_by,
                ))
                await session.commit()
        except Exception:
            logger.exception("[%s] Не удалось записать ошибку в sync_log", label)
        raise


# ═══════════════════════════════════════════════════════
# Row mappers  (dict API → dict DB;  None = skip)
# ═══════════════════════════════════════════════════════

def _entity_mapper(root_type: str) -> RowMapper:
    """Фабрика маппера для конкретного rootType."""
    def _map(item: dict, now: datetime) -> dict | None:
        uid = _safe_uuid(item.get("id"))
        if not uid:
            return None
        return {
            "id": uid, "root_type": root_type,
            "name": item.get("name"), "code": item.get("code"),
            "deleted": _safe_bool(item.get("deleted", False)),
            "synced_at": now, "raw_json": item,
        }
    return _map


def _map_supplier(item: dict, now: datetime) -> dict | None:
    uid = _safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "name": item.get("name"), "code": item.get("code"),
        "deleted": _safe_bool(item.get("deleted", False)),
        "card_number": item.get("cardNumber"),
        "taxpayer_id_number": item.get("taxpayerIdNumber"),
        "snils": item.get("snils"),
        "synced_at": now, "raw_json": item,
    }


def _map_corporate(item: dict, now: datetime) -> dict | None:
    uid = _safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "parent_id": _safe_uuid(item.get("parentId")),
        "name": item.get("name"), "code": item.get("code"),
        "department_type": item.get("type"),
        "deleted": _safe_bool(item.get("deleted", False)),
        "synced_at": now, "raw_json": item,
    }


def _map_product(item: dict, now: datetime) -> dict | None:
    uid = _safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "parent_id": _safe_uuid(item.get("parent")),
        "name": item.get("name"), "code": item.get("code"),
        "num": item.get("num"), "description": item.get("description"),
        "product_type": item.get("type"),
        "deleted": _safe_bool(item.get("deleted", False)),
        "main_unit": _safe_uuid(item.get("mainUnit")),
        "category": _safe_uuid(item.get("category")),
        "accounting_category": _safe_uuid(item.get("accountingCategory")),
        "tax_category": _safe_uuid(item.get("taxCategory")),
        "default_sale_price": _safe_decimal(item.get("defaultSalePrice")),
        "unit_weight": _safe_decimal(item.get("unitWeight")),
        "unit_capacity": _safe_decimal(item.get("unitCapacity")),
        "synced_at": now, "raw_json": item,
    }


def _map_employee(item: dict, now: datetime) -> dict | None:
    uid = _safe_uuid(item.get("id"))
    if not uid:
        return None
    parts = [item[f] for f in ("lastName", "firstName", "middleName") if item.get(f)]
    return {
        "id": uid,
        "name": " ".join(parts) if parts else item.get("name"),
        "code": item.get("code"),
        "deleted": _safe_bool(item.get("deleted", False)),
        "first_name": item.get("firstName"),
        "middle_name": item.get("middleName"),
        "last_name": item.get("lastName"),
        "role_id": _safe_uuid(item.get("mainRoleId")),
        "synced_at": now, "raw_json": item,
    }


def _map_role(item: dict, now: datetime) -> dict | None:
    uid = _safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "name": item.get("name"), "code": item.get("code"),
        "deleted": _safe_bool(item.get("deleted", False)),
        "payment_per_hour": _safe_decimal(item.get("paymentPerHour")),
        "steady_salary": _safe_decimal(item.get("steadySalary")),
        "schedule_type": item.get("scheduleType"),
        "synced_at": now, "raw_json": item,
    }


# ═══════════════════════════════════════════════════════
# Public API  (1 функция = 1 кнопка бота)
# ═══════════════════════════════════════════════════════

async def sync_entity_list(root_type: str, triggered_by: str | None = None) -> int:
    if root_type not in ENTITY_ROOT_TYPES:
        raise ValueError(f"Unknown rootType: {root_type}")
    return await _run_sync(
        root_type, iiko_api.fetch_entities(root_type),
        Entity.__table__, _entity_mapper(root_type),
        "uq_entity_id_root_type", triggered_by,
        mirror_scope={"root_type": root_type},
    )


async def sync_all_entities(triggered_by: str | None = None) -> dict[str, int]:
    """Fetch all 16 rootTypes in parallel, upsert in one transaction."""
    t0 = time.monotonic()
    started = datetime.utcnow()

    # 1) Параллельно забираем все 16 типов из API
    logger.info("=== Справочники: загружаю %d типов параллельно ===", len(ENTITY_ROOT_TYPES))
    coros = [iiko_api.fetch_entities(rt) for rt in ENTITY_ROOT_TYPES]
    fetched = dict(zip(
        ENTITY_ROOT_TYPES,
        await asyncio.gather(*coros, return_exceptions=True),
    ))
    t_api = time.monotonic() - t0
    logger.info("=== API: все %d типов за %.1f сек ===", len(ENTITY_ROOT_TYPES), t_api)

    # 2) Маппим все в строки для БД
    now = datetime.utcnow()
    results: dict[str, int] = {}
    all_rows: list[dict] = []

    for rt in ENTITY_ROOT_TYPES:
        items = fetched[rt]
        if isinstance(items, BaseException):
            logger.error("[%s] Ошибка API: %s", rt, items)
            results[rt] = -1
            continue
        mapper = _entity_mapper(rt)
        rows = [r for item in items if (r := mapper(item, now)) is not None]
        all_rows.extend(rows)
        results[rt] = len(rows)
        logger.info("[%s] %d записей", rt, len(rows))

    # 3) Один batch INSERT + sync_log для всех — 1 COMMIT
    t1 = time.monotonic()
    async with async_session_factory() as session:
        total = await _batch_upsert(
            Entity.__table__, all_rows, "uq_entity_id_root_type", "entities_all", session,
        )
        # Mirror-delete: удалить записи по root_type, которых больше нет в API
        total_deleted = 0
        for rt in ENTITY_ROOT_TYPES:
            raw = fetched[rt]
            if isinstance(raw, BaseException):
                continue
            rt_ids = {_safe_uuid(item.get("id")) for item in raw} - {None}
            if rt_ids:
                total_deleted += await _mirror_delete(
                    Entity.__table__, "id", rt_ids,
                    f"entity:{rt}", session,
                    extra_filters={"root_type": rt},
                )

        for rt, cnt in results.items():
            session.add(SyncLog(
                entity_type=rt,
                started_at=started,
                finished_at=datetime.utcnow(),
                status="success" if cnt >= 0 else "error",
                records_synced=cnt if cnt >= 0 else None,
                error_message=str(fetched[rt])[:2000] if cnt < 0 else None,
                triggered_by=triggered_by,
            ))
        await session.commit()

    ok = sum(1 for v in results.values() if v >= 0)
    logger.info(
        "=== Справочники: %d ok, %d err | %d записей, удалено %d | %.1f сек (API %.1f + БД %.1f) ===",
        ok, len(results) - ok, total, total_deleted,
        time.monotonic() - t0, t_api, time.monotonic() - t1,
    )
    return results


async def sync_suppliers(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Supplier", iiko_api.fetch_suppliers(),
        Supplier.__table__, _map_supplier, ["id"], triggered_by,
    )


async def sync_departments(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Department", iiko_api.fetch_departments(),
        Department.__table__, _map_corporate, ["id"], triggered_by,
    )


async def sync_stores(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Store", iiko_api.fetch_stores(),
        Store.__table__, _map_corporate, ["id"], triggered_by,
    )


async def sync_groups(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Group", iiko_api.fetch_groups(),
        GroupDepartment.__table__, _map_corporate, ["id"], triggered_by,
    )


async def sync_products(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Product", iiko_api.fetch_products(include_deleted=True),
        Product.__table__, _map_product, ["id"], triggered_by,
    )


async def sync_employees(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Employee", iiko_api.fetch_employees(include_deleted=False),
        Employee.__table__, _map_employee, ["id"], triggered_by,
    )


async def sync_employee_roles(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "EmployeeRole", iiko_api.fetch_employee_roles(),
        EmployeeRole.__table__, _map_role, ["id"], triggered_by,
    )
