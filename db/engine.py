"""
SQLAlchemy async engine + сессия.
Единственное место, где создаётся подключение к PostgreSQL.
"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import DATABASE_URL

logger = logging.getLogger(__name__)

# ── Engine ──
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=300,  # переподключаться каждые 5 мин (Railway дропает idle)
    connect_args={
        "server_settings": {
            "jit": "off",  # быстрее планирование batch INSERT (без JIT-компиляции)
        },
    },
)

# ── Session factory ──
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context-manager для DI / ручного использования."""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Закрыть пул при остановке приложения."""
    await engine.dispose()
    logger.info("DB engine disposed")
