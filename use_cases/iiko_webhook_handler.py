"""
Use-case: обработка входящих вебхуков от iikoCloud.

Логика:
  1. iikoCloud шлёт POST на /iiko-webhook при закрытии заказа или изменении стоп-листа
  2. Для закрытых заказов (DeliveryOrderUpdate / TableOrderUpdate):
     - Синхронизируем остатки из iiko REST API
     - Сравниваем с последним отправленным снэпшотом
     - Если изменение ≥ STOCK_CHANGE_THRESHOLD_PCT (5%) → обновляем сообщения
  3. Для StopListUpdate (debounce = 60 сек):
     - Накапливаем org_id в буфер, сбрасываем таймер при каждом вебхуке
     - Через 60 сек тишины: fetch → diff → если есть изменения → send
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

LABEL = "iikoWebhook"

# Последний снэпшот остатков (для дельта-сравнения)
_last_snapshot_hash: str | None = None
_last_snapshot_items: dict[tuple[str, str], float] = {}  # {(product_id, dept_id): amount}
_last_update_time: float | None = None  # timestamp последней отправки (monotonic)

# ═══════════════════════════════════════════════════════
# Debounce для StopListUpdate
# ═══════════════════════════════════════════════════════
_STOPLIST_DEBOUNCE_SEC = 60  # ждём 60 сек тишины перед обработкой

_pending_stoplist_org_ids: set[str] = set()  # буфер org_id между вебхуками
_stoplist_timer: asyncio.TimerHandle | None = None  # текущий таймер
_stoplist_bot_ref: Any = None  # ссылка на bot для отложенного вызова


# ═══════════════════════════════════════════════════════
# Парсинг входящего вебхука
# ═══════════════════════════════════════════════════════

def parse_webhook_events(body: list[dict]) -> list[dict[str, Any]]:
    """
    Парсит массив событий из тела вебхука.
    Возвращает только закрытые заказы (Delivery + Table).

    Структура от iikoCloud:
      eventInfo           → OrderInfo / TableOrderInfo
      eventInfo.order     → Order / TableOrder (null если creationStatus != Success)
      eventInfo.order.status → "Closed" / "New" / "Bill" и т.д.
    """
    closed_events = []
    for event in body:
        event_type = event.get("eventType", "")

        # Логируем каждое событие для диагностики
        logger.info(
            "[%s] event: type=%s, org=%s, time=%s",
            LABEL, event_type,
            event.get("organizationId", "?"),
            event.get("eventTime", "?"),
        )

        if event_type not in ("DeliveryOrderUpdate", "TableOrderUpdate"):
            continue

        # eventInfo.order может быть null (creationStatus != "Success")
        event_info = event.get("eventInfo") or {}
        order = event_info.get("order") or {}
        status = order.get("status", "")

        logger.info(
            "[%s] → order.status=%s, creationStatus=%s",
            LABEL, status, event_info.get("creationStatus", "?"),
        )

        if status == "Closed":
            closed_events.append({
                "event_type": event_type,
                "order_id": order.get("id"),
                "organization_id": event.get("organizationId"),
                "event_time": event.get("eventTime"),
            })
    return closed_events


def has_stoplist_update(body: list[dict]) -> bool:
    """Проверить, есть ли среди событий StopListUpdate."""
    return any(e.get("eventType") == "StopListUpdate" for e in body)


# ═══════════════════════════════════════════════════════
# Debounce: накопление и отложенный flush
# ═══════════════════════════════════════════════════════

def _schedule_stoplist_flush(org_ids: set[str], bot: Any) -> None:
    """
    Добавить org_ids в буфер и (пере)запустить таймер на 60 сек.
    Если за 60 сек придёт ещё один StopListUpdate — таймер сбросится.
    """
    global _stoplist_timer, _stoplist_bot_ref

    _pending_stoplist_org_ids.update(org_ids)
    _stoplist_bot_ref = bot

    # Отменяем предыдущий таймер
    if _stoplist_timer is not None:
        _stoplist_timer.cancel()

    loop = asyncio.get_running_loop()
    _stoplist_timer = loop.call_later(
        _STOPLIST_DEBOUNCE_SEC,
        lambda: asyncio.ensure_future(_flush_stoplist()),
    )

    logger.info(
        "[%s] StopListUpdate debounce: +%d org(s), буфер=%d, flush через %d сек",
        LABEL, len(org_ids), len(_pending_stoplist_org_ids), _STOPLIST_DEBOUNCE_SEC,
    )


async def _flush_stoplist() -> None:
    """
    Вызывается через 60 сек тишины.
    Забираем накопленные org_ids, запускаем цикл стоп-листа, рассылаем.
    """
    global _stoplist_timer

    _stoplist_timer = None
    bot = _stoplist_bot_ref

    # Забираем и очищаем буфер
    org_ids = _pending_stoplist_org_ids.copy()
    _pending_stoplist_org_ids.clear()

    if not org_ids:
        return

    logger.info("[%s] Debounce flush: обрабатываю StopListUpdate для %d org(s): %s",
                LABEL, len(org_ids), org_ids)
    t0 = time.monotonic()

    try:
        from use_cases.stoplist import (
            fetch_stoplist_items, sync_and_diff, format_stoplist_message,
        )
        from use_cases.pinned_stoplist_message import update_all_stoplist_messages
        from use_cases.cloud_org_mapping import get_all_cloud_org_ids

        if not org_ids:
            org_ids = set(await get_all_cloud_org_ids())

        # Накапливаем изменения по всем организациям
        all_added: list[dict] = []
        all_removed: list[dict] = []
        all_existing: list[dict] = []

        for oid in org_ids:
            items = await fetch_stoplist_items(org_id=oid)
            added, removed, existing = await sync_and_diff(items, org_id=oid)
            all_added.extend(added)
            all_removed.extend(removed)
            all_existing.extend(existing)

        has_changes = bool(all_added or all_removed)

        if not has_changes:
            logger.info(
                "[%s] Стоп-лист без изменений (orgs: %s), пропускаем обновление (%.1f сек)",
                LABEL, org_ids, time.monotonic() - t0,
            )
            return

        # Формируем единое сообщение с объединённым дифом
        combined_text = format_stoplist_message(all_added, all_removed, all_existing)

        await update_all_stoplist_messages(bot, combined_text)
        logger.info(
            "[%s] Стоп-лист обновлён и разослан (orgs: %s, +%d -%d =%d) за %.1f сек",
            LABEL, org_ids, len(all_added), len(all_removed),
            len(all_existing), time.monotonic() - t0,
        )
    except Exception:
        logger.exception("[%s] Ошибка при flush стоп-листа", LABEL)


# ═══════════════════════════════════════════════════════
# Хеширование снэпшота остатков
# ═══════════════════════════════════════════════════════

def _compute_snapshot_hash(items: list[dict]) -> str:
    """
    SHA-256 хеш от данных остатков ниже минимума.
    Сортируем по product_name+department_name для стабильности.
    """
    normalized = sorted(
        [
            {
                "p": it["product_name"],
                "d": it["department_name"],
                "a": round(it["total_amount"], 2),
                "m": round(it["min_level"], 2),
            }
            for it in items
        ],
        key=lambda x: (x["d"], x["p"]),
    )
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _compute_items_dict(items: list[dict]) -> dict[tuple[str, str], float]:
    """
    Преобразует список items в словарь {(product_name, department_name): total_amount}.
    Используется для покомпонентного сравнения (каждая позиция отдельно).
    """
    return {
        (it["product_name"], it["department_name"]): it["total_amount"]
        for it in items
    }


def _should_update(new_hash: str, new_items_dict: dict[tuple[str, str], float]) -> bool:
    """
    Определяем, нужно ли обновлять сообщения (защита от спама).
    
    Обновляем если:
      1. Первая проверка (нет предыдущего снэпшота)
      2. ЛЮБАЯ позиция изменилась >= 3% от своего значения
      3. Появились новые позиции ниже минимума
      4. Позиции исчезли из списка (стали выше минимума)
      5. Прошло >= 30 мин с последней отправки И есть хоть какие-то изменения
    
    НЕ обновляем если:
      - Хеш не изменился (ничего не изменилось)
      - Все изменения < 3% И прошло < 30 мин (мелкие продажи, антиспам)
    """
    from config import STOCK_CHANGE_THRESHOLD_PCT, STOCK_UPDATE_INTERVAL_MIN

    if _last_snapshot_hash is None:
        # Первый запуск — всегда обновляем
        logger.info("[%s] Первая проверка — обновляем", LABEL)
        return True

    if new_hash == _last_snapshot_hash:
        # Ничего не изменилось
        logger.info("[%s] Хеш не изменился — пропускаем", LABEL)
        return False

    # Хеш изменился — проверяем КАЖДУЮ позицию
    old_keys = set(_last_snapshot_items.keys())
    new_keys = set(new_items_dict.keys())
    
    # Проверяем новые позиции (появились в списке)
    added = new_keys - old_keys
    if added:
        logger.info(
            "[%s] Появилось %d новых позиций ниже минимума: %s — обновляем",
            LABEL, len(added), list(added)[:3],
        )
        return True
    
    # Проверяем удалённые позиции (исчезли из списка — стали выше минимума)
    removed = old_keys - new_keys
    if removed:
        logger.info(
            "[%s] Исчезло %d позиций (стали выше минимума): %s — обновляем",
            LABEL, len(removed), list(removed)[:3],
        )
        return True
    
    # Проверяем изменения существующих позиций
    max_change_pct = 0.0
    max_change_item = None
    
    for key in new_keys & old_keys:
        old_val = _last_snapshot_items[key]
        new_val = new_items_dict[key]
        
        if old_val == 0:
            continue  # пропускаем деление на 0
        
        change_pct = abs(new_val - old_val) / old_val * 100
        
        if change_pct > max_change_pct:
            max_change_pct = change_pct
            max_change_item = (key, old_val, new_val)
        
        # Если хотя бы одна позиция изменилась >= порога — триггерим
        if change_pct >= STOCK_CHANGE_THRESHOLD_PCT:
            logger.info(
                "[%s] Позиция '%s / %s' изменилась %.1f%% >= %.1f%% (%.2f → %.2f) — обновляем",
                LABEL, key[0][:30], key[1][:20], change_pct, STOCK_CHANGE_THRESHOLD_PCT,
                old_val, new_val,
            )
            return True
    
    # Все изменения < порога — проверяем временное условие (30 мин)
    if _last_update_time is not None:
        elapsed_min = (time.monotonic() - _last_update_time) / 60
        if elapsed_min >= STOCK_UPDATE_INTERVAL_MIN:
            logger.info(
                "[%s] Прошло %.1f мин >= %d мин + есть изменения (макс %.1f%%) — обновляем",
                LABEL, elapsed_min, STOCK_UPDATE_INTERVAL_MIN, max_change_pct,
            )
            return True
        else:
            logger.info(
                "[%s] Все изменения < %.1f%% (макс %.1f%% у '%s') И прошло %.1f мин < %d мин — пропускаем (антиспам)",
                LABEL, STOCK_CHANGE_THRESHOLD_PCT, max_change_pct,
                max_change_item[0][0][:30] if max_change_item else "?",
                elapsed_min, STOCK_UPDATE_INTERVAL_MIN,
            )
            return False
    
    # Первое изменение после старта (нет _last_update_time) — обновляем
    logger.info(
        "[%s] Первое изменение после старта (макс %.1f%%) — обновляем",
        LABEL, max_change_pct,
    )
    return True


# ═══════════════════════════════════════════════════════
# Основной обработчик: инкремент + проверка
# ═══════════════════════════════════════════════════════

async def handle_webhook(body: list[dict], bot: Any) -> dict[str, Any]:
    """
    Обработать входящий вебхук от iikoCloud.

    - StopListUpdate: debounce 60 сек — накапливаем org_ids, после тишины flush.
    - Закрытые заказы: синхронизируем остатки, при расхождении ≥ 5% → обновляем.

    Returns:
        {"processed": int, "triggered_check": bool, "updated_messages": bool,
         "stoplist_updated": bool}
    """
    global _last_snapshot_hash, _last_snapshot_items, _last_update_time

    result = {
        "processed": 0,
        "triggered_check": False,
        "updated_messages": False,
        "stoplist_updated": False,
    }

    # ── StopListUpdate → debounce (60 сек) ──
    if has_stoplist_update(body):
        event_org_ids = {
            e.get("organizationId") for e in body
            if e.get("eventType") == "StopListUpdate" and e.get("organizationId")
        }
        _schedule_stoplist_flush(event_org_ids, bot)
        result["stoplist_updated"] = True  # запланировано

    # ── Закрытые заказы → проверка остатков на каждый вебхук ──
    closed = parse_webhook_events(body)
    if not closed:
        return result

    result["processed"] = len(closed)

    logger.info(
        "[%s] Получено %d закрытых заказов (типы: %s)",
        LABEL, len(closed),
        ", ".join(set(e["event_type"] for e in closed)),
    )

    # Проверяем остатки при каждом вебхуке (без счётчика)
    logger.info("[%s] Проверяю остатки...", LABEL)
    t0 = time.monotonic()

    try:
        # 1. Синхронизируем остатки (через iiko REST API)
        from use_cases.sync_stock_balances import sync_stock_balances
        await sync_stock_balances(triggered_by="iiko_webhook")

        # 2. Проверяем минимальные остатки
        from use_cases.check_min_stock import check_min_stock_levels
        stock_result = await check_min_stock_levels()

        items = stock_result.get("items", [])
        new_hash = _compute_snapshot_hash(items)
        new_items_dict = _compute_items_dict(items)

        # 3. Покомпонентное сравнение (каждая позиция отдельно)
        should = _should_update(new_hash, new_items_dict)

        if should:
            # 4. Обновляем закреплённые сообщения (per-department для каждого юзера)
            from use_cases.pinned_stock_message import update_all_stock_alerts
            await update_all_stock_alerts(bot)

            _last_snapshot_hash = new_hash
            _last_snapshot_items = new_items_dict
            _last_update_time = time.monotonic()  # запоминаем время отправки
            logger.info(
                "[%s] Остатки обновлены и разосланы за %.1f сек (items=%d)",
                LABEL, time.monotonic() - t0, len(items),
            )
            result["triggered_check"] = True
            result["updated_messages"] = True
        else:
            logger.info(
                "[%s] Остатки без значимых изменений, пропускаем обновление (%.1f сек)",
                LABEL, time.monotonic() - t0,
            )
            result["triggered_check"] = True

    except Exception:
        logger.exception("[%s] Ошибка при проверке остатков после вебхука", LABEL)
        result["triggered_check"] = True

    return result


# ═══════════════════════════════════════════════════════
# Принудительное обновление (для ручного вызова)
# ═══════════════════════════════════════════════════════

async def force_stock_check(bot: Any) -> dict[str, Any]:
    """
    Принудительная проверка остатков и обновление сообщений.
    Вызывается вручную из админки (игнорирует счётчик и порог).
    """
    global _last_snapshot_hash, _last_snapshot_items, _last_update_time

    t0 = time.monotonic()
    logger.info("[%s] Принудительная проверка остатков...", LABEL)

    from use_cases.sync_stock_balances import sync_stock_balances
    await sync_stock_balances(triggered_by="manual_stock_check")

    from use_cases.check_min_stock import check_min_stock_levels
    result = await check_min_stock_levels()

    items = result.get("items", [])
    new_hash = _compute_snapshot_hash(items)
    new_items_dict = _compute_items_dict(items)

    from use_cases.pinned_stock_message import update_all_stock_alerts
    await update_all_stock_alerts(bot)

    _last_snapshot_hash = new_hash
    _last_snapshot_items = new_items_dict
    _last_update_time = time.monotonic()  # запоминаем время отправки

    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Принудительная проверка завершена: %d позиций ниже min за %.1f сек",
        LABEL, len(items), elapsed,
    )
    return {
        "below_min_count": len(items),
        "total_products": result.get("total_products", 0),
        "elapsed": round(elapsed, 1),
    }
