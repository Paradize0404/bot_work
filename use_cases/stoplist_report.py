"""
Use-case: ежедневный отчёт по стоп-листу (22:00 Калининград).

Логика:
  1. Из stoplist_history берём все записи за рабочий период (08:00–21:00 Калининград).
  2. Считаем суммарное время в стопе для каждого товара.
  3. Формируем текстовый отчёт.
  4. Отправляем всем авторизованным пользователям в Telegram.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, case, literal_column

from db.engine import async_session_factory
from db.models import StoplistHistory
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

LABEL = "StoplistReport"
_KGD_TZ = ZoneInfo("Europe/Kaliningrad")


# ═══════════════════════════════════════════════════════
# Получение статистики за день
# ═══════════════════════════════════════════════════════

async def fetch_daily_stats() -> list[dict]:
    """
    Получить статистику стоп-листа за сегодня (08:00–21:00 Калининград).

    Returns:
        [{product_id, name, total_seconds}, ...] отсортировано по убыванию времени.
    """
    now = now_kgd().replace(tzinfo=None)
    today = now.date()

    # Рабочие часы: 08:00–21:00 по Калининграду
    day_start = datetime(today.year, today.month, today.day, 8, 0)
    day_end = datetime(today.year, today.month, today.day, 21, 0)

    async with async_session_factory() as session:
        # Подсчёт: для каждого товара суммируем время пересечения с рабочим окном
        # started_at < day_end AND (ended_at IS NULL OR ended_at > day_start)
        result = await session.execute(
            select(
                StoplistHistory.product_id,
                StoplistHistory.name,
                StoplistHistory.terminal_group_id,
                StoplistHistory.started_at,
                StoplistHistory.ended_at,
            ).where(
                StoplistHistory.started_at < day_end,
                (StoplistHistory.ended_at.is_(None)) | (StoplistHistory.ended_at > day_start),
            )
        )
        rows = result.all()

    # Считаем суммарное время вручную (точнее чем в SQL для пересечения окон)
    product_stats: dict[str, dict] = {}
    for row in rows:
        pid = row.product_id
        name = row.name or "[?]"

        # Пересечение [started_at, ended_at] ∩ [day_start, day_end]
        actual_start = max(row.started_at, day_start)
        actual_end = min(row.ended_at or now, day_end)

        if actual_end <= actual_start:
            continue

        seconds = (actual_end - actual_start).total_seconds()

        if pid not in product_stats:
            product_stats[pid] = {"product_id": pid, "name": name, "total_seconds": 0}
        product_stats[pid]["total_seconds"] += seconds

    stats = sorted(product_stats.values(), key=lambda x: x["total_seconds"], reverse=True)
    return stats


# ═══════════════════════════════════════════════════════
# Форматирование отчёта
# ═══════════════════════════════════════════════════════

def _format_duration(seconds: int) -> str:
    """Перевод секунд в ЧЧ:ММ."""
    if seconds <= 0:
        return "00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02}:{minutes:02}"


def build_daily_report(stats: list[dict]) -> str:
    """
    Формирует текстовый отчёт по стоп-листу за день.
    """
    now = now_kgd()
    date_str = now.strftime("%d.%m.%Y")

    lines = [f"📊 Отчёт по стоп-листу за {date_str}", ""]

    if not stats:
        lines.append("Сегодня стопов не было 🎉")
        return "\n".join(lines)

    total_time = 0
    for item in stats[:50]:
        sec = int(item["total_seconds"])
        total_time += sec
        dur = _format_duration(sec)
        lines.append(f"▫️ {item['name']} — {dur}")

    if len(stats) > 50:
        lines.append(f"...и ещё {len(stats) - 50} позиций")

    lines.append("")
    lines.append(f"Всего позиций в стопе сегодня: {len(stats)}")
    lines.append(f"Суммарное время: {_format_duration(total_time)}")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:3950] + "\n\n...обрезано"
    return result


# ═══════════════════════════════════════════════════════
# Отправка отчёта всем пользователям
# ═══════════════════════════════════════════════════════

async def send_daily_stoplist_report(bot) -> int:
    """
    Ежедневный отчёт по стоп-листу → отправка всем авторизованным пользователям.
    Вызывается из scheduler в 22:00 по Калининграду.

    Returns:
        Количество успешно отправленных сообщений.
    """
    t0 = time.monotonic()
    logger.info("[%s] Формирую ежедневный отчёт...", LABEL)

    stats = await fetch_daily_stats()
    report = build_daily_report(stats)
    logger.info("[%s] Отчёт: %d позиций в стопе", LABEL, len(stats))

    # Получаем всех авторизованных пользователей
    from use_cases import permissions as perm_uc
    cache = await perm_uc._ensure_cache()
    user_ids = list(cache.keys())

    if not user_ids:
        logger.info("[%s] Нет пользователей для отправки отчёта", LABEL)
        return 0

    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, report)
            sent += 1
        except Exception:
            logger.warning("[%s] Не удалось отправить отчёт tg:%d", LABEL, uid)

    elapsed = time.monotonic() - t0
    logger.info("[%s] Отчёт отправлен %d/%d за %.1f сек", LABEL, sent, len(user_ids), elapsed)
    return sent
