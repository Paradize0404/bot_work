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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from use_cases._helpers import now_kgd, KGD_TZ

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

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
      4. Min/max из Google Таблицы → БД (sync_min_stock)
      5. Номенклатура БД → Google Таблица «Мин.остатки» (sync_nomenclature_to_gsheet)
      6. Обновить «Маппинг Справочник» в Google Таблице (GOODS-товары)
    """
    t0 = time.monotonic()
    started = now_kgd()
    logger.info(
        "=== [scheduler] Ежедневная синхронизация СТАРТ (%s) ===",
        started.strftime("%Y-%m-%d %H:%M"),
    )

    report_lines: list[str] = []

    # ── 1. iiko + FinTablo (параллельно) ──
    try:
        from use_cases.sync import sync_everything_with_report

        iiko_lines, ft_lines = await sync_everything_with_report(
            triggered_by=TRIGGERED_BY
        )
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

    # ── 3. Min/max из Google Таблицы (GSheet → БД) ──
    try:
        from use_cases.sync_min_stock import sync_min_stock_from_gsheet

        gs_count = await sync_min_stock_from_gsheet(triggered_by=TRIGGERED_BY)
        report_lines.append(f"📋 Min/max GSheet→БД: ✅ {gs_count} записей")
    except Exception:
        logger.exception("[scheduler] Ошибка sync min/max GSheet")
        report_lines.append("📋 Min/max GSheet→БД: ❌ ошибка")

    # ── 4. Номенклатура БД → GSheet (обновляет лист мин.остатков) ──
    try:
        from use_cases.sync_min_stock import sync_nomenclature_to_gsheet

        nomen_count = await sync_nomenclature_to_gsheet(triggered_by=TRIGGERED_BY)
        report_lines.append(f"📦 Номенкл.→GSheet: ✅ {nomen_count} товаров")
    except Exception:
        logger.exception("[scheduler] Ошибка sync номенклатуры в GSheet")
        report_lines.append("📦 Номенкл.→GSheet: ❌ ошибка")

    # ── 5. Обновить справочник товаров для маппинга ──
    try:
        from use_cases.ocr_mapping import refresh_ref_sheet

        ref_count = await refresh_ref_sheet()
        report_lines.append(f"🗂 Маппинг справочник: ✅ {ref_count} товаров")
    except Exception:
        logger.exception("[scheduler] Ошибка обновления справочника маппинга")
        report_lines.append("🗂 Маппинг справочник: ❌ ошибка")

    # ── 6. История ставок — синхронизация перед расчётом ФОТ ──
    try:
        from use_cases.salary_history import sync_salary_history

        hist_count = await sync_salary_history(triggered_by=TRIGGERED_BY)
        report_lines.append(f"📋 История ставок: ✅ {hist_count} записей")
    except Exception:
        logger.exception("[scheduler] Ошибка синхронизации истории ставок")
        report_lines.append("📋 История ставок: ❌ ошибка")

    # ── Опорная дата: вчера ──
    # Cron запускается в 07:00 — данные «сегодня» ещё пусты.
    # Используем вчерашний день, чтобы захватить полный предыдущий день:
    #   1 марта 07:00 → yesterday = 28 февраля → ФОТ/ОПИУ за февраль.
    #   2 марта 07:00 → yesterday = 1 марта   → ФОТ/ОПИУ за март.
    yesterday = (now_kgd() - timedelta(days=1)).date()
    yesterday_dt = datetime.combine(yesterday, datetime.min.time())
    logger.info("[scheduler] Опорная дата: %s (yesterday)", yesterday)

    # ── 7. ФОТ — расчёт зарплат ──
    try:
        from use_cases.payroll import update_fot_sheet

        fot_count = await update_fot_sheet(
            triggered_by=TRIGGERED_BY,
            target_date=yesterday,
        )
        report_lines.append(f"💰 ФОТ: ✅ {fot_count} сотрудников")
    except Exception:
        logger.exception("[scheduler] Ошибка обновления ФОТ")
        report_lines.append("💰 ФОТ: ❌ ошибка")

    # ── 8. Выгрузка ФОТ → FinTablo (дельта-синхронизация) ──
    try:
        from use_cases.fintablo_salary_sync import sync_fot_to_fintablo

        ft_stats = await sync_fot_to_fintablo(
            triggered_by=TRIGGERED_BY,
            target_date=yesterday,
        )
        ft_upd = ft_stats.get("updated", 0)
        ft_err = ft_stats.get("errors", 0)
        ft_total = ft_stats.get("total", 0)
        if ft_err:
            report_lines.append(
                f"📊 FinTablo: ⚠️ {ft_upd}/{ft_total} обновлено, {ft_err} ошибок"
            )
        else:
            report_lines.append(f"📊 FinTablo: ✅ {ft_upd}/{ft_total} обновлено")
    except Exception:
        logger.exception("[scheduler] Ошибка выгрузки ФОТ в FinTablo")
        report_lines.append("📊 FinTablo: ❌ ошибка")

    # ── 9. ОПИУ → FinTablo (маппинг iiko Account → FT PnL) ──
    opiu_stats: dict = {}
    try:
        from use_cases.pnl_sync import update_opiu

        opiu_stats = await update_opiu(
            triggered_by=TRIGGERED_BY,
            target_date=yesterday_dt,
        )
        opiu_upd = opiu_stats.get("updated", 0)
        opiu_err = opiu_stats.get("errors", 0)
        opiu_skip = opiu_stats.get("skipped", 0)
        unmapped = opiu_stats.get("unmapped_keys", [])
        if opiu_err:
            report_lines.append(
                f"📊 ОПИУ: ⚠️ {opiu_upd} обновлено, {opiu_skip} пропущено, {opiu_err} ошибок"
            )
        else:
            report_lines.append(
                f"📊 ОПИУ: ✅ {opiu_upd} обновлено, {opiu_skip} пропущено"
            )
        if unmapped:
            report_lines.append(f"   ⚠️ Не разнесено: {', '.join(unmapped[:10])}")
    except Exception:
        logger.exception("[scheduler] Ошибка обновления ОПИУ в FinTablo")
        report_lines.append("📊 ОПИУ: ❌ ошибка")

    # ── 10. Выручка → FinTablo (маппинг PayType / CookingPlaceType → FT PnL) ──
    try:
        from use_cases.revenue_sync import update_revenue

        rev_stats = await update_revenue(
            triggered_by=TRIGGERED_BY,
            target_date=yesterday_dt,
        )
        rev_upd = rev_stats.get("updated", 0)
        rev_err = rev_stats.get("errors", 0)
        rev_skip = rev_stats.get("skipped", 0)
        rev_unmapped = rev_stats.get("unmapped_keys", [])
        if rev_err:
            report_lines.append(
                f"💰 Выручка: ⚠️ {rev_upd} обновлено, {rev_skip} пропущено, {rev_err} ошибок"
            )
        else:
            report_lines.append(
                f"💰 Выручка: ✅ {rev_upd} обновлено, {rev_skip} пропущено"
            )
        if rev_unmapped:
            report_lines.append(f"   ⚠️ Не разнесено: {', '.join(rev_unmapped[:10])}")
    except Exception:
        logger.exception("[scheduler] Ошибка обновления выручки в FinTablo")
        report_lines.append("💰 Выручка: ❌ ошибка")

    elapsed = time.monotonic() - t0
    report_lines.append(f"\n⏱ Время: {elapsed:.1f} сек")
    logger.info(
        "=== [scheduler] Ежедневная синхронизация ЗАВЕРШЕНА за %.1f сек ===", elapsed
    )

    # ── Уведомление админов ──
    try:
        await _notify_admins_about_sync(report_lines)
    except Exception:
        logger.exception("[scheduler] Ошибка отправки уведомления админам")

    # ── Отдельный детальный ОПИУ-отчёт с кнопкой повтора ──
    try:
        await _notify_admins_opiu(opiu_stats)
    except Exception:
        logger.exception("[scheduler] Ошибка отправки ОПИУ-отчёта")


# ═══════════════════════════════════════════════════════
# Вечерний отчёт по стоп-листу (22:00)
# ═══════════════════════════════════════════════════════


async def _daily_stoplist_report() -> None:
    """
    Ежедневный отчёт по стоп-листу: отправляется всем авторизованным пользователям.
    Вызывается APScheduler в 22:00 по Калининграду.
    """
    # ОТКЛЮЧЕНО: отчёт по стоп-листу временно не отправляется
    return

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


# ═══════════════════════════════════════════════════════
# Ночное авто-перемещение расходных материалов (23:00)
# ═══════════════════════════════════════════════════════


async def _daily_negative_transfer() -> None:
    """
    Авто-перемещение отрицательных остатков расходных материалов.
    По всем ресторанам автоматически (паттерн "Хоз. товары (РЕСТОРАН)" → "Бар/Кухня (РЕСТОРАН)").
    Вызывается APScheduler в 23:00 по Калининграду.
    """
    logger.info("[scheduler] Старт авто-перемещения расходных материалов (23:00)")
    try:
        from use_cases.negative_transfer import run_negative_transfer_once

        result = await run_negative_transfer_once(triggered_by=TRIGGERED_BY)
        status = result.get("status", "?")
        restaurants = result.get("restaurants", {})

        if status == "no_restaurants":
            logger.warning("[scheduler] Авто-перемещение: рестораны не найдены")
        elif status == "nothing_to_transfer":
            logger.info("[scheduler] Авто-перемещение: нет отрицательных остатков")
        elif status == "ok":
            total = sum(len(r.get("transfers", [])) for r in restaurants.values())
            logger.info(
                "[scheduler] Авто-перемещение завершено: %d перемещений по %d ресторанам",
                total,
                len(restaurants),
            )
        else:
            logger.info("[scheduler] Авто-перемещение: status=%s", status)

        # Уведомить сис.админов о результате
        await _notify_admins_negative_transfer(result)

    except Exception:
        logger.exception("[scheduler] Ошибка авто-перемещения расходных материалов")
        await _notify_admins_negative_transfer({"status": "error"})


async def _notify_admins_negative_transfer(result: dict) -> None:
    """Отправить краткий итог авто-перемещения админам."""
    from use_cases.permissions import get_sysadmin_ids

    bot = _bot_ref
    if not bot:
        return

    status = result.get("status", "?")
    restaurants = result.get("restaurants", {})

    if status == "no_restaurants":
        text = "🚚 Авто-перемещение расх.мат.: рестораны не найдены (проверьте склады в БД)"
    elif status == "nothing_to_transfer":
        text = "🚚 Авто-перемещение расх.мат.: отрицательных остатков нет — нечего перемещать ✅"
    elif status == "locked":
        text = "🚚 Авто-перемещение расх.мат.: пропущено (уже выполнялось)"
    elif status == "error":
        text = "🚚 Авто-перемещение расх.мат.: ❌ ОШИБКА — смотрите логи"
    else:
        lines = [
            f"🚚 <b>Авто-перемещение расх.мат.</b> ({now_kgd().strftime('%d.%m.%Y 23:00')})"
        ]
        for rest, data in sorted(restaurants.items()):
            transfers = data.get("transfers", [])
            skipped = data.get("skipped_products", [])
            ok_count = sum(1 for t in transfers if "error" not in t)
            err_count = sum(1 for t in transfers if "error" in t)
            line = f"  • {rest}: {ok_count} перем."
            if err_count:
                line += f" ❌{err_count} ошибок"
            if skipped:
                line += f" (пропущено {len(skipped)} товаров)"
            lines.append(line)
        text = "\n".join(lines)

    try:
        admin_ids = await get_sysadmin_ids()
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                logger.warning(
                    "[scheduler] Не удалось уведомить tg:%d о перемещении", admin_id
                )
    except Exception:
        logger.exception("[scheduler] Ошибка при уведомлении о перемещении")


async def _notify_admins_opiu(opiu_stats: dict) -> None:
    """Отправить детальный ОПИУ-отчёт; при unmapped — с кнопкой повтора."""
    if not opiu_stats:
        return

    from use_cases.permissions import get_sysadmin_ids

    bot = _bot_ref
    if not bot:
        return

    from bot.pnl_handlers import format_opiu_result
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    text = format_opiu_result(opiu_stats)
    has_problems = bool(opiu_stats.get("errors") or opiu_stats.get("unmapped_keys"))

    kb = None
    if has_problems:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Повторить синхронизацию ОПИУ",
                        callback_data="pnl_update",
                    )
                ]
            ]
        )

    admin_ids = await get_sysadmin_ids()
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.warning(
                "[scheduler] Не удалось отправить ОПИУ-отчёт tg:%d", admin_id
            )


async def _notify_admins_about_sync(report_lines: list[str]) -> None:
    """Отправить результат синхронизации всем админам в Telegram."""
    from use_cases.permissions import get_sysadmin_ids

    admin_ids = await get_sysadmin_ids()
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
            logger.warning(
                "[scheduler] Не удалось отправить уведомление админу tg:%d", admin_id
            )


# ═══════════════════════════════════════════════════════
# Управление планировщиком
# ═══════════════════════════════════════════════════════

_bot_ref = None  # Ссылка на Bot-инстанс для отправки уведомлений


async def _daily_error_cleanup() -> None:
    """Удалить решённые ошибки старше 30 дней (03:00)."""
    try:
        from use_cases.error_store import cleanup_old

        deleted = await cleanup_old(days=30)
        if deleted:
            logger.info(
                "[scheduler] Очистка ошибок: удалено %d старых записей", deleted
            )
    except Exception:
        logger.exception("[scheduler] Ошибка очистки bot_error")


async def _daily_log_cleanup() -> None:
    """Удалить старые логи по retention-политике (03:10)."""
    try:
        from use_cases.log_store import cleanup_logs

        deleted = await cleanup_logs()
        if deleted:
            detail = ", ".join(f"{k}: {v}" for k, v in deleted.items())
            logger.info("[scheduler] Очистка логов: %s", detail)
    except Exception:
        logger.exception("[scheduler] Ошибка очистки bot_log")


def start_scheduler(bot) -> None:
    """
    Запустить APScheduler:
      - 07:00 — ежедневная синхронизация iiko + FinTablo + остатки + min/max + номенклатура GSheet + маппинг справочник
      - 22:00 — ежедневный отчёт по стоп-листу
      - 23:00 — авто-перемещение отрицательных остатков расходных материалов
    Вызывается из main.py при старте бота.
    """
    global _scheduler, _bot_ref
    _bot_ref = bot

    _scheduler = AsyncIOScheduler()

    # ── 07:00 — полная синхронизация ──
    _scheduler.add_job(
        _daily_full_sync,
        trigger=CronTrigger(hour=7, minute=0, timezone=KGD_TZ),
        id="daily_full_sync",
        name="Ежедневная синхронизация iiko+FinTablo (07:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 22:00 — отчёт по стоп-листу ──
    _scheduler.add_job(
        _daily_stoplist_report,
        trigger=CronTrigger(hour=22, minute=0, timezone=KGD_TZ),
        id="daily_stoplist_report",
        name="Ежедневный отчёт по стоп-листу (22:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 23:00 — авто-перемещение расходных материалов ──
    _scheduler.add_job(
        _daily_negative_transfer,
        trigger=CronTrigger(hour=23, minute=0, timezone=KGD_TZ),
        id="daily_negative_transfer",
        name="Авто-перемещение расх.мат. (23:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 03:00 — очистка старых решённых ошибок (30+ дней) ──
    _scheduler.add_job(
        _daily_error_cleanup,
        trigger=CronTrigger(hour=3, minute=0, timezone=KGD_TZ),
        id="daily_error_cleanup",
        name="Очистка старых ошибок (03:00 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 03:10 — очистка логов по retention ──
    _scheduler.add_job(
        _daily_log_cleanup,
        trigger=CronTrigger(hour=3, minute=10, timezone=KGD_TZ),
        id="daily_log_cleanup",
        name="Очистка логов по retention (03:10 Калининград)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()

    next_sync = _scheduler.get_job("daily_full_sync").next_run_time
    next_stoplist = _scheduler.get_job("daily_stoplist_report").next_run_time
    next_transfer = _scheduler.get_job("daily_negative_transfer").next_run_time
    logger.info(
        "[scheduler] ✅ Планировщик запущен. Синхронизация: %s | Стоп-лист: %s | Перемещение: %s",
        next_sync.strftime("%Y-%m-%d %H:%M %Z") if next_sync else "?",
        next_stoplist.strftime("%Y-%m-%d %H:%M %Z") if next_stoplist else "?",
        next_transfer.strftime("%Y-%m-%d %H:%M %Z") if next_transfer else "?",
    )


def stop_scheduler() -> None:
    """Остановить планировщик (graceful shutdown)."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Планировщик остановлен")
        _scheduler = None
