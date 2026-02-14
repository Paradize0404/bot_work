"""
Use-case: автоматическая ежедневная синхронизация по расписанию.

Расписание:
  - Каждый день в 07:00 по Калининграду (Europe/Kaliningrad, UTC+2)
  - Синхронизируются: iiko (все справочники) + FinTablo + остатки + min/max из GSheet

Архитектура:
  - APScheduler AsyncIOScheduler с CronTrigger
  - Каждая задача оборачивается в try/except + SyncLog
  - Уведомление админов о результате через Telegram

Подключение:
  - start_scheduler(bot) — вызывается из main.py при старте бота
  - stop_scheduler()     — вызывается при shutdown
"""

import asyncio
import logging
import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Калининградская TZ для CronTrigger
_KGD_TZ = ZoneInfo("Europe/Kaliningrad")

TRIGGERED_BY = "scheduler"


# ═══════════════════════════════════════════════════════
# Основная задача: полная синхронизация
# ═══════════════════════════════════════════════════════

async def _daily_full_sync() -> None:
    """
    Ежедневная полная синхронизация всех данных:
      1. iiko: справочники + подразделения + склады + номенклатура и т.д.
      2. FinTablo: все 13 справочников
      3. Остатки по складам (sync_stock_balances)
      4. Min/max из Google Таблицы (sync_min_stock)
    """
    t0 = time.monotonic()
    started = now_kgd()
    logger.info("=== [scheduler] Ежедневная синхронизация СТАРТ (%s) ===", started.strftime("%Y-%m-%d %H:%M"))

    report_lines: list[str] = []

    # ── 1. iiko + FinTablo (параллельно) ──
    try:
        from use_cases.sync import sync_everything_with_report
        iiko_lines, ft_lines = await sync_everything_with_report(triggered_by=TRIGGERED_BY)
        report_lines.append("📊 <b>iiko:</b>")
        report_lines.extend(iiko_lines)
        report_lines.append("")
        report_lines.append("📈 <b>FinTablo:</b>")
        report_lines.extend(ft_lines)
    except Exception:
        logger.exception("[scheduler] Ошибка sync iiko/FinTablo")
        report_lines.append("❌ iiko/FinTablo: ошибка синхронизации")

    # ── 2. Остатки по складам ──
    try:
        from use_cases.sync_stock_balances import sync_stock_balances
        stock_count = await sync_stock_balances(triggered_by=TRIGGERED_BY)
        report_lines.append(f"\n📦 Остатки: ✅ {stock_count} позиций")
    except Exception:
        logger.exception("[scheduler] Ошибка sync остатков")
        report_lines.append("\n📦 Остатки: ❌ ошибка")

    # ── 3. Min/max из Google Таблицы ──
    try:
        from use_cases.sync_min_stock import sync_min_stock_from_gsheet
        gs_count = await sync_min_stock_from_gsheet(triggered_by=TRIGGERED_BY)
        report_lines.append(f"📋 Min/max GSheet: ✅ {gs_count} записей")
    except Exception:
        logger.exception("[scheduler] Ошибка sync min/max GSheet")
        report_lines.append("📋 Min/max GSheet: ❌ ошибка")

    elapsed = time.monotonic() - t0
    report_lines.append(f"\n⏱ Время: {elapsed:.1f} сек")
    logger.info("=== [scheduler] Ежедневная синхронизация ЗАВЕРШЕНА за %.1f сек ===", elapsed)

    # ── 4. Уведомление админов ──
    try:
        await _notify_admins_about_sync(report_lines)
    except Exception:
        logger.exception("[scheduler] Ошибка отправки уведомления админам")


# ═══════════════════════════════════════════════════════
# Вечерний отчёт по стоп-листу (22:00)
# ═══════════════════════════════════════════════════════

async def _daily_stoplist_report() -> None:
    """
    Ежедневный отчёт по стоп-листу: отправляется всем авторизованным пользователям.
    Вызывается APScheduler в 22:00 по Калининграду.
    """
    bot = _bot_ref
    if not bot:
        logger.warning("[scheduler] Bot reference not set, cannot send stoplist report")
        return

    try:
        from use_cases.stoplist_report import send_daily_stoplist_report
        sent = await send_daily_stoplist_report(bot)
        logger.info("[scheduler] Отчёт по стоп-листу отправлен: %d сообщений", sent)
    except Exception:
        logger.exception("[scheduler] Ошибка отправки отчёта по стоп-листу")


async def _notify_admins_about_sync(report_lines: list[str]) -> None:
    """Отправить результат синхронизации всем админам в Telegram."""
    from use_cases.permissions import get_admin_ids

    admin_ids = await get_admin_ids()
    if not admin_ids:
        logger.warning("[scheduler] Нет админов для уведомления о синхронизации")
        return

    header = f"🔄 <b>Авто-синхронизация</b> ({now_kgd().strftime('%d.%m.%Y %H:%M')})\n"
    text = header + "\n".join(report_lines)

    # Импортируем бот из глобального контекста
    from aiogram import Bot

    # Бот передаётся через _bot_ref (устанавливается в start_scheduler)
    bot = _bot_ref
    if not bot:
        logger.warning("[scheduler] Bot reference not set, cannot notify admins")
        return

    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.warning("[scheduler] Не удалось отправить уведомление админу tg:%d", admin_id)


# ═══════════════════════════════════════════════════════
# Управление планировщиком
# ═══════════════════════════════════════════════════════

_bot_ref = None  # Ссылка на Bot-инстанс для отправки уведомлений


def start_scheduler(bot) -> None:
    """
    Запустить APScheduler:
      - 07:00 — ежедневная синхронизация iiko + FinTablo + остатки + min/max
      - 22:00 — ежедневный отчёт по стоп-листу
    Вызывается из main.py при старте бота.
    """
    global _scheduler, _bot_ref
    _bot_ref = bot

    _scheduler = AsyncIOScheduler()

    # ── 07:00 — полная синхронизация ──
    _scheduler.add_job(
        _daily_full_sync,
        trigger=CronTrigger(hour=7, minute=0, timezone=_KGD_TZ),
        id="daily_full_sync",
        name="Ежедневная синхронизация iiko+FinTablo (07:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 22:00 — отчёт по стоп-листу ──
    _scheduler.add_job(
        _daily_stoplist_report,
        trigger=CronTrigger(hour=22, minute=0, timezone=_KGD_TZ),
        id="daily_stoplist_report",
        name="Ежедневный отчёт по стоп-листу (22:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()

    next_sync = _scheduler.get_job("daily_full_sync").next_run_time
    next_stoplist = _scheduler.get_job("daily_stoplist_report").next_run_time
    logger.info(
        "[scheduler] ✅ Планировщик запущен. Синхронизация: %s | Стоп-лист отчёт: %s",
        next_sync.strftime("%Y-%m-%d %H:%M %Z") if next_sync else "?",
        next_stoplist.strftime("%Y-%m-%d %H:%M %Z") if next_stoplist else "?",
    )


def stop_scheduler() -> None:
    """Остановить планировщик (graceful shutdown)."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Планировщик остановлен")
        _scheduler = None
