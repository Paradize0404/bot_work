"""
Use-cases: СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ СЃРїСЂР°РІРѕС‡РЅРёРєРѕРІ FinTablo в†’ PostgreSQL.

РђСЂС…РёС‚РµРєС‚СѓСЂР° РєР°Рє Сѓ iiko sync:
  _run_ft_sync()  вЂ” РµРґРёРЅС‹Р№ С€Р°Р±Р»РѕРЅ: fetch API в†’ map в†’ batch upsert в†’ sync_log
  batch_upsert()  вЂ” РїРµСЂРµРёСЃРїРѕР»СЊР·СѓРµРј РёР· use_cases.sync
  _map_*()        вЂ” РјР°РїРїРёРЅРі dict РёР· API в†’ dict РґР»СЏ С‚Р°Р±Р»РёС†С‹
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Coroutine

from adapters import fintablo_api
from db.engine import async_session_factory
from db.models import SyncLog
from use_cases.sync import batch_upsert, mirror_delete
from use_cases._helpers import safe_int as _safe_int, safe_decimal, now_kgd
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


# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ
# Generic sync runner
# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ


async def _run_ft_sync(
    label: str,
    fetch_coro: Coroutine,
    table,
    mapper: FTRowMapper,
    conflict_target: list[str] | str,
    triggered_by: str | None = None,
) -> int:
    """
    Р•РґРёРЅС‹Р№ С€Р°Р±Р»РѕРЅ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёРё FinTablo:
      1. await fetch_coro      вЂ” РїРѕР»СѓС‡РёС‚СЊ РґР°РЅРЅС‹Рµ РёР· FinTablo API
      2. mapper()              вЂ” dict API в†’ dict Р‘Р” (None = РїСЂРѕРїСѓСЃС‚РёС‚СЊ)
      3. batch_upsert()       вЂ” batch INSERT ON CONFLICT
      4. SyncLog               вЂ” Р°СѓРґРёС‚
    """
    started = now_kgd()
    t0 = time.monotonic()
    logger.info("[FT:%s] РќР°С‡РёРЅР°СЋ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЋ...", label)

    try:
        items = await fetch_coro
        t_api = time.monotonic() - t0
        logger.info(
            "[FT:%s] API: %d Р·Р°РїРёСЃРµР№ Р·Р° %.1f СЃРµРє", label, len(items), t_api
        )

        now = now_kgd()
        rows = [r for item in items if (r := mapper(item, now)) is not None]
        skipped = len(items) - len(rows)
        if skipped:
            logger.warning(
                "[FT:%s] РџСЂРѕРїСѓС‰РµРЅРѕ %d (РЅРµРІР°Р»РёРґРЅС‹Р№ id)",
                label,
                skipped,
            )

        t1 = time.monotonic()
        async with async_session_factory() as session:
            count = await batch_upsert(
                table, rows, conflict_target, f"FT:{label}", session
            )
            # Mirror-delete: СѓРґР°Р»РёС‚СЊ Р·Р°РїРёСЃРё, РєРѕС‚РѕСЂС‹С… Р±РѕР»СЊС€Рµ РЅРµС‚ РІ API
            valid_ids = {r["id"] for r in rows if r.get("id") is not None}
            deleted = await mirror_delete(
                table, "id", valid_ids, f"FT:{label}", session
            )
            session.add(
                SyncLog(
                    entity_type=f"ft_{label}",
                    started_at=started,
                    finished_at=now_kgd(),
                    status="success",
                    records_synced=count,
                    triggered_by=triggered_by,
                )
            )
            await session.commit()

        logger.info(
            "[FT:%s] Р‘Р”: upsert %d, СѓРґР°Р»РµРЅРѕ %d Р·Р° %.1f СЃРµРє | РС‚РѕРіРѕ %.1f СЃРµРє",
            label,
            count,
            deleted,
            time.monotonic() - t1,
            time.monotonic() - t0,
        )
        return count

    except Exception as exc:
        logger.exception("[FT:%s] РћРЁРР‘РљРђ: %s", label, exc)
        try:
            async with async_session_factory() as session:
                session.add(
                    SyncLog(
                        entity_type=f"ft_{label}",
                        started_at=started,
                        finished_at=now_kgd(),
                        status="error",
                        error_message=str(exc)[:2000],
                        triggered_by=triggered_by,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception(
                "[FT:%s] РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РїРёСЃР°С‚СЊ РѕС€РёР±РєСѓ РІ sync_log",
                label,
            )
        raise


# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ
# Row mappers (dict API в†’ dict DB; None = skip)
# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ


def _map_category(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "parent_id": _safe_int(item.get("parentId")),
        "group": item.get("group"),
        "type": item.get("type"),
        "pnl_type": item.get("pnlType"),
        "description": item.get("description"),
        "is_built_in": _safe_int(item.get("isBuiltIn")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_moneybag(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "type": item.get("type"),
        "number": item.get("number"),
        "currency": item.get("currency"),
        "balance": safe_decimal(item.get("balance")),
        "surplus": safe_decimal(item.get("surplus")),
        "surplus_timestamp": _safe_int(item.get("surplusTimestamp")),
        "group_id": _safe_int(item.get("groupId")),
        "archived": _safe_int(item.get("archived")),
        "hide_in_total": _safe_int(item.get("hideInTotal")),
        "without_nds": _safe_int(item.get("withoutNds")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_partner(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "inn": item.get("inn"),
        "group_id": _safe_int(item.get("groupId")),
        "comment": item.get("comment"),
        "synced_at": now,
        "raw_json": item,
    }


def _map_direction(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "parent_id": _safe_int(item.get("parentId")),
        "description": item.get("description"),
        "archived": _safe_int(item.get("archived")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_moneybag_group(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "is_built_in": _safe_int(item.get("isBuiltIn")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_goods(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "cost": safe_decimal(item.get("cost")),
        "comment": item.get("comment"),
        "quantity": safe_decimal(item.get("quantity")),
        "start_quantity": safe_decimal(item.get("startQuantity")),
        "avg_cost": safe_decimal(item.get("avgCost")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_obtaining(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "goods_id": _safe_int(item.get("goodsId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "amount": safe_decimal(item.get("amount")),
        "cost": safe_decimal(item.get("cost")),
        "quantity": _safe_int(item.get("quantity")),
        "currency": item.get("currency"),
        "comment": item.get("comment"),
        "date": item.get("date"),
        "nds": safe_decimal(item.get("nds")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_job(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "cost": safe_decimal(item.get("cost")),
        "comment": item.get("comment"),
        "direction_id": _safe_int(item.get("directionId")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_deal(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "direction_id": _safe_int(item.get("directionId")),
        "amount": safe_decimal(item.get("amount")),
        "currency": item.get("currency"),
        "custom_cost_price": safe_decimal(item.get("customCostPrice")),
        "status_id": _safe_int(item.get("statusId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "responsible_id": _safe_int(item.get("responsibleId")),
        "comment": item.get("comment"),
        "start_date": item.get("startDate"),
        "end_date": item.get("endDate"),
        "act_date": item.get("actDate"),
        "nds": safe_decimal(item.get("nds")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_obligation_status(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "synced_at": now,
        "raw_json": item,
    }


def _map_obligation(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "category_id": _safe_int(item.get("categoryId")),
        "direction_id": _safe_int(item.get("directionId")),
        "deal_id": _safe_int(item.get("dealId")),
        "amount": safe_decimal(item.get("amount")),
        "currency": item.get("currency"),
        "status_id": _safe_int(item.get("statusId")),
        "partner_id": _safe_int(item.get("partnerId")),
        "comment": item.get("comment"),
        "act_date": item.get("actDate"),
        "nds": safe_decimal(item.get("nds")),
        "synced_at": now,
        "raw_json": item,
    }


def _map_pnl_category(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "type": item.get("type"),
        "pnl_type": item.get("pnlType"),
        "category_id": _safe_int(item.get("categoryId")),
        "comment": item.get("comment"),
        "synced_at": now,
        "raw_json": item,
    }


def _map_employee(item: dict, now: datetime) -> dict | None:
    fid = _safe_int(item.get("id"))
    if not fid:
        return None
    return {
        "id": fid,
        "name": item.get("name"),
        "date": item.get("date"),
        "currency": item.get("currency"),
        "regularfix": safe_decimal(item.get("regularfix")),
        "regularfee": safe_decimal(item.get("regularfee")),
        "regulartax": safe_decimal(item.get("regulartax")),
        "inn": item.get("inn"),
        "hired": item.get("hired"),
        "fired": item.get("fired"),
        "comment": item.get("comment"),
        "synced_at": now,
        "raw_json": item,
    }


# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ
# Public API (1 С„СѓРЅРєС†РёСЏ = 1 РєРЅРѕРїРєР° Р±РѕС‚Р°)
# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ


async def sync_ft_categories(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "category",
        fintablo_api.fetch_categories(),
        FTCategory.__table__,
        _map_category,
        ["id"],
        triggered_by,
    )


async def sync_ft_moneybags(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "moneybag",
        fintablo_api.fetch_moneybags(),
        FTMoneybag.__table__,
        _map_moneybag,
        ["id"],
        triggered_by,
    )


async def sync_ft_partners(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "partner",
        fintablo_api.fetch_partners(),
        FTPartner.__table__,
        _map_partner,
        ["id"],
        triggered_by,
    )


async def sync_ft_directions(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "direction",
        fintablo_api.fetch_directions(),
        FTDirection.__table__,
        _map_direction,
        ["id"],
        triggered_by,
    )


async def sync_ft_moneybag_groups(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "moneybag_group",
        fintablo_api.fetch_moneybag_groups(),
        FTMoneybagGroup.__table__,
        _map_moneybag_group,
        ["id"],
        triggered_by,
    )


async def sync_ft_goods(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "goods",
        fintablo_api.fetch_goods(),
        FTGoods.__table__,
        _map_goods,
        ["id"],
        triggered_by,
    )


async def sync_ft_obtainings(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obtaining",
        fintablo_api.fetch_obtainings(),
        FTObtaining.__table__,
        _map_obtaining,
        ["id"],
        triggered_by,
    )


async def sync_ft_jobs(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "job",
        fintablo_api.fetch_jobs(),
        FTJob.__table__,
        _map_job,
        ["id"],
        triggered_by,
    )


async def sync_ft_deals(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "deal",
        fintablo_api.fetch_deals(),
        FTDeal.__table__,
        _map_deal,
        ["id"],
        triggered_by,
    )


async def sync_ft_obligation_statuses(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obligation_status",
        fintablo_api.fetch_obligation_statuses(),
        FTObligationStatus.__table__,
        _map_obligation_status,
        ["id"],
        triggered_by,
    )


async def sync_ft_obligations(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "obligation",
        fintablo_api.fetch_obligations(),
        FTObligation.__table__,
        _map_obligation,
        ["id"],
        triggered_by,
    )


async def sync_ft_pnl_categories(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "pnl_category",
        fintablo_api.fetch_pnl_categories(),
        FTPnlCategory.__table__,
        _map_pnl_category,
        ["id"],
        triggered_by,
    )


async def sync_ft_employees(triggered_by: str | None = None) -> int:
    return await _run_ft_sync(
        "employee",
        fintablo_api.fetch_employees(),
        FTEmployee.__table__,
        _map_employee,
        ["id"],
        triggered_by,
    )


# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ
# РџР°СЂР°Р»Р»РµР»СЊРЅР°СЏ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ РІСЃРµРіРѕ FinTablo
# в•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђв•ђ

# РџРѕСЂСЏРґРѕРє: СЃРЅР°С‡Р°Р»Р° СЃРїСЂР°РІРѕС‡РЅРёРєРё (РѕС‚ РєРѕС‚РѕСЂС‹С… Р·Р°РІРёСЃСЏС‚ РґСЂСѓРіРёРµ), РїРѕС‚РѕРј Р±РёР·РЅРµСЃ-РґР°РЅРЅС‹Рµ
_FT_SYNC_TASKS = [
    ("рџ“Љ РЎС‚Р°С‚СЊРё Р”Р”РЎ", sync_ft_categories),
    ("рџ’° РЎС‡РµС‚Р°", sync_ft_moneybags),
    ("рџ¤ќ РљРѕРЅС‚СЂР°РіРµРЅС‚С‹", sync_ft_partners),
    ("рџЋЇ РќР°РїСЂР°РІР»РµРЅРёСЏ", sync_ft_directions),
    ("рџ“Ѓ Р“СЂСѓРїРїС‹ СЃС‡РµС‚РѕРІ", sync_ft_moneybag_groups),
    ("рџ“¦ РўРѕРІР°СЂС‹", sync_ft_goods),
    ("рџ›’ Р—Р°РєСѓРїРєРё", sync_ft_obtainings),
    ("рџ”§ РЈСЃР»СѓРіРё", sync_ft_jobs),
    ("рџ“ќ РЎРґРµР»РєРё", sync_ft_deals),
    ("рџЏ· РЎС‚Р°С‚СѓСЃС‹ РѕР±СЏР·.", sync_ft_obligation_statuses),
    ("рџ“‹ РћР±СЏР·Р°С‚РµР»СЊСЃС‚РІР°", sync_ft_obligations),
    ("рџ“€ РЎС‚Р°С‚СЊРё РџРёРЈ", sync_ft_pnl_categories),
    ("рџ‘¤ РЎРѕС‚СЂСѓРґРЅРёРєРё FT", sync_ft_employees),
]


async def sync_all_fintablo(
    triggered_by: str | None = None,
) -> list[tuple[str, int | str]]:
    """РџР°СЂР°Р»Р»РµР»СЊРЅР°СЏ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ РІСЃРµС… 13 СЃРїСЂР°РІРѕС‡РЅРёРєРѕРІ FinTablo."""
    t0 = time.monotonic()
    logger.info(
        "=== FinTablo: Р·Р°РїСѓСЃРєР°СЋ %d СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёР№ РїР°СЂР°Р»Р»РµР»СЊРЅРѕ ===",
        len(_FT_SYNC_TASKS),
    )

    coros = [func(triggered_by=triggered_by) for _, func in _FT_SYNC_TASKS]
    results = await asyncio.gather(*coros, return_exceptions=True)

    report: list[tuple[str, int | str]] = []
    for (label, _), result in zip(_FT_SYNC_TASKS, results):
        if isinstance(result, BaseException):
            logger.error("[FT] %s: РѕС€РёР±РєР° вЂ” %s", label, result)
            report.append((label, f"вќЊ {result}"))
        else:
            report.append((label, result))

    ok = sum(1 for _, r in report if isinstance(r, int))
    total_records = sum(r for _, r in report if isinstance(r, int))
    logger.info(
        "=== FinTablo: %d ok, %d err | %d Р·Р°РїРёСЃРµР№ | %.1f СЃРµРє ===",
        ok,
        len(report) - ok,
        total_records,
        time.monotonic() - t0,
    )
    return report
