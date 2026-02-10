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
import time
from typing import Any

logger = logging.getLogger(__name__)

# TTL в секундах
CACHE_TTL = 600        # 10 минут для складов / счетов
UNIT_CACHE_TTL = 1800  # 30 минут для единиц измерения

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


def get_stores(department_id: str) -> list[dict] | None:
    """Склады из кеша (или None если протухли)."""
    return _get(f"stores:{department_id}")


def set_stores(department_id: str, stores: list[dict]) -> None:
    _set(f"stores:{department_id}", stores)


def get_accounts(store_name: str) -> list[dict] | None:
    """Счета из кеша (или None если протухли)."""
    return _get(f"accounts:{store_name.lower()}")


def set_accounts(store_name: str, accounts: list[dict]) -> None:
    _set(f"accounts:{store_name.lower()}", accounts)


def get_unit(unit_id: str) -> str | None:
    """Единица измерения из кеша (длинный TTL)."""
    return _get(f"unit:{unit_id}", ttl=UNIT_CACHE_TTL)


def set_unit(unit_id: str, name: str) -> None:
    _set(f"unit:{unit_id}", name)


def invalidate() -> None:
    """Полный сброс кеша (кроме единиц измерения)."""
    keys_to_drop = [k for k in _store if not k.startswith("unit:")]
    for k in keys_to_drop:
        del _store[k]
    if keys_to_drop:
        logger.debug("[wo_cache] Сброшено %d ключей", len(keys_to_drop))


def invalidate_all() -> None:
    """Полный сброс всего кеша включая единицы."""
    count = len(_store)
    _store.clear()
    logger.debug("[wo_cache] Полный сброс: %d ключей", count)


def stats() -> dict:
    """Статистика кеша (для отладки)."""
    now = time.monotonic()
    alive = sum(1 for _, (__, ts) in _store.items() if now - ts < CACHE_TTL)
    return {"total_keys": len(_store), "alive": alive}
