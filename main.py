"""
Точка входа: инициализация логирования → создание таблиц → запуск бота.
Режим определяется автоматически:
  • WEBHOOK_URL задан  → webhook (Railway)
  • WEBHOOK_URL пуст   → long-polling (локальная разработка)
"""

import os
import sys
import signal
import asyncio
import logging

# Отключаем буферизацию stdout/stderr — иначе Railway не покажет логи в реалтайме
os.environ["PYTHONUNBUFFERED"] = "1"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from logging_config import setup_logging

# Логирование — первым делом
setup_logging()
logger = logging.getLogger(__name__)


def _build_bot_and_dp() -> tuple[Bot, Dispatcher]:
    from config import TELEGRAM_BOT_TOKEN
    from bot.global_commands import router as global_router, NavResetMiddleware
    from bot.handlers import router
    from bot.writeoff_handlers import router as writeoff_router
    from bot.admin_handlers import router as admin_router
    from bot.min_stock_handlers import router as min_stock_router
    from bot.invoice_handlers import router as invoice_router
    from bot.request_handlers import router as request_router

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    # Outer-middleware: сброс FSM при нажатии Reply-кнопки навигации
    dp.message.outer_middleware(NavResetMiddleware())
    dp.include_router(global_router)      # /cancel — первый, перехватывает всегда
    dp.include_router(admin_router)
    dp.include_router(writeoff_router)
    dp.include_router(min_stock_router)
    dp.include_router(invoice_router)
    dp.include_router(request_router)
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
    from bot.middleware import cancel_tracked_tasks
    await cancel_tracked_tasks()
    await close_iiko()
    await close_ft()
    await dispose_engine()
    logger.info("Shutdown complete")


# ─── Webhook mode (Railway) ────────────────────────────────────────
async def on_startup(bot: Bot) -> None:
    # Проверка БД в том же event loop, где работает aiohttp
    await _check_db()

    from config import WEBHOOK_URL, WEBHOOK_PATH, WEBHOOK_SECRET
    url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    logger.info("Setting webhook → %s", url)
    if not url.startswith("https://"):
        raise RuntimeError(f"Webhook URL must start with https://, got: {url}")
    await bot.set_webhook(url, drop_pending_updates=True, secret_token=WEBHOOK_SECRET)
    logger.info("Webhook set OK (with secret_token)")

    # Запускаем планировщик ежедневной синхронизации (07:00 Калининград)
    from use_cases.scheduler import start_scheduler
    start_scheduler(bot)


async def on_shutdown(bot: Bot) -> None:
    # Останавливаем планировщик
    from use_cases.scheduler import stop_scheduler
    stop_scheduler()
    # НЕ удаляем вебхук при shutdown — иначе при редеплое Railway
    # старый контейнер удалит вебхук, который новый уже поставил (гонка).
    # Вебхук будет перезаписан при следующем on_startup.
    await _cleanup()
    logger.info("Shutdown complete (webhook kept)")


def run_webhook() -> None:
    from config import WEBHOOK_PATH, WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_SECRET

    bot, dp = _build_bot_and_dp()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()

    # Health endpoint — Railway / load-balancer проверяет доступность
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info("Starting webhook server on %s:%s", WEBAPP_HOST, WEBAPP_PORT)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


# ─── Polling mode (local dev) ──────────────────────────────────────
async def run_polling() -> None:
    bot, dp = _build_bot_and_dp()
    await _check_db()

    # Снимаем вебхук, если остался с Railway
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook removed, starting polling...")

    # Запускаем планировщик ежедневной синхронизации (07:00 Калининград)
    from use_cases.scheduler import start_scheduler
    start_scheduler(bot)

    # Graceful shutdown по SIGTERM (Docker / Railway)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sigterm() -> None:
        logger.info("SIGTERM received, stopping polling...")
        stop_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    try:
        polling_task = asyncio.create_task(dp.start_polling(bot))
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        from use_cases.scheduler import stop_scheduler
        stop_scheduler()
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
