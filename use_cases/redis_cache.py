import json
import logging
from typing import Any, TypeVar, Callable, Awaitable
from redis.asyncio import Redis
from config import REDIS_URL

logger = logging.getLogger(__name__)

_redis_client: Redis | None = None

async def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client

T = TypeVar('T')

async def get_cached_or_fetch(
    key: str,
    fetch_func: Callable[[], Awaitable[T]],
    ttl_seconds: int = 1800,
    serializer: Callable[[T], str] = json.dumps,
    deserializer: Callable[[str], T] = json.loads
) -> T:
    """
    Получить данные из Redis, если нет - вызвать fetch_func, сохранить в Redis и вернуть.
    """
    try:
        redis = await get_redis()
        cached = await redis.get(key)
        if cached:
            return deserializer(cached)
    except Exception as e:
        logger.warning("[redis_cache] Failed to get key %s: %s", key, e)

    # Cache miss or error
    data = await fetch_func()

    try:
        if data is not None:
            redis = await get_redis()
            await redis.setex(key, ttl_seconds, serializer(data))
    except Exception as e:
        logger.warning("[redis_cache] Failed to set key %s: %s", key, e)

    return data

async def get_cache(
    key: str,
    deserializer: Callable[[str], Any] = json.loads
) -> Any | None:
    try:
        redis = await get_redis()
        cached = await redis.get(key)
        if cached:
            return deserializer(cached)
    except Exception as e:
        logger.warning("[redis_cache] Failed to get key %s: %s", key, e)
    return None

async def invalidate_key(key: str) -> None:
    try:
        redis = await get_redis()
        await redis.delete(key)
    except Exception as e:
        logger.warning("[redis_cache] Failed to delete key %s: %s", key, e)

async def invalidate_pattern(pattern: str) -> int:
    try:
        redis = await get_redis()
        count = 0
        async for key in redis.scan_iter(match=pattern):
            await redis.delete(key)
            count += 1
        return count
    except Exception as e:
        logger.warning("[redis_cache] Failed to delete pattern %s: %s", pattern, e)
        return 0

async def set_cache(
    key: str,
    data: Any,
    ttl_seconds: int = 1800,
    serializer: Callable[[Any], str] = json.dumps
) -> None:
    try:
        redis = await get_redis()
        await redis.setex(key, ttl_seconds, serializer(data))
    except Exception as e:
        logger.warning("[redis_cache] Failed to set key %s: %s", key, e)
