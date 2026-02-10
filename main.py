"""
Точка входа: инициализация логирования → создание таблиц → запуск бота.
"""

import asyncio
import logging

from logging_config import setup_logging

# Логирование — первым делом
setup_logging()
logger = logging.getLogger(__name__)


async def main() -> None:
    from aiogram import Bot, Dispatcher
    from config import TELEGRAM_BOT_TOKEN
    from db.engine import engine, dispose_engine
    from bot.handlers import router
    from bot.writeoff_handlers import router as writeoff_router
    from bot.admin_handlers import router as admin_router

    # 1. Лёгкая проверка связи с БД (таблицы создаются через python -m db.init_db)
    logger.info("Checking DB connection...")
    from sqlalchemy import text
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("DB connection OK")

    # 2. Telegram bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(admin_router)       # управление админами
    dp.include_router(writeoff_router)   # списания — первым, чтобы перехватить кнопку
    dp.include_router(router)

    logger.info("Starting Telegram bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        from adapters.iiko_api import close_client as close_iiko
        from adapters.fintablo_api import close_client as close_ft
        await close_iiko()
        await close_ft()
        await dispose_engine()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
