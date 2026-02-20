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

from use_cases._ttl_cache import TtlCache

logger = logging.getLogger(__name__)

# TTL в секундах
CACHE_TTL = 600        # 10 минут
PRODUCTS_TTL = 600     # 10 минут (номенклатура по дереву)

_cache = TtlCache(default_ttl=CACHE_TTL)


# ── Контрагенты (suppliers) ──

def get_suppliers() -> list[dict] | None:
    """Все поставщики из кеша (или None если протухли)."""
    return _cache.get("inv:suppliers")


def set_suppliers(suppliers: list[dict]) -> None:
    _cache.set("inv:suppliers", suppliers)


# ── Счёт реализации ──

def get_revenue_account() -> dict | None:
    """Счёт реализации из кеша (или None)."""
    return _cache.get("inv:revenue_account")


def set_revenue_account(account: dict) -> None:
    _cache.set("inv:revenue_account", account)


# ── Склады (бар/кухня по подразделению) ──

def get_stores(department_id: str) -> list[dict] | None:
    """Склады из кеша."""
    return _cache.get(f"inv:stores:{department_id}")


def set_stores(department_id: str, stores: list[dict]) -> None:
    _cache.set(f"inv:stores:{department_id}", stores)


# ── Номенклатура по дереву (GOODS + DISH из gsheet_export_group) ──

def get_products() -> list[dict] | None:
    """Все товары по дереву из кеша (или None если протухли)."""
    return _cache.get("inv:products_tree", ttl=PRODUCTS_TTL)


def set_products(products: list[dict]) -> None:
    _cache.set("inv:products_tree", products)


# ── Сброс ──

def invalidate() -> None:
    """Сброс кеша (при завершении/отмене)."""
    dropped = _cache.drop_matching(lambda k: k.startswith("inv:"))
    if dropped:
        logger.debug("[inv_cache] Сброшено %d ключей", dropped)
