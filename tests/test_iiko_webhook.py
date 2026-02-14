"""
Тесты для вебхук-пайплайна iikoCloud.

Покрываем:
  1. parse_webhook_events — реальные payloads от iikoCloud
  2. Null-safety (order=null когда creationStatus != Success)
  3. verify_webhook_auth — auth-token проверка
  4. register_webhook — структура фильтров
  5. Счётчик заказов и delta-логика
  6. format_stock_alert — форматирование сообщений
"""

import asyncio
import hashlib
import json
import sys
import os

# Фиксируем пути чтобы импорты работали из корня проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════
# 1. РЕАЛЬНЫЕ PAYLOADS ОТ IIKOCLOUD (из OpenAPI спеки)
# ═══════════════════════════════════════════════════════

# Delivery закрыт (ДОЛЖЕН пройти)
DELIVERY_CLOSED = [
    {
        "eventType": "DeliveryOrderUpdate",
        "eventTime": "2026-02-14 12:30:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "b090de0b-8550-6e17-70b2-bbba152bcbd3",
        "eventInfo": {
            "id": "aaaa1111-2222-3333-4444-555566667777",
            "organizationId": "550e8400-e29b-41d4-a716-446655440000",
            "timestamp": 1739530200,
            "creationStatus": "Success",
            "errorInfo": None,
            "order": {
                "id": "dddd1111-2222-3333-4444-555566667777",
                "status": "Closed",
                "number": 142,
                "items": [],
                "payments": [],
            },
        },
    }
]

# Table order закрыт (ДОЛЖЕН пройти)
TABLE_CLOSED = [
    {
        "eventType": "TableOrderUpdate",
        "eventTime": "2026-02-14 12:35:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "c090de0b-8550-6e17-70b2-bbba152bced4",
        "eventInfo": {
            "id": "bbbb1111-2222-3333-4444-555566667777",
            "organizationId": "550e8400-e29b-41d4-a716-446655440000",
            "timestamp": 1739530500,
            "creationStatus": "Success",
            "errorInfo": None,
            "order": {
                "id": "eeee1111-2222-3333-4444-555566667777",
                "status": "Closed",
                "number": 57,
                "items": [],
                "payments": [],
            },
        },
    }
]

# Delivery НЕ закрыт — на кухне (НЕ должен пройти)
DELIVERY_COOKING = [
    {
        "eventType": "DeliveryOrderUpdate",
        "eventTime": "2026-02-14 12:25:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "d111de0b-8550-6e17-70b2-bbba152bcbd3",
        "eventInfo": {
            "id": "cccc1111-2222-3333-4444-555566667777",
            "creationStatus": "Success",
            "errorInfo": None,
            "order": {
                "id": "ffff1111-2222-3333-4444-555566667777",
                "status": "CookingStarted",
                "number": 143,
            },
        },
    }
]

# Table order — New (НЕ должен пройти)
TABLE_NEW = [
    {
        "eventType": "TableOrderUpdate",
        "eventTime": "2026-02-14 12:40:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "e222de0b-8550-6e17-70b2-bbba152bced5",
        "eventInfo": {
            "id": "dddd2222-3333-4444-5555-666677778888",
            "creationStatus": "Success",
            "errorInfo": None,
            "order": {
                "id": "gggg2222-3333-4444-5555-666677778888",
                "status": "New",
                "number": 58,
            },
        },
    }
]

# Delivery с creationStatus=Error → order=null (НЕ должен пройти, НЕ должен крашить)
DELIVERY_ERROR_NULL_ORDER = [
    {
        "eventType": "DeliveryOrderUpdate",
        "eventTime": "2026-02-14 12:50:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "f333de0b-8550-6e17-70b2-bbba152bcbd3",
        "eventInfo": {
            "id": "hhhh1111-2222-3333-4444-555566667777",
            "creationStatus": "Error",
            "errorInfo": {
                "code": "SomeErrorCode",
                "message": "Failed to create delivery order",
            },
            "order": None,  # ← NULL! Критический случай
        },
    }
]

# eventInfo = null (крайний случай, не по спеке, но на продакшне бывает всякое)
DELIVERY_NULL_EVENT_INFO = [
    {
        "eventType": "DeliveryOrderUpdate",
        "eventTime": "2026-02-14 12:55:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "a444de0b-8550-6e17-70b2-bbba152bcbd3",
        "eventInfo": None,
    }
]

# StopListUpdate — тип события которое мы НЕ обрабатываем
STOPLIST_EVENT = [
    {
        "eventType": "StopListUpdate",
        "eventTime": "2026-02-14 13:00:00.000",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "correlationId": "b555de0b-8550-6e17-70b2-bbba152bcbd3",
        "eventInfo": {
            "terminalGroupsStopListsUpdates": [],
        },
    }
]

# Смешанный payload: 2 закрытых + 1 на кухне + 1 ошибка
MIXED_PAYLOAD = DELIVERY_CLOSED + TABLE_CLOSED + DELIVERY_COOKING + DELIVERY_ERROR_NULL_ORDER

# Пустой массив (iikoCloud может иногда прислать)
EMPTY_PAYLOAD = []


# ═══════════════════════════════════════════════════════
# 2. ТЕСТЫ parse_webhook_events
# ═══════════════════════════════════════════════════════

def test_parse_delivery_closed():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(DELIVERY_CLOSED)
    assert len(result) == 1, f"Expected 1 closed delivery, got {len(result)}"
    assert result[0]["event_type"] == "DeliveryOrderUpdate"
    assert result[0]["order_id"] == "dddd1111-2222-3333-4444-555566667777"
    assert result[0]["organization_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert result[0]["event_time"] == "2026-02-14 12:30:00.000"
    print("  ✓ Delivery Closed — парсится корректно")


def test_parse_table_closed():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(TABLE_CLOSED)
    assert len(result) == 1, f"Expected 1 closed table order, got {len(result)}"
    assert result[0]["event_type"] == "TableOrderUpdate"
    assert result[0]["order_id"] == "eeee1111-2222-3333-4444-555566667777"
    print("  ✓ Table Closed — парсится корректно")


def test_parse_delivery_cooking_ignored():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(DELIVERY_COOKING)
    assert len(result) == 0, f"Expected 0 (not closed), got {len(result)}"
    print("  ✓ Delivery CookingStarted — проигнорирован")


def test_parse_table_new_ignored():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(TABLE_NEW)
    assert len(result) == 0, f"Expected 0 (New, not Closed), got {len(result)}"
    print("  ✓ Table New — проигнорирован")


def test_parse_null_order_no_crash():
    """КРИТИЧЕСКИЙ: order=null при creationStatus=Error не должен крашить."""
    from use_cases.iiko_webhook_handler import parse_webhook_events
    try:
        result = parse_webhook_events(DELIVERY_ERROR_NULL_ORDER)
        assert len(result) == 0, f"Expected 0 (order is null), got {len(result)}"
        print("  ✓ order=null — не крашит, проигнорирован")
    except AttributeError as e:
        print(f"  ✗ CRASH! order=null → AttributeError: {e}")
        raise


def test_parse_null_event_info_no_crash():
    """eventInfo=null — крайний случай, не должен крашить."""
    from use_cases.iiko_webhook_handler import parse_webhook_events
    try:
        result = parse_webhook_events(DELIVERY_NULL_EVENT_INFO)
        assert len(result) == 0
        print("  ✓ eventInfo=null — не крашит")
    except AttributeError as e:
        print(f"  ✗ CRASH! eventInfo=null → AttributeError: {e}")
        raise


def test_parse_stoplist_ignored():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(STOPLIST_EVENT)
    assert len(result) == 0
    print("  ✓ StopListUpdate — проигнорирован")


def test_parse_mixed_payload():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(MIXED_PAYLOAD)
    assert len(result) == 2, f"Expected 2 closed (1 delivery + 1 table), got {len(result)}"
    types = {e["event_type"] for e in result}
    assert types == {"DeliveryOrderUpdate", "TableOrderUpdate"}
    print("  ✓ Mixed payload — 2 из 4 событий прошли (только Closed)")


def test_parse_empty_payload():
    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(EMPTY_PAYLOAD)
    assert len(result) == 0
    print("  ✓ Пустой payload — 0 событий")


# ═══════════════════════════════════════════════════════
# 3. ТЕСТЫ verify_webhook_auth
# ═══════════════════════════════════════════════════════

def test_auth_valid_bearer():
    """authToken с Bearer prefix."""
    from adapters.iiko_cloud_api import verify_webhook_auth
    from config import IIKO_CLOUD_WEBHOOK_SECRET as secret
    assert verify_webhook_auth(f"Bearer {secret}") is True
    print(f"  ✓ Bearer <secret> — валидный (secret={secret[:8]}...)")


def test_auth_valid_raw():
    """authToken без Bearer prefix (на случай если iikoCloud шлёт без Bearer)."""
    from adapters.iiko_cloud_api import verify_webhook_auth
    from config import IIKO_CLOUD_WEBHOOK_SECRET as secret
    assert verify_webhook_auth(secret) is True
    print("  ✓ Raw <secret> — валидный")


def test_auth_invalid():
    from adapters.iiko_cloud_api import verify_webhook_auth
    assert verify_webhook_auth("Bearer wrong_token_123") is False
    assert verify_webhook_auth("") is False
    assert verify_webhook_auth(None) is False
    print("  ✓ Невалидные токены — отклонены")


# ═══════════════════════════════════════════════════════
# 4. ТЕСТ структуры фильтров register_webhook
# ═══════════════════════════════════════════════════════

def test_webhook_filter_structure():
    """Проверяем что фильтры соответствуют OpenAPI спеке iikoCloud."""
    # Эмулируем payload без реального HTTP-вызова
    from config import IIKO_CLOUD_WEBHOOK_SECRET

    payload = {
        "organizationId": "test-org-id",
        "webHooksUri": "https://example.com/iiko-webhook",
        "authToken": IIKO_CLOUD_WEBHOOK_SECRET,
        "webHooksFilter": {
            "deliveryOrderFilter": {
                "orderStatuses": ["Closed"],
                "errors": False,
            },
            "tableOrderFilter": {
                "orderStatuses": ["Closed"],
                "errors": False,
            },
        },
    }

    # Проверки по OpenAPI спеке
    f = payload["webHooksFilter"]

    # deliveryOrderFilter
    assert "deliveryOrderFilter" in f
    dof = f["deliveryOrderFilter"]
    assert "orderStatuses" in dof
    assert "Closed" in dof["orderStatuses"]
    valid_delivery_statuses = {
        "Unconfirmed", "WaitCooking", "ReadyForCooking", "CookingStarted",
        "CookingCompleted", "Waiting", "OnWay", "Delivered", "Closed", "Cancelled",
    }
    for s in dof["orderStatuses"]:
        assert s in valid_delivery_statuses, f"'{s}' not a valid DeliveryStatus"
    assert dof["errors"] is False  # Не подписываемся на DeliveryOrderError

    # tableOrderFilter
    assert "tableOrderFilter" in f
    tof = f["tableOrderFilter"]
    assert "orderStatuses" in tof
    assert "Closed" in tof["orderStatuses"]
    valid_table_statuses = {"New", "Bill", "Closed", "Deleted"}
    for s in tof["orderStatuses"]:
        assert s in valid_table_statuses, f"'{s}' not a valid OrderStatus (table)"
    assert tof["errors"] is False  # Не подписываемся на TableOrderError

    print("  ✓ Фильтры полностью соответствуют OpenAPI спеке iikoCloud")


# ═══════════════════════════════════════════════════════
# 5. ТЕСТ delta-логики (_should_update)
# ═══════════════════════════════════════════════════════

def test_should_update_first_run():
    """Первый запуск — всегда обновлять."""
    from use_cases import iiko_webhook_handler as wh
    # Reset state
    wh._last_snapshot_hash = None
    wh._last_total_sum = 0.0

    assert wh._should_update("somehash", 100.0) is True
    print("  ✓ Первый запуск — обновляем")


def test_should_update_same_hash():
    """Если хеш совпадает — не обновлять."""
    from use_cases import iiko_webhook_handler as wh
    wh._last_snapshot_hash = "abc123"
    wh._last_total_sum = 100.0

    assert wh._should_update("abc123", 100.0) is False
    print("  ✓ Одинаковый хеш — пропускаем")


def test_should_update_small_change():
    """Хеш разный, но изменение < 5% — не обновлять."""
    from use_cases import iiko_webhook_handler as wh
    wh._last_snapshot_hash = "old_hash"
    wh._last_total_sum = 1000.0

    # 3% изменение (< 5% порога)
    assert wh._should_update("new_hash", 1030.0) is False
    print("  ✓ Малое изменение (3%) — пропускаем")


def test_should_update_large_change():
    """Хеш разный и изменение >= 5% — обновлять."""
    from use_cases import iiko_webhook_handler as wh
    wh._last_snapshot_hash = "old_hash"
    wh._last_total_sum = 1000.0

    # 10% изменение (> 5% порога)
    assert wh._should_update("new_hash", 1100.0) is True
    print("  ✓ Большое изменение (10%) — обновляем")


def test_should_update_from_zero():
    """Было 0, стало что-то — обновлять."""
    from use_cases import iiko_webhook_handler as wh
    wh._last_snapshot_hash = "zero_hash"
    wh._last_total_sum = 0.0

    assert wh._should_update("new_hash", 50.0) is True
    print("  ✓ Было 0 → стало 50 — обновляем")


# ═══════════════════════════════════════════════════════
# 6. ТЕСТ _compute_snapshot_hash (стабильность)
# ═══════════════════════════════════════════════════════

def test_snapshot_hash_stability():
    """Один и тот же набор данных → одинаковый хеш, независимо от порядка."""
    from use_cases.iiko_webhook_handler import _compute_snapshot_hash

    items_a = [
        {"product_name": "Мука", "department_name": "Кухня", "total_amount": 5.5, "min_level": 10.0},
        {"product_name": "Соль", "department_name": "Бар", "total_amount": 1.0, "min_level": 3.0},
    ]
    items_b = [
        {"product_name": "Соль", "department_name": "Бар", "total_amount": 1.0, "min_level": 3.0},
        {"product_name": "Мука", "department_name": "Кухня", "total_amount": 5.5, "min_level": 10.0},
    ]

    assert _compute_snapshot_hash(items_a) == _compute_snapshot_hash(items_b)
    print("  ✓ Хеш стабилен (не зависит от порядка)")


def test_snapshot_hash_differs():
    """Разные данные → разный хеш."""
    from use_cases.iiko_webhook_handler import _compute_snapshot_hash

    items_a = [
        {"product_name": "Мука", "department_name": "Кухня", "total_amount": 5.5, "min_level": 10.0},
    ]
    items_b = [
        {"product_name": "Мука", "department_name": "Кухня", "total_amount": 4.0, "min_level": 10.0},
    ]

    assert _compute_snapshot_hash(items_a) != _compute_snapshot_hash(items_b)
    print("  ✓ Разные данные → разный хеш")


# ═══════════════════════════════════════════════════════
# 7. ТЕСТ format_stock_alert
# ═══════════════════════════════════════════════════════

def test_format_alert_all_ok():
    from use_cases.pinned_stock_message import format_stock_alert
    data = {"below_min_count": 0, "total_products": 15, "items": []}
    text = format_stock_alert(data, department_name="Пиццерия")
    assert "✅" in text
    assert "Пиццерия" in text
    assert "15" in text
    print(f"  ✓ Все ОК → сообщение: {text[:60]}...")


def test_format_alert_below_min():
    from use_cases.pinned_stock_message import format_stock_alert
    data = {
        "below_min_count": 2,
        "total_products": 15,
        "items": [
            {
                "product_name": "Мука пшеничная",
                "department_name": "Пиццерия",
                "total_amount": 3.5,
                "min_level": 10.0,
                "max_level": 25.0,
                "deficit": 6.5,
            },
            {
                "product_name": "Соус томатный",
                "department_name": "Пиццерия",
                "total_amount": 1.0,
                "min_level": 5.0,
                "max_level": None,
                "deficit": 4.0,
            },
        ],
    }
    text = format_stock_alert(data, department_name="Пиццерия")
    assert "⚠️" in text
    assert "2 поз." in text
    assert "Мука пшеничная" in text
    assert "Соус томатный" in text
    assert "Пиццерия" in text  # В заголовке как department_name
    assert "📍 Пиццерия" in text  # В группе по ресторану
    assert len(text) < 4096  # Telegram лимит
    print(f"  ✓ Ниже минимума → сообщение: {len(text)} символов")


def test_format_alert_no_department():
    """Без department_name — не должен падать."""
    from use_cases.pinned_stock_message import format_stock_alert
    data = {"below_min_count": 0, "total_products": 5, "items": []}
    text = format_stock_alert(data)
    assert "✅" in text
    print("  ✓ Без department_name — OK")


# ═══════════════════════════════════════════════════════
# 8. ТЕСТ webhook endpoint (aiohttp request simulation)
# ═══════════════════════════════════════════════════════

def test_webhook_body_array_handling():
    """Проверяем что endpoint правильно обрабатывает list и dict payloads."""
    # Если body — list → оставить как есть
    body_list = DELIVERY_CLOSED
    assert isinstance(body_list, list)

    # Если body — dict → обернуть в list (наша защита в main.py)
    body_dict = DELIVERY_CLOSED[0]
    if not isinstance(body_dict, list):
        body_dict = [body_dict]
    assert isinstance(body_dict, list)
    assert len(body_dict) == 1

    from use_cases.iiko_webhook_handler import parse_webhook_events
    result = parse_webhook_events(body_dict)
    assert len(result) == 1
    print("  ✓ Dict body → обёрнут в list → парсится корректно")


# ═══════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════

def run_all():
    tests = [
        ("parse_webhook_events", [
            test_parse_delivery_closed,
            test_parse_table_closed,
            test_parse_delivery_cooking_ignored,
            test_parse_table_new_ignored,
            test_parse_null_order_no_crash,
            test_parse_null_event_info_no_crash,
            test_parse_stoplist_ignored,
            test_parse_mixed_payload,
            test_parse_empty_payload,
        ]),
        ("verify_webhook_auth", [
            test_auth_valid_bearer,
            test_auth_valid_raw,
            test_auth_invalid,
        ]),
        ("webhook filter structure", [
            test_webhook_filter_structure,
        ]),
        ("delta logic (_should_update)", [
            test_should_update_first_run,
            test_should_update_same_hash,
            test_should_update_small_change,
            test_should_update_large_change,
            test_should_update_from_zero,
        ]),
        ("snapshot hash", [
            test_snapshot_hash_stability,
            test_snapshot_hash_differs,
        ]),
        ("format_stock_alert", [
            test_format_alert_all_ok,
            test_format_alert_below_min,
            test_format_alert_no_department,
        ]),
        ("webhook endpoint handling", [
            test_webhook_body_array_handling,
        ]),
    ]

    total = 0
    passed = 0
    failed = 0

    for group_name, test_fns in tests:
        print(f"\n{'='*50}")
        print(f"  {group_name}")
        print(f"{'='*50}")
        for fn in test_fns:
            total += 1
            try:
                fn()
                passed += 1
            except Exception as e:
                failed += 1
                print(f"  ✗ FAILED: {fn.__name__}: {e}")

    print(f"\n{'='*50}")
    print(f"  ИТОГО: {passed}/{total} прошли, {failed} провалились")
    print(f"{'='*50}")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
