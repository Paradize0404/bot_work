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
    from bot.global_commands import (
        router as global_router,
        NavResetMiddleware,
        PermissionMiddleware,
        BlockCheckMiddleware,
    )
    from bot.handlers import router
    from bot.writeoff_handlers import router as writeoff_router
    from bot.min_stock_handlers import router as min_stock_router
    from bot.invoice_handlers import router as invoice_router
    from bot.request_handlers import router as request_router
    from bot.document_handlers import router as document_router
    from bot.pastry_handlers import router as pastry_router
    from bot.day_report_handlers import router as day_report_router
    from bot.pending_docs_handlers import router as pending_docs_router
    from bot.invoice_edit_handlers import router as invoice_edit_router
    from bot.salary_handlers import router as salary_router
    from bot.pnl_handlers import router as pnl_router
    from bot.report_sub_handlers import router as report_sub_router
    from bot.block_handlers import router as block_router
    from bot.error_handlers import router as error_router
    from bot.log_handlers import router as log_router
    from bot.retry_session import RetryAiohttpSession

    from aiogram.fsm.storage.redis import RedisStorage
    from config import REDIS_URL

    session = RetryAiohttpSession(max_retries=3, base_delay=1.0)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
    storage = RedisStorage.from_url(REDIS_URL)
    dp = Dispatcher(storage=storage)
    # Outer-middleware: блокировка пользователей (ПЕРВЫЙ — до всех остальных)
    dp.message.outer_middleware(BlockCheckMiddleware())
    dp.callback_query.outer_middleware(BlockCheckMiddleware())
    # Outer-middleware: сброс FSM при нажатии Reply-кнопки навигации
    dp.message.outer_middleware(NavResetMiddleware())
    # Outer-middleware: централизованная проверка прав (permission_map.py)
    dp.message.outer_middleware(PermissionMiddleware())
    dp.callback_query.outer_middleware(PermissionMiddleware())
    dp.include_router(global_router)  # /cancel — первый, перехватывает всегда
    dp.include_router(writeoff_router)
    dp.include_router(min_stock_router)
    dp.include_router(invoice_router)
    dp.include_router(request_router)
    dp.include_router(document_router)  # OCR распознавание накладных
    dp.include_router(pastry_router)
    dp.include_router(day_report_router)
    dp.include_router(pending_docs_router)  # Ожидают отправки — сводный список
    dp.include_router(invoice_edit_router)  # Редактирование pending накладных
    dp.include_router(salary_router)  # Управление списком ФОТ
    dp.include_router(pnl_router)  # Маппинг ОПИУ iiko→FinTablo
    dp.include_router(report_sub_router)  # Подписки на отчёты дня
    dp.include_router(block_router)  # Блокировка пользователей
    dp.include_router(error_router)  # /errors — хранилище ошибок (сисадмины)
    dp.include_router(log_router)  # /logs — все логи из БД (сисадмины)
    dp.include_router(router)

    # Error handler: ловим оставшиеся сетевые ошибки (после retry)
    from aiogram.exceptions import TelegramNetworkError, TelegramServerError
    from aiogram.types import ErrorEvent
    from use_cases._helpers import mask_secrets
    from use_cases.admin import alert_admins
    from use_cases.error_store import save_error
    import traceback as _tb

    @dp.errors()
    async def _global_error_handler(event: ErrorEvent):
        exception = event.exception
        if isinstance(exception, (TelegramNetworkError, TelegramServerError)):
            logger.warning(
                "[error-handler] %s suppressed after retries: %s",
                type(exception).__name__,
                exception,
            )
            return True  # mark handled, don't crash

        # Логируем с маскировкой секретов
        error_msg = mask_secrets(str(exception))
        logger.exception("[error-handler] Unhandled exception: %s", error_msg)

        # Собираем контекст для error_store
        ctx: dict = {"source": "global_error_handler"}
        try:
            upd = event.update
            if upd.message and upd.message.from_user:
                ctx["user_id"] = upd.message.from_user.id
                ctx["username"] = upd.message.from_user.username
                ctx["text"] = (upd.message.text or "")[:200]
            elif upd.callback_query and upd.callback_query.from_user:
                ctx["user_id"] = upd.callback_query.from_user.id
                ctx["username"] = upd.callback_query.from_user.username
                ctx["callback_data"] = (upd.callback_query.data or "")[:200]
        except Exception:
            pass

        # Сохраняем в БД (fire-and-forget, safe)
        await save_error(
            level="ERROR",
            logger_name="aiogram.errors",
            message=f"{type(exception).__name__}: {error_msg}"[:4000],
            traceback="".join(
                _tb.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )[:20_000],
            context=ctx,
        )

        # Отправляем алерт админам
        try:
            await alert_admins(
                bot, f"Unhandled exception:\n{type(exception).__name__}: {error_msg}"
            )
        except Exception:
            logger.debug("suppressed", exc_info=True)
        # Пытаемся ответить пользователю
        try:
            if event.update.message:
                await event.update.message.answer(
                    "⚠️ Произошла техническая ошибка. Администраторы уже уведомлены."
                )
            elif event.update.callback_query:
                await event.update.callback_query.answer(
                    "⚠️ Техническая ошибка", show_alert=True
                )
        except Exception:
            logger.debug("suppressed", exc_info=True)
        return True  # mark handled

    return bot, dp


async def _check_db() -> None:
    from db.engine import engine
    from sqlalchemy import text

    logger.info("Checking DB connection...")
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("DB connection OK")


async def _init_db() -> None:
    """Инициализация БД: создание таблиц + миграции (идемпотентно)."""
    from db.init_db import create_tables

    logger.info("Initializing database (tables + migrations)...")
    await create_tables()
    logger.info("Database initialized OK")


async def _check_iiko() -> None:
    """Startup self-check: iiko API доступен."""
    try:
        from iiko_auth import get_auth_token

        token = await get_auth_token()
        logger.info("iiko API OK (token len=%d)", len(token))
    except Exception:
        logger.warning("[startup] iiko API недоступен (не критично)", exc_info=True)


async def _check_fintablo() -> None:
    """Startup self-check: FinTablo API доступен."""
    try:
        from adapters.fintablo_api import fetch_categories

        cats = await fetch_categories()
        logger.info("FinTablo API OK (%d categories)", len(cats))
    except Exception:
        logger.warning("[startup] FinTablo API недоступен (не критично)", exc_info=True)


async def _check_staleness() -> None:
    """Staleness detection: предупреждение если последний SyncLog старше 24ч."""
    try:
        from db.engine import async_session_factory
        from sqlalchemy import text

        async with async_session_factory() as session:
            row = await session.execute(
                text("SELECT MAX(started_at) FROM iiko_sync_log")
            )
            last_sync = row.scalar()
            if last_sync is None:
                logger.warning(
                    "[startup] SyncLog пуст — синхронизация никогда не запускалась!"
                )
                return
            from use_cases._helpers import now_kgd

            age_hours = (now_kgd() - last_sync).total_seconds() / 3600
            if age_hours > 24:
                logger.warning(
                    "[startup] ⚠️ Последняя синхронизация %.1f ч назад (>24ч)! last=%s",
                    age_hours,
                    last_sync,
                )
            else:
                logger.info("[startup] Last sync %.1fч ago — OK", age_hours)
    except Exception:
        logger.warning("[startup] Не удалось проверить staleness", exc_info=True)


async def _warmup_caches() -> None:
    """Прогрев кешей при старте бота: permissions + user_context."""
    import time as _time

    t0 = _time.monotonic()
    try:
        from use_cases.permissions import _ensure_cache

        await _ensure_cache()

        from use_cases.user_context import get_user_context
        from db.engine import async_session_factory
        from sqlalchemy import text

        async with async_session_factory() as session:
            rows = await session.execute(
                text(
                    "SELECT telegram_id FROM iiko_employee WHERE telegram_id IS NOT NULL"
                )
            )
            tg_ids = [r[0] for r in rows]
        for tg_id in tg_ids:
            await get_user_context(tg_id)

        elapsed = _time.monotonic() - t0
        logger.info(
            "[startup] Cache warmup done: %d users, %.1fs", len(tg_ids), elapsed
        )
    except Exception:
        logger.warning("[startup] Cache warmup failed (non-critical)", exc_info=True)


async def _cleanup() -> None:
    from adapters.iiko_api import close_client as close_iiko
    from adapters.iiko_cloud_api import close_client as close_iiko_cloud
    from adapters.fintablo_api import close_client as close_ft
    from adapters.gpt5_vision_ocr import close_client as close_openai
    from db.engine import dispose_engine
    from bot.middleware import cancel_tracked_tasks

    # Pending writeoffs теперь в PostgreSQL — переживают рестарт, логировать не нужно
    try:
        from use_cases.pending_writeoffs import all_pending

        pending = await all_pending()
        if pending:
            logger.info(
                "[shutdown] %d pending writeoffs сохранены в БД (переживут рестарт): %s",
                len(pending),
                [(d.doc_id, d.author_name) for d in pending],
            )
    except Exception:
        logger.debug("suppressed", exc_info=True)
    await cancel_tracked_tasks()
    await close_iiko()
    await close_iiko_cloud()
    await close_ft()
    await close_openai()
    await dispose_engine()

    # Закрываем Redis-соединение FSM storage
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        # dp.storage уже может быть закрыт диспатчером, suppress ошибки
    except Exception:
        logger.debug("suppressed", exc_info=True)
    logger.info("Shutdown complete")


# ─── Регистрация команд бота (меню-кнопка слева от поля ввода) ────
async def _set_bot_commands(bot: Bot) -> None:
    """Регистрирует /start, /menu, /cancel в меню-кнопке Telegram."""
    from aiogram.types import BotCommand, BotCommandScopeDefault, MenuButtonCommands

    commands = [
        BotCommand(command="start", description="Старт / авторизация"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="cancel", description="Сброс"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("[startup] Bot menu commands registered (%d)", len(commands))


# ─── Webhook mode (Railway) ────────────────────────────────────────
async def on_startup(bot: Bot) -> None:
    # Привязываем бот к Telegram-оповещениям об ошибках
    from logging_config import get_telegram_handler

    get_telegram_handler().attach_bot(bot)

    # Инициализация БД: создание таблиц + миграции (идемпотентно)
    await _check_db()
    await _init_db()

    # Startup self-checks (не критичные — не блокируют старт)
    await asyncio.gather(
        _check_iiko(),
        _check_fintablo(),
        _check_staleness(),
        return_exceptions=True,
    )

    # Прогрев кешей (permissions + user_context)
    await _warmup_caches()

    # Регистрируем команды в меню-кнопке Telegram
    await _set_bot_commands(bot)

    from config import WEBHOOK_URL, WEBHOOK_PATH, WEBHOOK_SECRET

    url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    logger.info("Setting webhook → %s", url)
    if not url.startswith("https://"):
        raise RuntimeError(f"Webhook URL must start with https://, got: {url}")
    await bot.set_webhook(url, drop_pending_updates=True, secret_token=WEBHOOK_SECRET)
    logger.info("Webhook set OK (with secret_token)")

    # Регистрация вебхука iikoCloud (для всех привязанных организаций)
    try:
        from use_cases.cloud_org_mapping import get_all_cloud_org_ids
        from adapters.iiko_cloud_api import register_webhook
        from config import IIKO_CLOUD_ORG_ID, IIKO_CLOUD_WEBHOOK_SECRET

        org_ids = await get_all_cloud_org_ids()
        if IIKO_CLOUD_ORG_ID and IIKO_CLOUD_ORG_ID not in org_ids:
            org_ids.append(IIKO_CLOUD_ORG_ID)

        if org_ids:
            wh_url = f"{WEBHOOK_URL}/iiko-webhook"
            ok = 0
            for oid in org_ids:
                try:
                    await register_webhook(oid, wh_url, IIKO_CLOUD_WEBHOOK_SECRET)
                    ok += 1
                except Exception:
                    logger.warning("[startup] iikoCloud webhook failed for org %s", oid)
            logger.info(
                "[startup] iikoCloud webhook registered for %d/%d orgs → %s",
                ok,
                len(org_ids),
                wh_url,
            )
        else:
            logger.info(
                "[startup] No iikoCloud orgs mapped — skipping webhook registration"
            )
    except Exception:
        logger.warning(
            "[startup] iikoCloud webhook registration skipped (error)", exc_info=True
        )

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


async def _process_iiko_webhook(body: list[dict], bot: "Bot") -> None:
    """Фоновая обработка вебхука от iikoCloud (не блокирует HTTP-ответ)."""
    try:
        from use_cases.iiko_webhook_handler import handle_webhook

        result = await handle_webhook(body, bot)
        logger.info("[iiko-webhook] Обработано: %s", result)
    except Exception:
        logger.exception("[iiko-webhook] Ошибка обработки вебхука")


def run_webhook() -> None:
    from config import WEBHOOK_PATH, WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_SECRET

    bot, dp = _build_bot_and_dp()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()

    # Health endpoint — Railway / load-balancer проверяет доступность
    async def health(_request: web.Request) -> web.Response:
        try:
            from db.engine import async_session_factory
            from sqlalchemy import text

            async with async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            return web.json_response({"status": "ok", "db": "connected"})
        except Exception as e:
            logger.error("[health] DB check failed: %s", e)
            return web.json_response({"status": "error", "db": str(e)}, status=503)

    app.router.add_get("/health", health)

    # iikoCloud webhook endpoint — принимает события от iikoCloud
    async def iiko_webhook(request: web.Request) -> web.Response:
        from adapters.iiko_cloud_api import verify_webhook_auth

        # Верификация authToken (iikoCloud шлёт его в Authorization header)
        auth = request.headers.get("Authorization", "")
        if not verify_webhook_auth(auth):
            logger.warning(
                "[iiko-webhook] Невалидный auth: %s (headers: %s)",
                repr(auth[:30]) if auth else "empty",
                {
                    k: v[:20]
                    for k, v in request.headers.items()
                    if k.lower() not in ("cookie",)
                },
            )
            return web.Response(status=401, text="Unauthorized")

        try:
            body = await request.json()
        except Exception:
            logger.warning("[iiko-webhook] Невалидный JSON в теле запроса")
            return web.Response(status=400, text="Invalid JSON")

        # Логируем сырое тело для диагностики (первые 500 символов)
        import json as _json

        logger.info(
            "[iiko-webhook] Получен вебхук: %d событий, body[:500]=%s",
            len(body) if isinstance(body, list) else 1,
            _json.dumps(body, ensure_ascii=False, default=str)[:500],
        )

        # iikoCloud шлёт массив событий
        if not isinstance(body, list):
            body = [body]

        # Обрабатываем фоновой задачей чтобы быстро вернуть 200
        # (iikoCloud ожидает быстрый ответ, иначе может отключить вебхук)
        from bot.middleware import track_task

        track_task(_process_iiko_webhook(body, bot))

        return web.Response(status=200, text="ok")

    app.router.add_post("/iiko-webhook", iiko_webhook)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info("Starting webhook server on %s:%s", WEBAPP_HOST, WEBAPP_PORT)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


# ─── Polling mode (local dev) ──────────────────────────────────────
async def run_polling() -> None:
    bot, dp = _build_bot_and_dp()

    # Привязываем бот к Telegram-оповещениям об ошибках
    from logging_config import get_telegram_handler

    get_telegram_handler().attach_bot(bot)

    # Инициализация БД: создание таблиц + миграции (идемпотентно)
    await _check_db()
    await _init_db()

    # Startup self-checks (не критичные — не блокируют старт)
    await asyncio.gather(
        _check_iiko(),
        _check_fintablo(),
        _check_staleness(),
        return_exceptions=True,
    )

    # Прогрев кешей
    await _warmup_caches()

    # Снимаем вебхук, если остался с Railway
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook removed, starting polling...")

    # Регистрируем команды в меню-кнопке Telegram
    await _set_bot_commands(bot)

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
            {polling_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
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
