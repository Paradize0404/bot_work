"""
Use-case: блокировка пользователей бота.

Заблокированный пользователь полностью теряет доступ к боту:
бот игнорирует любые сообщения и callback от него.

Функции:
  - is_blocked(telegram_id) — проверить, заблокирован ли пользователь
  - block_user(telegram_id, ...) — заблокировать
  - unblock_user(telegram_id) — разблокировать
  - get_all_blocked() — список всех заблокированных
"""

import logging
import time

from sqlalchemy import select, delete

from db.engine import async_session_factory
from db.models import BlockedUser

logger = logging.getLogger(__name__)

# ── In-memory cache чтобы не ходить в БД на каждое сообщение ──
_blocked_ids: set[int] | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 60.0  # 1 минута


async def _ensure_cache() -> set[int]:
    """Загрузить / обновить кеш заблокированных telegram_id."""
    global _blocked_ids, _cache_ts
    now = time.monotonic()
    if _blocked_ids is not None and (now - _cache_ts) < _CACHE_TTL:
        return _blocked_ids

    async with async_session_factory() as session:
        stmt = select(BlockedUser.telegram_id)
        result = await session.execute(stmt)
        ids = {row[0] for row in result.all()}

    _blocked_ids = ids
    _cache_ts = now
    logger.debug("[blocked] cache refreshed: %d users", len(ids))
    return _blocked_ids


def _invalidate_cache() -> None:
    """Сбросить кеш (после block/unblock)."""
    global _blocked_ids, _cache_ts
    _blocked_ids = None
    _cache_ts = 0.0


async def is_blocked(telegram_id: int) -> bool:
    """Проверить, заблокирован ли пользователь (кешировано)."""
    ids = await _ensure_cache()
    return telegram_id in ids


async def block_user(
    telegram_id: int,
    user_name: str | None = None,
    blocked_by: int | None = None,
) -> bool:
    """
    Заблокировать пользователя.
    Возвращает True если заблокирован, False если уже был заблокирован.
    """
    async with async_session_factory() as session:
        existing = await session.execute(
            select(BlockedUser).where(BlockedUser.telegram_id == telegram_id)
        )
        if existing.scalar_one_or_none():
            return False

        session.add(
            BlockedUser(
                telegram_id=telegram_id,
                user_name=user_name,
                blocked_by=blocked_by,
            )
        )
        await session.commit()

    _invalidate_cache()
    logger.info(
        "[blocked] Пользователь tg:%d (%s) заблокирован admin:%s",
        telegram_id,
        user_name,
        blocked_by,
    )
    return True


async def unblock_user(telegram_id: int) -> bool:
    """
    Разблокировать пользователя.
    Возвращает True если разблокирован, False если не был заблокирован.
    """
    async with async_session_factory() as session:
        stmt = delete(BlockedUser).where(BlockedUser.telegram_id == telegram_id)
        result = await session.execute(stmt)
        await session.commit()
        removed = result.rowcount > 0

    if removed:
        _invalidate_cache()
        logger.info("[blocked] Пользователь tg:%d разблокирован", telegram_id)
    return removed


async def get_all_blocked() -> list[dict]:
    """
    Список всех заблокированных пользователей.
    Возвращает: [{telegram_id, user_name, blocked_at}, ...]
    """
    async with async_session_factory() as session:
        stmt = select(BlockedUser).order_by(BlockedUser.blocked_at.desc())
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "telegram_id": r.telegram_id,
            "user_name": r.user_name or f"tg:{r.telegram_id}",
            "blocked_at": r.blocked_at,
        }
        for r in rows
    ]
