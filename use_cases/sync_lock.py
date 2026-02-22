import asyncio
import logging

logger = logging.getLogger(__name__)

_sync_locks: dict[str, asyncio.Lock] = {}


def get_sync_lock(entity: str) -> asyncio.Lock:
    """Получить lock для конкретного типа синхронизации."""
    if entity not in _sync_locks:
        _sync_locks[entity] = asyncio.Lock()
    return _sync_locks[entity]


async def run_sync_with_lock(entity: str, sync_coro):
    """
    Запуск синхронизации с гарантией единственного выполнения.
    Возвращает None, если синхронизация уже запущена.

    Использует acquire(blocking=False) вместо lock.locked() + async with
    чтобы избежать TOCTOU race condition.
    """
    lock = get_sync_lock(entity)
    acquired = lock.acquire_nowait()  # atomically try to acquire, no race
    if not acquired:
        logger.debug("[sync_lock] %s уже запущена, пропускаем", entity)
        return None
    try:
        return await sync_coro
    finally:
        lock.release()
