"""
Use-case: обработка входящих вебхуков от iikoCloud.

Логика:
  1. iikoCloud шлёт POST на /iiko-webhook при закрытии заказа или изменении стоп-листа
  2. Для закрытых заказов (DeliveryOrderUpdate / TableOrderUpdate):
     - Синхронизируем остатки из iiko REST API
     - Сравниваем с последним отправленным снэпшотом
     - Если изменение ≥ STOCK_CHANGE_THRESHOLD_PCT (5%) → обновляем сообщения
  3. Для StopListUpdate:
     - Запускаем цикл стоп-листа: fetch → diff → всегда удаляем старое и шлём новое
"""

import hashlib
import json
import logging
import time
from typing import Any

from config import STOCK_CHANGE_THRESHOLD_PCT

logger = logging.getLogger(__name__)

LABEL = "iikoWebhook"

# Последний снэпшот остатков (для дельта-сравнения)
_last_snapshot_hash: str | None = None
_last_total_sum: float = 0.0


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


def _compute_total_sum(items: list[dict]) -> float:
    """Суммарное значение остатков (для процентного сравнения)."""
    return sum(it.get("total_amount", 0.0) for it in items)


def _should_update(new_hash: str, new_total: float) -> bool:
    """
    Определяем, нужно ли обновлять сообщения.
    Обновляем если:
      - Это первая проверка (нет предыдущего снэпшота)
      - Хеш изменился И изменение суммы >= threshold %
    """
    global _last_snapshot_hash, _last_total_sum

    if _last_snapshot_hash is None:
        # Первый запуск — всегда обновляем
        return True

    if new_hash == _last_snapshot_hash:
        # Ничего не изменилось
        return False

    # Хеш разный — проверяем порог по сумме
    if _last_total_sum == 0:
        return True  # было 0, стало что-то (или наоборот)

    change_pct = abs(new_total - _last_total_sum) / _last_total_sum * 100
    logger.info(
        "[%s] Дельта остатков: %.1f%% (порог: %.1f%%)",
        LABEL, change_pct, STOCK_CHANGE_THRESHOLD_PCT,
    )
    return change_pct >= STOCK_CHANGE_THRESHOLD_PCT


# ═══════════════════════════════════════════════════════
# Основной обработчик: инкремент + проверка
# ═══════════════════════════════════════════════════════

async def handle_webhook(body: list[dict], bot: Any) -> dict[str, Any]:
    """
    Обработать входящий вебхук от iikoCloud.

    - StopListUpdate: всегда удаляем старое сообщение и шлём новое (delete → send → pin).
    - Закрытые заказы: синхронизируем остатки, при расхождении ≥ 5% → обновляем.

    Returns:
        {"processed": int, "triggered_check": bool, "updated_messages": bool,
         "stoplist_updated": bool}
    """
    global _last_snapshot_hash, _last_total_sum

    result = {
        "processed": 0,
        "triggered_check": False,
        "updated_messages": False,
        "stoplist_updated": False,
    }

    # ── StopListUpdate → всегда обновляем ──
    if has_stoplist_update(body):
        logger.info("[%s] Обнаружен StopListUpdate, запускаю цикл стоп-листа...", LABEL)
        try:
            from use_cases.stoplist import run_stoplist_cycle
            from use_cases.pinned_stoplist_message import update_all_stoplist_messages
            from use_cases.cloud_org_mapping import get_all_cloud_org_ids

            # Определяем org_id из самого события (или из маппинга)
            event_org_ids = {
                e.get("organizationId") for e in body
                if e.get("eventType") == "StopListUpdate" and e.get("organizationId")
            }
            if not event_org_ids:
                event_org_ids = set(await get_all_cloud_org_ids())

            last_text: str | None = None
            for oid in event_org_ids:
                text, _ = await run_stoplist_cycle(org_id=oid)
                if text:
                    last_text = text

            # Всегда шлём, даже если нет изменений — пользователь видит актуальное
            if last_text:
                await update_all_stoplist_messages(bot, last_text)
                result["stoplist_updated"] = True
                logger.info("[%s] Стоп-лист обновлён и разослан (orgs: %s)", LABEL, event_org_ids)
            else:
                logger.warning("[%s] Не удалось получить текст стоп-листа", LABEL)
        except Exception:
            logger.exception("[%s] Ошибка обработки StopListUpdate", LABEL)

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
        new_total = _compute_total_sum(items)

        # 3. Дельта-сравнение (≥ 5% изменений)
        should = _should_update(new_hash, new_total)

        if should:
            # 4. Обновляем закреплённые сообщения (per-department для каждого юзера)
            from use_cases.pinned_stock_message import update_all_stock_alerts
            await update_all_stock_alerts(bot)

            _last_snapshot_hash = new_hash
            _last_total_sum = new_total
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
    global _last_snapshot_hash, _last_total_sum

    t0 = time.monotonic()
    logger.info("[%s] Принудительная проверка остатков...", LABEL)

    from use_cases.sync_stock_balances import sync_stock_balances
    await sync_stock_balances(triggered_by="manual_stock_check")

    from use_cases.check_min_stock import check_min_stock_levels
    result = await check_min_stock_levels()

    items = result.get("items", [])
    new_hash = _compute_snapshot_hash(items)
    new_total = _compute_total_sum(items)

    from use_cases.pinned_stock_message import update_all_stock_alerts
    await update_all_stock_alerts(bot)

    _last_snapshot_hash = new_hash
    _last_total_sum = new_total

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
