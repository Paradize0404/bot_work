"""
Use-case: хранилище ВСЕХ логов бота в БД (таблица bot_log).

Буферизованный logging handler: копит записи и пишет batch INSERT
каждые FLUSH_INTERVAL секунд или при накоплении FLUSH_SIZE записей.

Публичный API:
  get_recent()       — последние N записей (фильтр по уровню, логгеру, поиск)
  get_stats()        — кол-во логов по уровням за 24ч / 7д
  cleanup_logs()     — удалить старые: INFO 3д, WARNING 14д, ERROR 90д
"""

import asyncio
import logging
import time
import traceback as tb_module
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from db.engine import async_session_factory
from db.models import BotLog, _now_kgd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# Настройки буфера
# ═══════════════════════════════════════════════════════

FLUSH_INTERVAL = 5.0  # секунд между flush
FLUSH_SIZE = 50  # макс записей до принудительного flush

# Retention (дней) по уровням
RETENTION = {
    "DEBUG": 1,
    "INFO": 3,
    "WARNING": 14,
    "ERROR": 90,
    "CRITICAL": 90,
}


# ═══════════════════════════════════════════════════════
# Буферизованная запись
# ═══════════════════════════════════════════════════════

_buffer: deque[dict[str, Any]] = deque(maxlen=2000)
_flush_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None


async def _flush_buffer() -> None:
    """Записать накопленный буфер в БД одним batch INSERT."""
    if not _buffer:
        return

    async with _flush_lock:
        # Забираем всё из буфера
        batch = []
        while _buffer:
            batch.append(_buffer.popleft())

    if not batch:
        return

    try:
        async with async_session_factory() as session:
            session.add_all([BotLog(**row) for row in batch])
            await session.commit()
    except Exception:
        # Не логируем — мы внутри logging handler, рекурсия
        pass


async def _periodic_flush() -> None:
    """Фоновая задача: flush буфера каждые FLUSH_INTERVAL секунд."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            await _flush_buffer()
        except Exception:
            pass


def _ensure_flush_task() -> None:
    """Создать фоновую задачу flush (если ещё нет)."""
    global _flush_task
    if _flush_task is not None and not _flush_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _flush_task = loop.create_task(_periodic_flush(), name="log_store_flush")
    except RuntimeError:
        pass


def _enqueue(row: dict[str, Any]) -> None:
    """Добавить запись в буфер. Если буфер полон — flush."""
    _buffer.append(row)
    if len(_buffer) >= FLUSH_SIZE:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_flush_buffer())
        except RuntimeError:
            pass


# ═══════════════════════════════════════════════════════
# Чтение логов
# ═══════════════════════════════════════════════════════


async def get_recent(
    *,
    limit: int = 30,
    level: str | None = None,
    logger_name: str | None = None,
    search: str | None = None,
    hours: int | None = None,
    offset: int = 0,
) -> list[BotLog]:
    """Последние логи с фильтрами."""
    async with async_session_factory() as session:
        stmt = select(BotLog).order_by(BotLog.created_at.desc())

        if level:
            stmt = stmt.where(BotLog.level == level.upper())
        if logger_name:
            stmt = stmt.where(BotLog.logger_name.ilike(f"%{logger_name}%"))
        if search:
            stmt = stmt.where(BotLog.message.ilike(f"%{search}%"))
        if hours:
            since = _now_kgd() - timedelta(hours=hours)
            stmt = stmt.where(BotLog.created_at >= since)

        stmt = stmt.offset(offset).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_stats() -> dict[str, Any]:
    """Статистика логов: кол-во по уровням за 24ч и 7д."""
    now = _now_kgd()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    async with async_session_factory() as session:
        # За 24 часа по уровням
        day_q = await session.execute(
            select(BotLog.level, func.count())
            .where(BotLog.created_at >= day_ago)
            .group_by(BotLog.level)
        )
        day_counts = {row[0]: row[1] for row in day_q}

        # За 7 дней
        week_q = await session.execute(
            select(func.count()).where(BotLog.created_at >= week_ago)
        )
        week_total = week_q.scalar() or 0

        # Общее кол-во
        total_q = await session.execute(select(func.count()).select_from(BotLog))
        total = total_q.scalar() or 0

    return {
        "last_24h": day_counts,
        "last_7d": week_total,
        "total": total,
    }


async def count_by_filter(
    *,
    level: str | None = None,
    logger_name: str | None = None,
    search: str | None = None,
    hours: int | None = None,
) -> int:
    """Подсчёт записей по фильтрам (для пагинации)."""
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(BotLog)
        if level:
            stmt = stmt.where(BotLog.level == level.upper())
        if logger_name:
            stmt = stmt.where(BotLog.logger_name.ilike(f"%{logger_name}%"))
        if search:
            stmt = stmt.where(BotLog.message.ilike(f"%{search}%"))
        if hours:
            since = _now_kgd() - timedelta(hours=hours)
            stmt = stmt.where(BotLog.created_at >= since)
        result = await session.execute(stmt)
        return result.scalar() or 0


# ═══════════════════════════════════════════════════════
# Очистка
# ═══════════════════════════════════════════════════════


async def cleanup_logs() -> dict[str, int]:
    """
    Удалить старые логи по retention-политике.
    Возвращает кол-во удалённых по уровням.
    """
    now = _now_kgd()
    deleted = {}

    async with async_session_factory() as session:
        for level_name, days in RETENTION.items():
            cutoff = now - timedelta(days=days)
            result = await session.execute(
                delete(BotLog).where(
                    BotLog.level == level_name,
                    BotLog.created_at < cutoff,
                )
            )
            if result.rowcount:
                deleted[level_name] = result.rowcount
        await session.commit()

    return deleted


# ═══════════════════════════════════════════════════════
# Logging Handler → БД (буферизованный)
# ═══════════════════════════════════════════════════════


class DBLogHandler(logging.Handler):
    """
    Logging handler: ВСЕ логи (INFO+) → таблица bot_log.
    Буферизованный: пишет batch INSERT каждые 5 сек / 50 записей.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        # Пропускаем логи от самого себя и от SQLAlchemy (иначе рекурсия)
        if record.name in ("use_cases.log_store", "sqlalchemy.engine"):
            return

        _ensure_flush_task()

        # Traceback при наличии
        traceback_text = None
        if record.exc_info and record.exc_info[1]:
            traceback_text = "".join(tb_module.format_exception(*record.exc_info))
            if traceback_text:
                traceback_text = traceback_text[:20_000]

        _enqueue(
            {
                "level": record.levelname[:10],
                "logger_name": record.name[:300],
                "message": self.format(record)[:4000],
                "traceback": traceback_text,
            }
        )


# Singleton
_db_log_handler = DBLogHandler()


def get_db_log_handler() -> DBLogHandler:
    """Получить singleton DBLogHandler для регистрации в root logger."""
    return _db_log_handler
