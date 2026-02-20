"""
Use-cases: синхронизация справочников iiko → PostgreSQL.

Архитектура:
  _run_sync()     — единый шаблон: fetch API → map → batch upsert → sync_log
  batch_upsert()  — generic INSERT … ON CONFLICT DO UPDATE батчами по BATCH_SIZE
  _map_*()        — маппинг dict из API → dict для таблицы
"""

import asyncio
import logging
import time
import uuid
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
    ProductGroup,
    Store,
    Supplier,
    SyncLog,
)
from use_cases._helpers import safe_uuid, safe_bool, safe_decimal, now_kgd

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

RowMapper = Callable[[dict, Any], dict | None]


# ═══════════════════════════════════════════════════════
# Generic batch upsert
# ═══════════════════════════════════════════════════════

async def batch_upsert(
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


async def mirror_delete(
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

    Безопасность:
      - если valid_ids пуст (API вернул 0) — пропускаем
      - если удалить пришлось бы >50% записей — это аномалия, пропускаем
    """
    if not valid_ids:
        logger.warning("[%s] Mirror-delete пропущен: пустой набор ID из API", label)
        return 0

    # Подсчёт текущих записей для sanity-check
    from sqlalchemy import func as sa_func, select as sa_select
    col = table.c[id_column]

    count_stmt = sa_select(sa_func.count()).select_from(table)
    if extra_filters:
        for col_name, val in extra_filters.items():
            count_stmt = count_stmt.where(table.c[col_name] == val)
    total_in_db = (await session.execute(count_stmt)).scalar() or 0

    to_stay = len(valid_ids)
    to_delete = max(0, total_in_db - to_stay)

    if total_in_db > 0 and to_delete > total_in_db * 0.5:
        logger.error(
            "[%s] Mirror-delete ЗАБЛОКИРОВАН: удалили бы %d из %d (>50%%). "
            "Возможен сбой API — пропускаем.",
            label, to_delete, total_in_db,
        )
        return 0

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
      3. batch_upsert()        — batch INSERT ON CONFLICT
      4. SyncLog               — в той же сессии (0 лишних round-trip)
    """
    started = now_kgd()
    t0 = time.monotonic()
    logger.info("[%s] Начинаю синхронизацию...", label)

    try:
        items = await fetch_coro
        t_api = time.monotonic() - t0
        logger.info("[%s] API: %d записей за %.1f сек", label, len(items), t_api)

        now = now_kgd()
        rows = [r for item in items if (r := mapper(item, now)) is not None]
        skipped = len(items) - len(rows)
        if skipped:
            logger.warning("[%s] Пропущено %d (невалидный UUID)", label, skipped)

        t1 = time.monotonic()
        async with async_session_factory() as session:
            count = await batch_upsert(table, rows, conflict_target, label, session)
            # Mirror-delete: удалить записи, которых больше нет в API
            valid_ids = {r[pk_column] for r in rows if r.get(pk_column) is not None}
            deleted = await mirror_delete(
                table, pk_column, valid_ids, label, session, mirror_scope,
            )
            # sync_log в той же сессии — экономим 1 round-trip
            session.add(SyncLog(
                entity_type=label,
                started_at=started,
                finished_at=now_kgd(),
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
                    finished_at=now_kgd(),
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
    def _map(item: dict, now) -> dict | None:
        uid = safe_uuid(item.get("id"))
        if not uid:
            return None
        return {
            "id": uid, "root_type": root_type,
            "name": item.get("name"), "code": item.get("code"),
            "deleted": safe_bool(item.get("deleted", False)),
            "synced_at": now, "raw_json": item,
        }
    return _map


def _map_supplier(item: dict, now) -> dict | None:
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "name": item.get("name"), "code": item.get("code"),
        "deleted": safe_bool(item.get("deleted", False)),
        "card_number": item.get("cardNumber"),
        "taxpayer_id_number": item.get("taxpayerIdNumber"),
        "snils": item.get("snils"),
        "synced_at": now, "raw_json": item,
    }


def _map_corporate(item: dict, now) -> dict | None:
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "parent_id": safe_uuid(item.get("parentId")),
        "name": item.get("name"), "code": item.get("code"),
        "department_type": item.get("type"),
        "deleted": safe_bool(item.get("deleted", False)),
        "synced_at": now, "raw_json": item,
    }


def _map_product(item: dict, now) -> dict | None:
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "parent_id": safe_uuid(item.get("parent")),
        "name": item.get("name"), "code": item.get("code"),
        "num": item.get("num"), "description": item.get("description"),
        "product_type": item.get("type"),
        "deleted": safe_bool(item.get("deleted", False)),
        "main_unit": safe_uuid(item.get("mainUnit")),
        "category": safe_uuid(item.get("category")),
        "accounting_category": safe_uuid(item.get("accountingCategory")),
        "tax_category": safe_uuid(item.get("taxCategory")),
        "default_sale_price": safe_decimal(item.get("defaultSalePrice")),
        "unit_weight": safe_decimal(item.get("unitWeight")),
        "unit_capacity": safe_decimal(item.get("unitCapacity")),
        "synced_at": now, "raw_json": item,
    }


def _map_product_group(item: dict, now) -> dict | None:
    """Маппинг номенклатурной группы из iiko API → БД."""
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid,
        "parent_id": safe_uuid(item.get("parent")),
        "name": item.get("name"),
        "code": item.get("code"),
        "num": item.get("num"),
        "description": item.get("description"),
        "deleted": safe_bool(item.get("deleted", False)),
        "category": safe_uuid(item.get("category")),
        "accounting_category": safe_uuid(item.get("accountingCategory")),
        "tax_category": safe_uuid(item.get("taxCategory")),
        "synced_at": now, "raw_json": item,
    }


def _map_employee(item: dict, now) -> dict | None:
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    parts = [item[f] for f in ("lastName", "firstName", "middleName") if item.get(f)]
    return {
        "id": uid,
        "name": " ".join(parts) if parts else item.get("name"),
        "code": item.get("code"),
        "deleted": safe_bool(item.get("deleted", False)),
        "first_name": item.get("firstName"),
        "middle_name": item.get("middleName"),
        "last_name": item.get("lastName"),
        "role_id": safe_uuid(item.get("mainRoleId")),
        "synced_at": now, "raw_json": item,
    }


def _map_role(item: dict, now) -> dict | None:
    uid = safe_uuid(item.get("id"))
    if not uid:
        return None
    return {
        "id": uid, "name": item.get("name"), "code": item.get("code"),
        "deleted": safe_bool(item.get("deleted", False)),
        "payment_per_hour": safe_decimal(item.get("paymentPerHour")),
        "steady_salary": safe_decimal(item.get("steadySalary")),
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
    started = now_kgd()

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
    now = now_kgd()
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
        total = await batch_upsert(
            Entity.__table__, all_rows, "uq_entity_id_root_type", "entities_all", session,
        )
        # Mirror-delete: удалить записи по root_type, которых больше нет в API
        total_deleted = 0
        for rt in ENTITY_ROOT_TYPES:
            raw = fetched[rt]
            if isinstance(raw, BaseException):
                continue
            rt_ids = {safe_uuid(item.get("id")) for item in raw} - {None}
            if rt_ids:
                total_deleted += await mirror_delete(
                    Entity.__table__, "id", rt_ids,
                    f"entity:{rt}", session,
                    extra_filters={"root_type": rt},
                )

        for rt, cnt in results.items():
            session.add(SyncLog(
                entity_type=rt,
                started_at=started,
                finished_at=now_kgd(),
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
    count = await _run_sync(
        "Department", iiko_api.fetch_departments(),
        Department.__table__, _map_corporate, ["id"], triggered_by,
    )
    # Обновляем список заведений для заявок в GSheet
    try:
        from use_cases.product_request import sync_request_stores_sheet
        await sync_request_stores_sheet()
    except Exception:
        logger.warning("[Department] Ошибка синхронизации заведений → GSheet Настройки", exc_info=True)
    return count


async def sync_stores(triggered_by: str | None = None) -> int:
    count = await _run_sync(
        "Store", iiko_api.fetch_stores(),
        Store.__table__, _map_corporate, ["id"], triggered_by,
    )
    return count


async def sync_groups(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Group", iiko_api.fetch_groups(),
        GroupDepartment.__table__, _map_corporate, ["id"], triggered_by,
    )


async def sync_product_groups(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "ProductGroup", iiko_api.fetch_product_groups(),
        ProductGroup.__table__, _map_product_group, ["id"], triggered_by,
    )


async def sync_products(triggered_by: str | None = None) -> int:
    return await _run_sync(
        "Product", iiko_api.fetch_products(include_deleted=False),
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


# ═══════════════════════════════════════════════════════
# Оркестрация для handler'ов (бизнес-логика отчётов)
# ═══════════════════════════════════════════════════════

async def sync_all_iiko_with_report(
    triggered_by: str,
) -> list[str]:
    """
    Полная синхронизация iiko — справочники + остальные параллельно.
    Возвращает список строк отчёта (для отображения в Telegram).
    """
    report: list[str] = []

    # 1) Справочники (уже внутри параллельные)
    try:
        results = await sync_all_entities(triggered_by=triggered_by)
        total = sum(v for v in results.values() if v >= 0)
        errors = sum(1 for v in results.values() if v < 0)
        report.append(f"📋 Справочники: {total} записей, ошибок: {errors}")
    except Exception:
        report.append("📋 Справочники: ❌ ошибка")

    # 2) Остальные 8 — параллельно через asyncio.gather
    sync_tasks = [
        ("🏢 Подразделения", sync_departments),
        ("🏪 Склады", sync_stores),
        ("👥 Группы", sync_groups),
        ("📁 Ном.группы", sync_product_groups),
        ("📦 Номенклатура", sync_products),
        ("🚚 Поставщики", sync_suppliers),
        ("👷 Сотрудники", sync_employees),
        ("🎭 Должности", sync_employee_roles),
    ]
    coros = [func(triggered_by=triggered_by) for _, func in sync_tasks]
    results_list = await asyncio.gather(*coros, return_exceptions=True)

    for (label, _), result in zip(sync_tasks, results_list):
        if isinstance(result, BaseException):
            report.append(f"{label}: ❌ {result}")
        else:
            report.append(f"{label}: ✅ {result} записей")

    return report


async def sync_everything_with_report(
    triggered_by: str,
) -> tuple[list[str], list[str]]:
    """
    Полная синхронизация iiko + FinTablo параллельно.
    Возвращает (iiko_lines, ft_lines) для отображения.
    """
    from use_cases import sync_fintablo as ft_uc

    async def _iiko_rest():
        tasks = [
            sync_departments, sync_stores, sync_groups,
            sync_product_groups, sync_products, sync_suppliers,
            sync_employees, sync_employee_roles,
        ]
        return await asyncio.gather(
            *[f(triggered_by=triggered_by) for f in tasks],
            return_exceptions=True,
        )

    iiko_entities_r, iiko_rest_r, ft_r = await asyncio.gather(
        sync_all_entities(triggered_by=triggered_by),
        _iiko_rest(),
        ft_uc.sync_all_fintablo(triggered_by=triggered_by),
        return_exceptions=True,
    )

    # ── iiko lines ──
    iiko_lines: list[str] = []
    if isinstance(iiko_entities_r, BaseException):
        iiko_lines.append("  📋 Справочники: ❌")
    else:
        total = sum(v for v in iiko_entities_r.values() if v >= 0)
        iiko_lines.append(f"  📋 Справочники: ✅ {total}")

    iiko_labels = [
        "🏢 Подразд.", "🏪 Склады", "👥 Группы", "📁 Ном.группы",
        "📦 Номенкл.", "🚚 Поставщ.", "👷 Сотрудн.", "🎭 Должности",
    ]
    if isinstance(iiko_rest_r, BaseException):
        for lb in iiko_labels:
            iiko_lines.append(f"  {lb}: ❌")
    else:
        for lb, r in zip(iiko_labels, iiko_rest_r):
            iiko_lines.append(f"  {lb}: {'✅ ' + str(r) if isinstance(r, int) else '❌'}")

    # ── FinTablo lines ──
    ft_lines: list[str] = []
    if isinstance(ft_r, BaseException):
        ft_lines.append("  ❌ Ошибка")
    else:
        for label, result in ft_r:
            if isinstance(result, int):
                ft_lines.append(f"  {label}: ✅ {result}")
            else:
                ft_lines.append(f"  {label}: {result}")

    return iiko_lines, ft_lines


async def bg_sync_for_documents(triggered_by: str) -> None:
    """Фоновая синхронизация номенклатуры и справочников при открытии раздела Документы."""
    logger.info("[bg] Фоновая синхронизация старт (%s)", triggered_by)
    try:
        await asyncio.gather(
            sync_products(triggered_by=triggered_by),
            sync_all_entities(triggered_by=triggered_by),
            return_exceptions=True,
        )
        logger.info("[documents] Фоновая синхронизация номенклатуры + справочников завершена (%s)", triggered_by)
    except Exception:
        logger.warning("[documents] Ошибка фоновой синхронизации", exc_info=True)
