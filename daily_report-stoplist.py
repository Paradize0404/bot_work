import os
import asyncio
import asyncpg
import httpx
from datetime import datetime, date
from dotenv import load_dotenv
from datetime import timedelta
import logging

load_dotenv()

DB_CONFIG = {
    "user": os.getenv("PGUSER"),
    "password": os.getenv("PGPASSWORD"),
    "database": os.getenv("PGDATABASE"),
    "host": os.getenv("PGHOST"),
    "port": os.getenv("PGPORT"),
}

BOT_TOKEN = os.getenv("BOT_TOKEN")

# сюда впишешь Telegram ID, кому нужен отчёт в конце дня
REPORT_RECIPIENTS = [
    1877127405,
    1059714785,
    1078562089,
    5534584014
]


async def db():
    return await asyncpg.connect(**DB_CONFIG)


def format_duration(seconds: int) -> str:
    """Перевод секунд в формат ЧЧ:ММ."""
    if seconds <= 0:
        return "00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02}:{minutes:02}"


async def fetch_daily_stats():
    conn = await db()

    # Сегодняшняя дата в UTC
    today = datetime.utcnow().date()

    # формируем диапазон 06:00–19:00 UTC
    day_start = datetime(today.year, today.month, today.day, 6, 0)
    day_end   = datetime(today.year, today.month, today.day, 19, 0)

    rows = await conn.fetch("""
        SELECT
            sku,
            name,
            SUM(
                CASE
                    WHEN ended_at IS NULL THEN
                        EXTRACT(EPOCH FROM (LEAST($2, NOW()) - GREATEST(started_at, $1)))
                    ELSE
                        EXTRACT(EPOCH FROM (LEAST(ended_at, $2) - GREATEST(started_at, $1)))
                END
            ) AS total_sec
        FROM stoplist_history
        WHERE started_at < $2
          AND (ended_at IS NULL OR ended_at > $1)
        GROUP BY sku, name
        HAVING SUM(
                CASE
                    WHEN ended_at IS NULL THEN
                        EXTRACT(EPOCH FROM (LEAST($2, NOW()) - GREATEST(started_at, $1)))
                    ELSE
                        EXTRACT(EPOCH FROM (LEAST(ended_at, $2) - GREATEST(started_at, $1)))
                END
            ) > 0
        ORDER BY total_sec DESC;
    """, day_start, day_end)

    await conn.close()
    return rows


def build_report(rows):
    today = date.today().strftime("%d.%m.%Y")
    msg = f"📊 Отчёт по стоп-листу за {today}\n\n"

    if not rows:
        msg += "Сегодня не было стопов."
        return msg

    for row in rows:
        sku = row["sku"]
        name = row["name"]
        sec = int(row["total_sec"])

        msg += f"▫️ {name} — {format_duration(sec)}\n"

    return msg


async def send_report(text):
    if not REPORT_RECIPIENTS:
        logging.warning("⚠️ REPORT_RECIPIENTS пуст — отчёт некому отправлять.")
        return

    async with httpx.AsyncClient() as client:
        for chat_id in REPORT_RECIPIENTS:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text}
                )
                logging.info(f"✅ Отчёт отправлен → {chat_id}")
            except Exception as e:
                logging.error(f"❌ Ошибка отправки отчёта {chat_id}: {e}")


async def main():
    rows = await fetch_daily_stats()
    report = build_report(rows)
    await send_report(report)

async def send_daily_report():
    logging.info("📊 Начинаю формирование ежедневного отчёта...")
    rows = await fetch_daily_stats()
    report = build_report(rows)
    logging.info(f"📝 Отчёт сформирован: {len(rows)} позиций")
    await send_report(report)


if __name__ == "__main__":
    asyncio.run(main())
