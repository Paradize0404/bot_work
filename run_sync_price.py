"""Запускает полную синхронизацию прайс-листа с исправленным расчётом себестоимости."""
import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
for _s in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_s):
        os.environ[_s] = "stub"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa


async def main():
    from use_cases.outgoing_invoice import sync_price_sheet
    print("Запускаем синхронизацию прайс-листа...")
    result = await sync_price_sheet(days_back=90)
    print(f"Готово: {result}")


asyncio.run(main())
