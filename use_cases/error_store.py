"""
Use-case: хранилище ошибок бота (таблица bot_error).

Все ERROR/CRITICAL из logging перехватываются DBErrorHandler
и сохраняются в PostgreSQL. Просмотр через /errors (сисадмины).

Публичный API:
  save_error()       — записать ошибку (вызывается из logging handler)
  get_recent()       — последние N ошибок (с фильтрами)
  get_stats()        — статистика: кол-во по уровням за 24ч / 7д / всего
  mark_resolved()    — пометить ошибку как решённую
  mark_all_resolved()— пометить все как решённые
  cleanup_old()      — удалить ошибки старше N дней
"""

import logging
import traceback as tb_module
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from db.engine import async_session_factory
from db.models import BotError, _now_kgd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Запись ошибки
# ═══════════════════════════════════════════════════════


async def save_error(
    *,
    level: str,
    logger_name: str,
    message: str,
    traceback: str | None = None,
    context: dict[str, Any] | None = None,
) -> int | None:
    """
    Сохранить ошибку в БД. Возвращает pk записи или None при неудаче.
    Никогда не бросает исключений (safe для вызова из logging handler).
    """
    try:
        async with async_session_factory() as session:
            err = BotError(
                level=level[:20],
                logger_name=logger_name[:300],
                message=message[:4000],
                traceback=traceback[:20_000] if traceback else None,
                context=context,
            )
            session.add(err)
            await session.commit()
            return err.pk
    except Exception:
        # Внутри error handler — нельзя логировать (рекурсия)
        return None


# ═══════════════════════════════════════════════════════
# Чтение ошибок
# ═══════════════════════════════════════════════════════


async def get_recent(
    *,
    limit: int = 20,
    level: str | None = None,
    resolved: bool | None = False,
    hours: int | None = None,
) -> list[BotError]:
    """Последние ошибки с фильтрами."""
    async with async_session_factory() as session:
        stmt = select(BotError).order_by(BotError.created_at.desc())

        if level:
            stmt = stmt.where(BotError.level == level.upper())
        if resolved is not None:
            stmt = stmt.where(BotError.resolved == resolved)
        if hours:
            since = _now_kgd() - timedelta(hours=hours)
            stmt = stmt.where(BotError.created_at >= since)

        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_by_pk(pk: int) -> BotError | None:
    """Получить одну ошибку по pk."""
    async with async_session_factory() as session:
        return await session.get(BotError, pk)


async def get_stats() -> dict[str, Any]:
    """Статистика ошибок: кол-во за 24ч, 7д, всего (по уровням)."""
    now = _now_kgd()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    async with async_session_factory() as session:
        # Общее кол-во нерешённых
        total_q = await session.execute(
            select(func.count()).where(BotError.resolved == False)  # noqa: E712
        )
        total_unresolved = total_q.scalar() or 0

        # За 24 часа по уровням
        day_q = await session.execute(
            select(BotError.level, func.count())
            .where(BotError.created_at >= day_ago)
            .group_by(BotError.level)
        )
        day_counts = {row[0]: row[1] for row in day_q}

        # За 7 дней
        week_q = await session.execute(
            select(func.count()).where(BotError.created_at >= week_ago)
        )
        week_total = week_q.scalar() or 0

    return {
        "unresolved": total_unresolved,
        "last_24h": day_counts,
        "last_7d": week_total,
    }


# ═══════════════════════════════════════════════════════
# Управление
# ═══════════════════════════════════════════════════════


async def mark_resolved(pk: int) -> bool:
    """Пометить ошибку как решённую."""
    async with async_session_factory() as session:
        result = await session.execute(
            update(BotError).where(BotError.pk == pk).values(resolved=True)
        )
        await session.commit()
        return result.rowcount > 0


async def mark_all_resolved() -> int:
    """Пометить все нерешённые ошибки как решённые. Возвращает кол-во."""
    async with async_session_factory() as session:
        result = await session.execute(
            update(BotError)
            .where(BotError.resolved == False)  # noqa: E712
            .values(resolved=True)
        )
        await session.commit()
        return result.rowcount


async def cleanup_old(days: int = 30) -> int:
    """Удалить решённые ошибки старше N дней. Возвращает кол-во удалённых."""
    cutoff = _now_kgd() - timedelta(days=days)
    async with async_session_factory() as session:
        result = await session.execute(
            delete(BotError).where(
                BotError.resolved == True,  # noqa: E712
                BotError.created_at < cutoff,
            )
        )
        await session.commit()
        return result.rowcount


# ═══════════════════════════════════════════════════════
# Logging Handler → БД
# ═══════════════════════════════════════════════════════


class DBErrorHandler(logging.Handler):
    """
    Logging handler: сохраняет ERROR/CRITICAL записи в таблицу bot_error.
    Асинхронный — планирует корутину через event loop.
    Дедупликация: не сохраняет одинаковые сообщения чаще раза в 30 сек.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._recent: dict[str, float] = {}
        self._dedup_sec = 30.0

    def emit(self, record: logging.LogRecord) -> None:
        import asyncio
        import time

        # Дедупликация: одинаковые ошибки не чаще раза в 30 сек
        key = f"{record.name}:{record.getMessage()[:200]}"
        now = time.monotonic()
        if (now - self._recent.get(key, 0)) < self._dedup_sec:
            return
        self._recent[key] = now

        # Ограничиваем размер кеша дедупликации
        if len(self._recent) > 500:
            cutoff = now - self._dedup_sec
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}

        # Собираем traceback если есть
        traceback_text = None
        if record.exc_info and record.exc_info[1]:
            traceback_text = "".join(tb_module.format_exception(*record.exc_info))

        # Контекст из extra (если передали)
        context = None
        for attr in ("error_context", "user_id", "handler_name"):
            val = getattr(record, attr, None)
            if val is not None:
                if context is None:
                    context = {}
                context[attr] = val

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                save_error(
                    level=record.levelname,
                    logger_name=record.name,
                    message=self.format(record)[:4000],
                    traceback=traceback_text,
                    context=context,
                )
            )
        except RuntimeError:
            pass  # нет event loop — при shutdown


# Singleton
_db_error_handler = DBErrorHandler()


def get_db_error_handler() -> DBErrorHandler:
    """Получить singleton DBErrorHandler для регистрации в root logger."""
    return _db_error_handler
