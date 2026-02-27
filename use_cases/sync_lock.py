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

    В asyncio (single-threaded) проверка locked() + немедленный async with
    безопасна: между ними нет await, значит нет yield и другая корутина
    не может вклиниться.
    """
    lock = get_sync_lock(entity)
    if lock.locked():
        logger.debug("[sync_lock] %s уже запущена, пропускаем", entity)
        return None
    async with lock:
        return await sync_coro
