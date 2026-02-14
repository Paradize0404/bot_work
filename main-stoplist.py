import os
import asyncio
import asyncpg
import httpx
import requests
from dotenv import load_dotenv
import logging
import threading
import time
from datetime import datetime, timedelta
from daily_report import send_daily_report


load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,                      # показывает всё: DEBUG, INFO, WARNING, ERROR
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("stoplist.log", encoding="utf-8"),
        logging.StreamHandler()               # вывод в консоль (можно убрать, если не нужно)
    ]
)

DB_CONFIG = {
    "user": os.getenv("PGUSER"),
    "password": os.getenv("PGPASSWORD"),
    "database": os.getenv("PGDATABASE"),
    "host": os.getenv("PGHOST"),
    "port": os.getenv("PGPORT"),
}

BOT_TOKEN = os.getenv("BOT_TOKEN")
IIKO_ORG_ID = os.getenv("ORG_ID")


# ====================== БАЗА ======================

async def db():
    return await asyncpg.connect(**DB_CONFIG)


async def init_tables():
    conn = await db()

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_stoplist (
            sku TEXT PRIMARY KEY,
            balance REAL,
            name TEXT
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stoplist_message (
            chat_id BIGINT PRIMARY KEY,
            message_id BIGINT
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stoplist_history (
            id SERIAL PRIMARY KEY,
            sku TEXT,
            name TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            duration_seconds INT,
            date DATE
        );
    """)

    await conn.close()


async def get_all_chat_ids():
    conn = await db()
    rows = await conn.fetch("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL")
    await conn.close()
    return [row["telegram_id"] for row in rows]


# ====================== IIKO ======================

async def fetch_token():
    conn = await db()
    row = await conn.fetchrow("SELECT token FROM iiko_access_tokens ORDER BY created_at DESC LIMIT 1")
    await conn.close()
    return row["token"] if row else None


def fetch_terminal_groups(token):
    url = "https://api-ru.iiko.services/api/1/terminal_groups"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"organizationIds": [IIKO_ORG_ID]}

    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    data = r.json()

    return [g["id"] for g in data["terminalGroups"][0]["items"]]


def fetch_stoplist_raw(token, terminal_group_ids):
    url = "https://api-ru.iiko.services/api/1/stop_lists"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"organizationIds": [IIKO_ORG_ID], "terminalGroupIds": terminal_group_ids}

    r = requests.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        return []

    try:
        data = r.json()
        return data["terminalGroupStopLists"][0]["items"][0]["items"]
    except:
        return []

def run_daily_scheduler():
    """Фоновый бесконечный цикл, который ждёт 22:00 Калининграда и шлёт отчёт."""
    from zoneinfo import ZoneInfo
    
    while True:
        # Получаем текущее время в часовом поясе Калининграда
        kaliningrad_tz = ZoneInfo("Europe/Kaliningrad")
        now = datetime.now(kaliningrad_tz)

        # Следующая отправка: сегодня в 22:00 или завтра в 22:00
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logging.info(f"⏳ Жду до следующей отправки отчёта: {wait_seconds:.0f} сек (до {target.strftime('%d.%m.%Y %H:%M')})")

        time.sleep(wait_seconds)

        try:
            logging.info("📤 Авто-отправка вечернего отчёта...")
            asyncio.run(send_daily_report())
        except Exception as e:
            logging.error(f"Ошибка при авто-отправке отчёта: {e}")


async def map_names(items):
    conn = await db()

    product_ids = [i["productId"] for i in items]
    rows = await conn.fetch("""
        SELECT id, name FROM nomenclature WHERE id = ANY($1)
    """, product_ids)
    await conn.close()

    id2name = {str(r["id"]): r["name"] for r in rows}

    for item in items:
        item["name"] = id2name.get(item["productId"], "[НЕ НАЙДЕНО]")
        item["sku"] = item["productId"]  # создаём SKU как productId

    return items


# ====================== ИСТОРИЯ ======================

async def update_history(old_state, new_state):
    conn = await db()

    old_zero = {sku for sku, v in old_state.items() if v["balance"] == 0}
    new_zero = {sku for sku, v in new_state.items() if v["balance"] == 0}

    # вошли в стоп
    for sku in new_zero - old_zero:
        item = new_state[sku]
        await conn.execute("""
            INSERT INTO stoplist_history (sku, name, started_at, date)
            VALUES ($1, $2, NOW(), CURRENT_DATE)
        """, sku, item["name"])

    # вышли из стопа
    for sku in old_zero - new_zero:
        await conn.execute("""
            UPDATE stoplist_history
            SET ended_at = NOW(),
                duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at))
            WHERE sku=$1 AND ended_at IS NULL
        """, sku)

    await conn.close()


# ====================== DIFF ======================

def format_name(item):
    if item["balance"] > 0:
        return f"{item['name']} ({int(item['balance'])})"
    return f"{item['name']} — стоп"


def format_stoplist_message(added, removed, existing):
    msg = "Новые блюда в стоп-листе 🚫"
    msg += "\n" + "\n".join("▫️ " + format_name(i) for i in added) if added else "\n▫️ —"

    msg += "\n\nУдалены из стоп-листа ✅"
    msg += "\n" + "\n".join("▫️ " + i["name"] for i in removed) if removed else "\n▫️ —"

    msg += "\n\nОстались в стоп-листе"
    msg += "\n" + "\n".join("▫️ " + format_name(i) for i in existing) if existing else "\n▫️ —"

    return msg + "\n\n#стоплист"


async def sync_and_diff(stop_items):
    conn = await db()

    rows = await conn.fetch("SELECT sku, balance, name FROM active_stoplist")
    old = {r["sku"]: {"balance": r["balance"], "name": r["name"]} for r in rows}

    new = {i["sku"]: {"balance": i["balance"], "name": i["name"]} for i in stop_items}

    # Изменение: учитываем не только появление/удаление, но и изменение баланса
    added = []
    removed = []
    existing = []

    for sku in new:
        if sku not in old:
            # новое блюдо в стопе
            added.append(dict(sku=sku, **new[sku]))
        else:
            # блюдо было — проверяем изменение баланса
            old_balance = float(old[sku]["balance"])
            new_balance = float(new[sku]["balance"])

            if old_balance != new_balance:
                added.append(dict(sku=sku, **new[sku]))  # считаем как "добавленное изменение"
            else:
                existing.append(dict(sku=sku, **new[sku]))

    for sku in old:
        if sku not in new:
            removed.append(dict(sku=sku, **old[sku]))

    await update_history(old, new)

    await conn.execute("DELETE FROM active_stoplist")
    for sku, data in new.items():
        await conn.execute("""
            INSERT INTO active_stoplist (sku, balance, name)
            VALUES ($1, $2, $3)
        """, sku, data["balance"], data["name"])

    await conn.close()

    return added, removed, existing


# ====================== TELEGRAM ======================

async def update_stoplist_message(text):
    chat_ids = await get_all_chat_ids()
    if not chat_ids:
        return

    conn = await db()

    for chat_id in chat_ids:
        row = await conn.fetchrow("SELECT message_id FROM stoplist_message WHERE chat_id=$1", chat_id)

        if row:
            try:
                httpx.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": row["message_id"]}
                )
            except:
                pass

        r = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )

        data = r.json()

        # Если бот НЕ может отправить сообщение (403, 400, invalid chat, block и т.д.)
        if not data.get("ok"):
            logging.error(f"Ошибка Telegram при отправке в chat_id={chat_id}: {data}")
            continue

        msg_id = data["result"]["message_id"]

        await conn.execute("""
            INSERT INTO stoplist_message (chat_id, message_id)
            VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET message_id = EXCLUDED.message_id
        """, chat_id, msg_id)

    await conn.close()


# ====================== MAIN ======================

async def main():
    await init_tables()

    token = await fetch_token()
    if not token:
        print("❌ Нет токена iiko")
        return

    tg_ids = fetch_terminal_groups(token)
    raw = fetch_stoplist_raw(token, tg_ids)
    raw = await map_names(raw)

    added, removed, existing = await sync_and_diff(raw)

    if not added and not removed:
        print("ℹ️ Нет изменений")
        return

    text = format_stoplist_message(added, removed, existing)
    await update_stoplist_message(text)


if __name__ == "__main__":
    asyncio.run(main())
