"""
Скрипт создания / пересоздания таблиц в PostgreSQL.
Запуск: python -m db.init_db
"""

import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path чтобы работал как модуль
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_config import setup_logging
setup_logging()

from db.engine import engine
from db.models import Base
# Импортируем ft_models чтобы SQLAlchemy увидел таблицы ft_*
import db.ft_models  # noqa: F401

logger = logging.getLogger(__name__)


async def create_tables() -> None:
    """Создать все таблицы (если не существуют) + безопасная миграция новых столбцов."""
    from sqlalchemy import text

    logger.info("Creating tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Миграция: добавляем столбцы, которых нет в старых таблицах.
    # IF NOT EXISTS — безопасно при повторном запуске.
    _MIGRATIONS = [
        "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS telegram_id BIGINT UNIQUE",
        "CREATE INDEX IF NOT EXISTS ix_iiko_employee_telegram_id ON iiko_employee (telegram_id)",
        "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS department_id UUID",
        "CREATE INDEX IF NOT EXISTS ix_iiko_employee_department_id ON iiko_employee (department_id)",
        # bot_admin — таблица создаётся через create_all, но индекс на всякий случай
        "CREATE INDEX IF NOT EXISTS ix_bot_admin_telegram_id ON bot_admin (telegram_id)",
        # iiko_stock_balance — индексы для быстрых выборок по store/product
        "CREATE INDEX IF NOT EXISTS ix_stock_balance_store ON iiko_stock_balance (store_id)",
        "CREATE INDEX IF NOT EXISTS ix_stock_balance_product ON iiko_stock_balance (product_id)",
    ]
    async with engine.begin() as conn:
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))

    logger.info("All tables created / migrated successfully")


async def drop_tables() -> None:
    """Удалить все таблицы (осторожно!)."""
    logger.info("Dropping all tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("All tables dropped")


async def main() -> None:
    await create_tables()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
