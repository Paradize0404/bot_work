"""
Use-cases: синхронизация справочников FinTablo → PostgreSQL.

Архитектура как у iiko sync:
  _run_ft_sync()  — единый шаблон: fetch API → map → batch upsert → sync_log
  _batch_upsert() — переиспользуем из use_cases.sync
  _map_*()        — маппинг dict из API → dict для таблицы
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Coroutine

from adapters import fintablo_api
from db.engine import async_session_factory
from db.models import SyncLog
from use_cases.sync import _batch_upsert, _mirror_delete, _safe_decimal  # DRY: shared helpers
from db.ft_models import (
    FTCategory,
    FTMoneybag,
    FTPartner,
    FTDirection,
    FTMoneybagGroup,
    FTGoods,
    FTObtaining,
    FTJob,
    FTDeal,
    FTObligationStatus,
    FTObligation,
    FTPnlCategory,
    FTEmployee,
)

logger = logging.getLogger(__name__)

FTRowMapper = Callable[[dict, datetime], dict | None]


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════

def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════
# Generic sync runner
# ═══════════════════════════════════════════════════════

async def _run_ft_sync(
    label: str,
    fetch_coro: Coroutine,
    table,
    mapper: FTRowMapper,
    conflict_target: list[str] | str,
    triggered_by: str | None = None,
) -> int:
    """
    Единый шаблон синхронизации FinTablo:
      1. await fetch_coro      — получить данные из FinTablo API
      2. mapper()              — dict API → dict БД (None = пропустить)
      3. _batch_upsert()       — batch INSERT ON CONFLICT
      4. SyncLog               — аудит
    """
    started = datetime.utcnow()
    t0 = time.monotonic()
    logger.info("[FT:%s] Начинаю синхронизацию...", label)

    try:
        items = await fetch_coro
        t_api = time.monotonic() - t0
        logger.info("[FT:%s] API: %d записей за %.1f сек", label, len(items), t_api)

        now = datetime.utcnow()
        rows = [r for item in items if (r := mapper(item, now)) is not None]
        skipped = len(items) - len(rows)
        if skipped:
            logger.warning("[FT:%s] Пропущено %d (невалидный id)", label, skipped)

        t1 = time.monotonic()
        async with async_session_factory() as session:
            count = await _batch_upsert(table, rows, conflict_target, f"FT:{label}", session)
            # Mirror-delete: удалить записи, которых больше нет в API
            valid_ids = {r["id"] for r in rows if r.get("id") is not None}
            deleted = await _mirror_delete(table, "id", valid_ids, f"FT:{label}", session)
            session.add(SyncLog(
                entity_type=f"ft_{label}",
                started_at=started,
                finished_at=datetime.utcnow(),
                status="success",
                records_synced=count,
                triggered_by=triggered_by,
            ))
            await session.commit()

        logger.info("[FT:%s] БД: upsert %d, удалено %d за %.1f сек | Итого %.1f сек",
                    label, count, deleted, time.monotonic() - t1, time.monotonic() - t0)
        return count

    except Exception as exc:
        logger.exception("[FT:%s] ОШИБКА: %s", label, exc)
        try:
            async with async_session_factory() as session:
                session.add(SyncLog(
                    entity_type=f"ft_{label}",
                    started_at=started,
                    finished_at=datetime.utcnow(),
                    status="error",
                    error_message=str(exc)[:2000],
                    triggered_by=triggered_by,
                ))
                await session.commit()
        except Exception:
            logger.exception("[FT:%s] Не удалось записать ошибку в sync_log", label)
        raise


# ═══════════════════════════════════════════════════════
# Row mappers (dict API → dict DB; None = skip)
# ═══════════════════════════════════════════════════════

def _map_category(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "parent_id": _safe_int(item.get("parentId")),
        "group": item.get("group"), "type": item.get("type"),
        "pnl_type": item.get("pnlType"),
        "description": item.get("description"),
        "is_built_in": _safe_int(item.get("isBuiltIn")),
        "synced_at": now, "raw_json": item,
    }


def _map_moneybag(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "type": item.get("type"), "number": item.get("number"),
        "currency": item.get("currency"),
        "balance": _safe_decimal(item.get("balance")),
        "surplus": _safe_decimal(item.get("surplus")),
        "surplus_timestamp": _safe_int(item.get("surplusTimestamp")),
        "group_id": _safe_int(item.get("groupId")),
        "archived": _safe_int(item.get("archived")),
        "hide_in_total": _safe_int(item.get("hideInTotal")),
        "without_nds": _safe_int(item.get("withoutNds")),
        "synced_at": now, "raw_json": item,
    }


def _map_partner(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "inn": item.get("inn"),
        "group_id": _safe_int(item.get("groupId")),
        "comment": item.get("comment"),
        "synced_at": now, "raw_json": item,
    }


def _map_direction(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "parent_id": _safe_int(item.get("parentId")),
        "description": item.get("description"),
        "archived": _safe_int(item.get("archived")),
        "synced_at": now, "raw_json": item,
    }


def _map_moneybag_group(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "is_built_in": _safe_int(item.get("isBuiltIn")),
        "synced_at": now, "raw_json": item,
    }


def _map_goods(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "cost": _safe_decimal(item.get("cost")),
        "comment": item.get("comment"),
        "quantity": _safe_decimal(item.get("quantity")),
        "start_quantity": _safe_decimal(item.get("startQuantity")),
        "avg_cost": _safe_decimal(item.get("avgCost")),
        "synced_at": now, "raw_json": item,
    }


def _map_obtaining(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "goods_id": _safe_int(item.get("goodsId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "amount": _safe_decimal(item.get("amount")),
        "cost": _safe_decimal(item.get("cost")),
        "quantity": _safe_int(item.get("quantity")),
        "currency": item.get("currency"),
        "comment": item.get("comment"),
        "date": item.get("date"),
        "nds": _safe_decimal(item.get("nds")),
        "synced_at": now, "raw_json": item,
    }


def _map_job(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "cost": _safe_decimal(item.get("cost")),
        "comment": item.get("comment"),
        "direction_id": _safe_int(item.get("directionId")),
        "synced_at": now, "raw_json": item,
    }


def _map_deal(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "direction_id": _safe_int(item.get("directionId")),
        "amount": _safe_decimal(item.get("amount")),
        "currency": item.get("currency"),
        "custom_cost_price": _safe_decimal(item.get("customCostPrice")),
        "status_id": _safe_int(item.get("statusId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "responsible_id": _safe_int(item.get("responsibleId")),
        "comment": item.get("comment"),
        "start_date": item.get("startDate"),
        "end_date": item.get("endDate"),
        "act_date": item.get("actDate"),
        "nds": _safe_decimal(item.get("nds")),
        "synced_at": now, "raw_json": item,
    }


def _map_obligation_status(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "synced_at": now, "raw_json": item,
    }


def _map_obligation(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "category_id": _safe_int(item.get("categoryId")),
        "direction_id": _safe_int(item.get("directionId")),
        "deal_id": _safe_int(item.get("dealId")),
        "amount": _safe_decimal(item.get("amount")),
        "currency": item.get("currency"),
        "status_id": _safe_int(item.get("statusId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "comment": item.get("comment"),
        "act_date": item.get("actDate"),
        "nds": _safe_decimal(item.get("nds")),
        "synced_at": now, "raw_json": item,
    }


def _map_pnl_category(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "type": item.get("type"),
        "pnl_type": item.get("pnlType"),
        "category_id": _safe_int(item.get("categoryId")),
        "comment": item.get("comment"),
        "synced_at": now, "raw_json": item,
    }


def _map_employee(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid, "name": item.get("name"),
        "date": item.get("date"),
        "currency": item.get("currency"),
        "regularfix": _safe_decimal(item.get("regularfix")),
        "regularfee": _safe_decimal(item.get("regularfee")),
        "regulartax": _safe_decimal(item.get("regulartax")),
        "inn": item.get("inn"),
        "hired": item.get("hired"),
        "fired": item.get("fired"),
        "comment": item.get("comment"),
        "synced_at": now, "raw_json": item,
    }


# ═══════════════════════════════════════════════════════
# Public API (1 функция = 1 кнопка бота)
# ═══════════════════════════════════════════════════════

async def sync_ft_categories(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "category", fintablo_api.fetch_categories(),
        FTCategory.__table__, _map_category, ["id"], triggered_by,
    )


async def sync_ft_moneybags(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "moneybag", fintablo_api.fetch_moneybags(),
        FTMoneybag.__table__, _map_moneybag, ["id"], triggered_by,
    )


async def sync_ft_partners(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "partner", fintablo_api.fetch_partners(),
        FTPartner.__table__, _map_partner, ["id"], triggered_by,
    )


async def sync_ft_directions(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "direction", fintablo_api.fetch_directions(),
        FTDirection.__table__, _map_direction, ["id"], triggered_by,
    )


async def sync_ft_moneybag_groups(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "moneybag_group", fintablo_api.fetch_moneybag_groups(),
        FTMoneybagGroup.__table__, _map_moneybag_group, ["id"], triggered_by,
    )


async def sync_ft_goods(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "goods", fintablo_api.fetch_goods(),
        FTGoods.__table__, _map_goods, ["id"], triggered_by,
    )


async def sync_ft_obtainings(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obtaining", fintablo_api.fetch_obtainings(),
        FTObtaining.__table__, _map_obtaining, ["id"], triggered_by,
    )


async def sync_ft_jobs(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "job", fintablo_api.fetch_jobs(),
        FTJob.__table__, _map_job, ["id"], triggered_by,
    )


async def sync_ft_deals(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "deal", fintablo_api.fetch_deals(),
        FTDeal.__table__, _map_deal, ["id"], triggered_by,
    )


async def sync_ft_obligation_statuses(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obligation_status", fintablo_api.fetch_obligation_statuses(),
        FTObligationStatus.__table__, _map_obligation_status, ["id"], triggered_by,
    )


async def sync_ft_obligations(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obligation", fintablo_api.fetch_obligations(),
        FTObligation.__table__, _map_obligation, ["id"], triggered_by,
    )


async def sync_ft_pnl_categories(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "pnl_category", fintablo_api.fetch_pnl_categories(),
        FTPnlCategory.__table__, _map_pnl_category, ["id"], triggered_by,
    )


async def sync_ft_employees(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "employee", fintablo_api.fetch_employees(),
        FTEmployee.__table__, _map_employee, ["id"], triggered_by,
    )


# ═══════════════════════════════════════════════════════
# Параллельная синхронизация всего FinTablo
# ═══════════════════════════════════════════════════════

# Порядок: сначала справочники (от которых зависят другие), потом бизнес-данные
_FT_SYNC_TASKS = [
    ("📊 Статьи ДДС", sync_ft_categories),
    ("💰 Счета", sync_ft_moneybags),
    ("🤝 Контрагенты", sync_ft_partners),
    ("🎯 Направления", sync_ft_directions),
    ("📁 Группы счетов", sync_ft_moneybag_groups),
    ("📦 Товары", sync_ft_goods),
    ("🛒 Закупки", sync_ft_obtainings),
    ("🔧 Услуги", sync_ft_jobs),
    ("📝 Сделки", sync_ft_deals),
    ("🏷 Статусы обяз.", sync_ft_obligation_statuses),
    ("📋 Обязательства", sync_ft_obligations),
    ("📈 Статьи ПиУ", sync_ft_pnl_categories),
    ("👤 Сотрудники FT", sync_ft_employees),
]


async def sync_all_fintablo(triggered_by: str | None = None) -> list[tuple[str, int | str]]:
    """Параллельная синхронизация всех 13 справочников FinTablo."""
    t0 = time.monotonic()
    logger.info("=== FinTablo: запускаю %d синхронизаций параллельно ===", len(_FT_SYNC_TASKS))

    coros = [func(triggered_by=triggered_by) for _, func in _FT_SYNC_TASKS]
    results = await asyncio.gather(*coros, return_exceptions=True)

    report: list[tuple[str, int | str]] = []
    for (label, _), result in zip(_FT_SYNC_TASKS, results):
        if isinstance(result, BaseException):
            logger.error("[FT] %s: ошибка — %s", label, result)
            report.append((label, f"❌ {result}"))
        else:
            report.append((label, result))

    ok = sum(1 for _, r in report if isinstance(r, int))
    total_records = sum(r for _, r in report if isinstance(r, int))
    logger.info(
        "=== FinTablo: %d ok, %d err | %d записей | %.1f сек ===",
        ok, len(report) - ok, total_records, time.monotonic() - t0,
    )
    return report
