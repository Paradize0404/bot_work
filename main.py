"""
Точка входа: инициализация логирования → создание таблиц → запуск бота.
Режим определяется автоматически:
  • WEBHOOK_URL задан  → webhook (Railway)
  • WEBHOOK_URL пуст   → long-polling (локальная разработка)
"""

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from logging_config import setup_logging

# Логирование — первым делом
setup_logging()
logger = logging.getLogger(__name__)


def _build_bot_and_dp() -> tuple[Bot, Dispatcher]:
    from config import TELEGRAM_BOT_TOKEN
    from bot.handlers import router
    from bot.writeoff_handlers import router as writeoff_router
    from bot.admin_handlers import router as admin_router

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(writeoff_router)
    dp.include_router(router)
    return bot, dp


async def _check_db() -> None:
    from db.engine import engine
    from sqlalchemy import text
    logger.info("Checking DB connection...")
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("DB connection OK")


async def _cleanup() -> None:
    from adapters.iiko_api import close_client as close_iiko
    from adapters.fintablo_api import close_client as close_ft
    from db.engine import dispose_engine
    await close_iiko()
    await close_ft()
    await dispose_engine()
    logger.info("Shutdown complete")


# ─── Webhook mode (Railway) ────────────────────────────────────────
async def on_startup(bot: Bot) -> None:
    from config import WEBHOOK_URL, WEBHOOK_PATH
    url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(url, drop_pending_updates=True)
    logger.info("Webhook set → %s", url)


async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    await _cleanup()
    logger.info("Webhook removed")


def run_webhook() -> None:
    from config import WEBHOOK_PATH, WEBAPP_HOST, WEBAPP_PORT

    bot, dp = _build_bot_and_dp()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # Проверка БД перед стартом
    asyncio.run(_check_db())

    logger.info("Starting webhook server on %s:%s", WEBAPP_HOST, WEBAPP_PORT)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


# ─── Polling mode (local dev) ──────────────────────────────────────
async def run_polling() -> None:
    bot, dp = _build_bot_and_dp()
    await _check_db()

    # Снимаем вебхук, если остался с Railway
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook removed, starting polling...")

    try:
        await dp.start_polling(bot)
    finally:
        await _cleanup()


# ─── Entrypoint ────────────────────────────────────────────────────
if __name__ == "__main__":
    from config import WEBHOOK_URL

    try:
        if WEBHOOK_URL:
            run_webhook()
        else:
            asyncio.run(run_polling())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
