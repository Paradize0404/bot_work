"""
In-memory TTL-кеш для writeoff flow.

Кеширует данные, которые редко меняются (склады, счета, единицы измерения),
чтобы не бить по БД 400ms round-trip на каждый шаг FSM.

Стратегия:
  - preload() — прогревает кеш при клике на «📄 Документы» (фоново)
  - TTL 10 минут — потом данные считаются устаревшими и подтягиваются заново
  - invalidate() — сброс при отмене/завершении акта (чтобы не копить мусор)
  - get_unit_name() кешируется на 30 минут (единицы не меняются никогда)

~50 КБ RAM на 200 счетов + 50 складов + 200 единиц. Redis не нужен.
"""

import logging

from use_cases._ttl_cache import TtlCache

logger = logging.getLogger(__name__)

# TTL в секундах
CACHE_TTL = 600  # 10 минут для складов / счетов
UNIT_CACHE_TTL = 1800  # 30 минут для единиц измерения

_cache = TtlCache(default_ttl=CACHE_TTL)


def get_stores(department_id: str) -> list[dict] | None:
    """Склады из кеша (или None если протухли)."""
    return _cache.get(f"stores:{department_id}")


def set_stores(department_id: str, stores: list[dict]) -> None:
    _cache.set(f"stores:{department_id}", stores)


def get_accounts(store_name: str) -> list[dict] | None:
    """Счета из кеша (или None если протухли)."""
    return _cache.get(f"accounts:{store_name.lower()}")


def set_accounts(store_name: str, accounts: list[dict]) -> None:
    _cache.set(f"accounts:{store_name.lower()}", accounts)


def get_unit(unit_id: str) -> str | None:
    """Единица измерения из кеша (длинный TTL)."""
    return _cache.get(f"unit:{unit_id}", ttl=UNIT_CACHE_TTL)


def set_unit(unit_id: str, name: str) -> None:
    _cache.set(f"unit:{unit_id}", name)


def get_products(department_id: str = "all") -> list[dict] | None:
    """Товары из кеша для данного подразделения (или None если нет/протухли).

    department_id="all" — старый глобальный ключ (fallback для обратной совместимости).
    """
    return _cache.get(f"products:{department_id}")


def set_products(products: list[dict], department_id: str = "all") -> None:
    _cache.set(f"products:{department_id}", products)


def invalidate() -> None:
    """Полный сброс кеша (кроме единиц измерения)."""
    dropped = _cache.drop_matching(lambda k: not k.startswith("unit:"))
    if dropped:
        logger.debug("[wo_cache] Сброшено %d ключей", dropped)


def stats() -> dict:
    """Статистика кеша (для отладки)."""
    return _cache.stats()
