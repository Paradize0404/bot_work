"""
Use-case: ежедневное авто-перемещение отрицательных остатков расходных материалов.

Запускается в 23:00 по Калининграду (APScheduler в scheduler.py).

Принцип работы:
  1. Загрузка всех активных складов из БД.
  2. Группировка по ресторану: паттерн имени склада = "TYPE (РЕСТОРАН)".
  3. Для каждого ресторана:
       - SOURCE = склад с именем "{SOURCE_PREFIX} ({РЕСТОРАН})"  — откуда перемещаем
       - TARGETS = склады с именем "{TARGET_PREFIX} ({РЕСТОРАН})" — куда перемещаем
  4. OLAP-запрос v1: остатки на сегодня по всем складам (один запрос).
  5. Фильтр: target stores + Product.TopParent == PRODUCT_GROUP + amount < 0.
  6. Для каждой связки source→target: POST /v2/documents/internalTransfer.
  7. Запись в SyncLog.

Конфигурация (.env, все с дефолтами):
  NEGATIVE_TRANSFER_SOURCE_PREFIX    — префикс склада-источника  (default: "Хоз. товары")
  NEGATIVE_TRANSFER_TARGET_PREFIXES  — CSV целевых префиксов     (default: "Бар,Кухня")
  NEGATIVE_TRANSFER_PRODUCT_GROUP    — фильтр TopParent          (default: "Расходные материалы")

Авто-масштабирование: при добавлении нового ресторана (нового склада "Хоз. товары (НОВЫЙ)"),
переносы для него начнутся автоматически без изменений кода.
"""

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select

from adapters import iiko_api
from db.engine import async_session_factory
from db.models import Product, Store, SyncLog
from use_cases._helpers import now_kgd, safe_float

logger = logging.getLogger(__name__)

LABEL = "NegativeTransfer"

# asyncio.Lock — защита от двойного запуска (например, ручной и по расписанию)
_SYNC_LOCK = asyncio.Lock()


# ═══════════════════════════════════════════════════════
# Конфигурация (читается из config.py при каждом вызове,
# чтобы подхватывать обновлённые env-переменные без рестарта)
# ═══════════════════════════════════════════════════════

def _cfg_source_prefix() -> str:
    import config
    return getattr(config, "NEGATIVE_TRANSFER_SOURCE_PREFIX", "Хоз. товары")


def _cfg_target_prefixes() -> list[str]:
    import config
    raw = getattr(config, "NEGATIVE_TRANSFER_TARGET_PREFIXES", "Бар,Кухня")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _cfg_product_group() -> str:
    import config
    return getattr(config, "NEGATIVE_TRANSFER_PRODUCT_GROUP", "Расходные материалы")


# ═══════════════════════════════════════════════════════
# Определение ресторанов по паттерну имён складов
# ═══════════════════════════════════════════════════════

# Паттерн: "TYPE (РЕСТОРАН)"  →  группа 1 = "TYPE", группа 2 = "РЕСТОРАН"
_STORE_PATTERN = re.compile(r"^(.+?)\s*\((.+?)\)$")


def _parse_store_name(name: str) -> tuple[str, str] | None:
    """
    Разбирает имя склада вида 'TYPE (РЕСТОРАН)'.
    Возвращает (type_prefix, restaurant) или None если не совпадает.
    """
    m = _STORE_PATTERN.match((name or "").strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _build_restaurant_map(
    stores: list[tuple[str, str]],   # [(id_str, name), ...]
    source_prefix: str,
    target_prefixes: list[str],
) -> dict[str, dict]:
    """
    Строит карту ресторанов:
      restaurant → {
          "source":  (store_id, store_name),
          "targets": [(store_id, store_name), ...],
      }

    Включает только рестораны, у которых найдены И source, И хотя бы один target.
    Рестораны без пары молча пропускаются (логируются на DEBUG).
    """
    by_restaurant: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for store_id, store_name in stores:
        parsed = _parse_store_name(store_name)
        if parsed:
            _, restaurant = parsed
            by_restaurant[restaurant].append((store_id, store_name))

    result: dict[str, dict] = {}
    for restaurant, store_list in by_restaurant.items():
        source: tuple[str, str] | None = None
        targets: list[tuple[str, str]] = []

        for store_id, store_name in store_list:
            parsed = _parse_store_name(store_name)
            if not parsed:
                continue
            prefix, _ = parsed
            if prefix == source_prefix:
                source = (store_id, store_name)
            elif prefix in target_prefixes:
                targets.append((store_id, store_name))

        if source and targets:
            result[restaurant] = {"source": source, "targets": targets}
        elif source:
            logger.debug(
                "[%s] Ресторан '%s': источник '%s' есть, целевых складов не найдено — пропуск",
                LABEL, restaurant, source[1],
            )
        elif targets:
            logger.debug(
                "[%s] Ресторан '%s': есть цели %s, источника '%s' нет — пропуск",
                LABEL, restaurant, [t[1] for t in targets], source_prefix,
            )

    return result


# ═══════════════════════════════════════════════════════
# Загрузка данных из БД
# ═══════════════════════════════════════════════════════

async def _load_stores() -> list[tuple[str, str]]:
    """Загрузить все активные склады из БД. Возвращает [(id_str, name), ...]."""
    async with async_session_factory() as session:
        rows = await session.execute(
            select(Store.id, Store.name).where(Store.deleted == False)  # noqa: E712
        )
        return [(str(r.id), r.name or "") for r in rows.all()]


async def _load_products_by_name(names: set[str]) -> dict[str, dict]:
    """
    Загрузить продукты по именам из БД.
    Возвращает: {product_name: {"id": str, "main_unit": str | None}}
    """
    if not names:
        return {}
    async with async_session_factory() as session:
        rows = await session.execute(
            select(Product.id, Product.name, Product.main_unit).where(
                Product.name.in_(list(names)),
                Product.deleted == False,  # noqa: E712
            )
        )
        result: dict[str, dict] = {}
        for row in rows.all():
            if row.name:
                result[row.name.strip()] = {
                    "id": str(row.id),
                    "main_unit": str(row.main_unit) if row.main_unit else None,
                }
        return result


# ═══════════════════════════════════════════════════════
# Получение остатков через OLAP
# ═══════════════════════════════════════════════════════

async def _fetch_balances_today() -> list[dict[str, Any]]:
    """Получить остатки по всем складам на сегодня через OLAP v1 GET."""
    today = datetime.now().strftime("%d.%m.%Y")
    return await iiko_api.fetch_olap_transactions_v1(today, today)


# ═══════════════════════════════════════════════════════
# Фильтрация отрицательных позиций
# ═══════════════════════════════════════════════════════

def _collect_negative_items(
    rows: list[dict],
    target_store_names: set[str],
    product_group_filter: str,
) -> dict[str, list[dict]]:
    """
    Из OLAP-строк выбирает только:
      - склады из target_store_names
      - Product.TopParent == product_group_filter
      - FinalBalance.Amount < 0

    Возвращает: {store_name: [{"product_name": str, "amount": float (>0)}, ...]}
    """
    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        store_name = (row.get("Account.Name") or "").strip()
        if store_name not in target_store_names:
            continue
        top_parent = (row.get("Product.TopParent") or "").strip()
        if top_parent != product_group_filter:
            continue
        amount = safe_float(row.get("FinalBalance.Amount"))
        if amount is None or amount >= 0:
            continue
        product_name = (row.get("Product.Name") or "").strip()
        if not product_name:
            continue
        result[store_name].append({
            "product_name": product_name,
            "amount": abs(amount),
        })
    return dict(result)


# ═══════════════════════════════════════════════════════
# Основная логика
# ═══════════════════════════════════════════════════════

async def run_negative_transfer_all_restaurants(
    triggered_by: str = "scheduler",
) -> dict[str, Any]:
    """
    Авто-перемещение отрицательных остатков расходных материалов по всем ресторанам.

    Возвращает словарь с итогами:
      {
        "status": "ok" | "no_restaurants" | "nothing_to_transfer",
        "restaurants": {
          "РЕСТОРАН": {
            "transfers": [{"from": ..., "to": ..., "count": int, "error"?: str}],
            "skipped_products": [str, ...]
          }
        }
      }
    """
    started = now_kgd()
    t0 = time.monotonic()
    logger.info(
        "[%s] ===== Старт авто-перемещения расх.мат. (triggered_by=%s) =====",
        LABEL, triggered_by,
    )

    source_prefix   = _cfg_source_prefix()
    target_prefixes = _cfg_target_prefixes()
    product_group   = _cfg_product_group()

    logger.info(
        "[%s] Конфиг: source_prefix=%r, target_prefixes=%r, product_group=%r",
        LABEL, source_prefix, target_prefixes, product_group,
    )

    result_data: dict[str, Any] = {}

    try:
        # ── 1. Загрузить склады, построить карту ресторанов ──
        stores = await _load_stores()
        restaurant_map = _build_restaurant_map(stores, source_prefix, target_prefixes)

        if not restaurant_map:
            logger.warning(
                "[%s] Не найдено ни одного ресторана с парой source+target. "
                "Проверьте NEGATIVE_TRANSFER_SOURCE_PREFIX и NEGATIVE_TRANSFER_TARGET_PREFIXES. "
                "Текущие склады в БД: %s",
                LABEL,
                [s[1] for s in stores],
            )
            _write_sync_log(started, "success", 0, triggered_by)
            return {"status": "no_restaurants", "restaurants": {}}

        logger.info(
            "[%s] Заведений для перемещения: %d — %s",
            LABEL, len(restaurant_map), ", ".join(sorted(restaurant_map.keys())),
        )

        # ── 2. Получить остатки через OLAP (один запрос для всех) ──
        logger.info("[%s] Запрашиваю OLAP остатки на сегодня...", LABEL)
        t_olap = time.monotonic()
        all_rows = await _fetch_balances_today()
        logger.info(
            "[%s] OLAP: %d строк за %.1f сек", LABEL, len(all_rows), time.monotonic() - t_olap,
        )

        # ── 3. Собрать целевые склады всех ресторанов ──
        all_target_names: set[str] = set()
        for rest_data in restaurant_map.values():
            for _, tname in rest_data["targets"]:
                all_target_names.add(tname)

        # ── 4. Фильтровать отрицательные позиции по расх.материалам ──
        negative_by_store = _collect_negative_items(all_rows, all_target_names, product_group)

        if not negative_by_store:
            logger.info(
                "[%s] Нет отрицательных остатков группы '%s' ни на одном целевом складе — нечего перемещать",
                LABEL, product_group,
            )
            _write_sync_log(started, "success", 0, triggered_by)
            return {"status": "nothing_to_transfer", "restaurants": {}}

        total_negative = sum(len(v) for v in negative_by_store.values())
        logger.info(
            "[%s] Отрицательных позиций: %d по %d складам",
            LABEL, total_negative, len(negative_by_store),
        )

        # ── 5. Загрузить product UUID + main_unit из БД ──
        all_product_names: set[str] = {
            item["product_name"]
            for items in negative_by_store.values()
            for item in items
        }
        products_db = await _load_products_by_name(all_product_names)
        missing_in_db = all_product_names - set(products_db.keys())
        if missing_in_db:
            logger.warning(
                "[%s] Не найдены в БД %d товаров (пропускаем): %s",
                LABEL, len(missing_in_db), ", ".join(sorted(missing_in_db)),
            )

        # ── 6. Для каждого ресторана: отправить перемещения ──
        total_transfers = 0
        for restaurant, rest_data in sorted(restaurant_map.items()):
            source_id, source_name = rest_data["source"]
            rest_result: dict[str, Any] = {"transfers": [], "skipped_products": []}
            result_data[restaurant] = rest_result

            for target_id, target_name in rest_data["targets"]:
                negative_items = negative_by_store.get(target_name, [])
                if not negative_items:
                    logger.debug(
                        "[%s] %s: на складе '%s' отрицательных позиций нет — пропуск",
                        LABEL, restaurant, target_name,
                    )
                    continue

                # Резолвим UUID продуктов
                transfer_items: list[dict] = []
                for item in negative_items:
                    pname = item["product_name"]
                    pdata = products_db.get(pname)
                    if not pdata:
                        rest_result["skipped_products"].append(pname)
                        continue
                    if not pdata.get("main_unit"):
                        logger.warning(
                            "[%s] '%s' — нет main_unit в БД, пропускаем",
                            LABEL, pname,
                        )
                        rest_result["skipped_products"].append(pname)
                        continue
                    transfer_items.append({
                        "productId":     pdata["id"],
                        "amount":        round(item["amount"], 6),
                        "measureUnitId": pdata["main_unit"],
                    })

                if not transfer_items:
                    logger.info(
                        "[%s] %s → %s: все позиции пропущены (нет в БД / нет unit)",
                        LABEL, source_name, target_name,
                    )
                    continue

                comment = (
                    f"Авто-перемещение расх.мат. ({restaurant}) "
                    f"{now_kgd().strftime('%d.%m.%Y')}"
                )
                logger.info(
                    "[%s] %s → %s: %d позиций: %s",
                    LABEL, source_name, target_name, len(transfer_items),
                    ", ".join(
                        f"{it.get('productName', it['productId'][:8])}×{it['amount']}"
                        for it in transfer_items[:5]
                    ),
                )

                try:
                    doc = {
                        "dateIncoming": now_kgd().strftime("%Y-%m-%dT%H:%M:%S"),
                        "status":       "PROCESSED",
                        "comment":      comment,
                        "storeFromId":  source_id,
                        "storeToId":    target_id,
                        "items":        transfer_items,
                    }
                    await iiko_api.send_internal_transfer(doc)
                    rest_result["transfers"].append({
                        "from":  source_name,
                        "to":    target_name,
                        "count": len(transfer_items),
                    })
                    total_transfers += 1
                    logger.info(
                        "[%s] ✅ Перемещение отправлено: %s → %s (%d поз.)",
                        LABEL, source_name, target_name, len(transfer_items),
                    )

                except Exception as exc:
                    logger.exception(
                        "[%s] ❌ Ошибка перемещения %s → %s: %s",
                        LABEL, source_name, target_name, exc,
                    )
                    rest_result["transfers"].append({
                        "from":  source_name,
                        "to":    target_name,
                        "count": len(transfer_items),
                        "error": str(exc)[:300],
                    })

        # ── 7. SyncLog ──
        elapsed = time.monotonic() - t0
        logger.info(
            "[%s] ===== Завершено: %d перемещений за %.1f сек =====",
            LABEL, total_transfers, elapsed,
        )
        _write_sync_log(started, "success", total_transfers, triggered_by)

        return {"status": "ok", "restaurants": result_data}

    except Exception as exc:
        logger.exception("[%s] КРИТИЧЕСКАЯ ОШИБКА: %s", LABEL, exc)
        _write_sync_log(started, "error", 0, triggered_by, error=str(exc)[:2000])
        raise


def _write_sync_log(
    started: Any,
    status: str,
    records: int,
    triggered_by: str,
    error: str | None = None,
) -> None:
    """Записать запись в iiko_sync_log без await (создаём task)."""
    asyncio.create_task(_async_write_sync_log(started, status, records, triggered_by, error))


async def _async_write_sync_log(
    started: Any,
    status: str,
    records: int,
    triggered_by: str,
    error: str | None,
) -> None:
    try:
        async with async_session_factory() as session:
            session.add(SyncLog(
                entity_type=LABEL,
                started_at=started,
                finished_at=now_kgd(),
                status=status,
                records_synced=records,
                triggered_by=triggered_by,
                error_message=error,
            ))
            await session.commit()
    except Exception:
        logger.exception("[%s] Не удалось записать SyncLog", LABEL)


# ═══════════════════════════════════════════════════════
# Публичный API — с Lock
# ═══════════════════════════════════════════════════════

async def run_negative_transfer_once(
    triggered_by: str = "scheduler",
) -> dict[str, Any]:
    """
    Запуск авто-перемещения с защитой от дублирования (asyncio.Lock).
    Если уже запущено — немедленно возвращает {"status": "locked"}.
    """
    if _SYNC_LOCK.locked():
        logger.warning("[%s] Уже запущено, пропускаю (triggered_by=%s)", LABEL, triggered_by)
        return {"status": "locked"}

    async with _SYNC_LOCK:
        return await run_negative_transfer_all_restaurants(triggered_by=triggered_by)
