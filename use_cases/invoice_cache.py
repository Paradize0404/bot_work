"""
In-memory TTL-кеш для расходных накладных (outgoing invoice).

Кеширует данные, которые редко меняются:
  - Контрагенты (suppliers)
  - Счёт реализации (account)
  - Номенклатура по дереву gsheet_export_group (GOODS + DISH)

Стратегия:
  - preload() — прогревает кеш при клике на «Расходная накладная» (фоново)
  - TTL 10 мин (склады, контрагенты, счёт), 10 мин (номенклатура)
  - invalidate() — сброс при отмене/завершении

~300 КБ RAM на ~2000 товаров + ~50 контрагентов. Redis не нужен.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# TTL в секундах
CACHE_TTL = 600        # 10 минут
PRODUCTS_TTL = 600     # 10 минут (номенклатура по дереву)

# Хранилище: {key: (data, timestamp)}
_store: dict[str, tuple[Any, float]] = {}


def _get(key: str, ttl: float = CACHE_TTL) -> Any | None:
    """Получить значение из кеша, если не протухло."""
    entry = _store.get(key)
    if entry is None:
        return None
    data, ts = entry
    if time.monotonic() - ts > ttl:
        del _store[key]
        return None
    return data


def _set(key: str, data: Any) -> None:
    """Положить значение в кеш."""
    _store[key] = (data, time.monotonic())


# ── Контрагенты (suppliers) ──

def get_suppliers() -> list[dict] | None:
    """Все поставщики из кеша (или None если протухли)."""
    return _get("inv:suppliers")


def set_suppliers(suppliers: list[dict]) -> None:
    _set("inv:suppliers", suppliers)


# ── Счёт реализации ──

def get_revenue_account() -> dict | None:
    """Счёт реализации из кеша (или None)."""
    return _get("inv:revenue_account")


def set_revenue_account(account: dict) -> None:
    _set("inv:revenue_account", account)


# ── Склады (бар/кухня по подразделению) ──

def get_stores(department_id: str) -> list[dict] | None:
    """Склады из кеша."""
    return _get(f"inv:stores:{department_id}")


def set_stores(department_id: str, stores: list[dict]) -> None:
    _set(f"inv:stores:{department_id}", stores)


# ── Номенклатура по дереву (GOODS + DISH из gsheet_export_group) ──

def get_products() -> list[dict] | None:
    """Все товары по дереву из кеша (или None если протухли)."""
    return _get("inv:products_tree", ttl=PRODUCTS_TTL)


def set_products(products: list[dict]) -> None:
    _set("inv:products_tree", products)


# ── Сброс ──

def invalidate() -> None:
    """Сброс кеша (при завершении/отмене)."""
    keys_to_drop = [k for k in _store if k.startswith("inv:")]
    for k in keys_to_drop:
        del _store[k]
    if keys_to_drop:
        logger.debug("[inv_cache] Сброшено %d ключей", len(keys_to_drop))
