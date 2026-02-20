"""
OLAP отчёт «Остатки складов бот» (reportId: cd6829b0-39f6-47b4-a040-986b9fefc46c)
POST /resto/api/v2/reports/olap
"""
import asyncio, os, sys, json, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
for _stub in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_stub):
        os.environ[_stub] = "stub"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa


REPORT_ID = "cd6829b0-39f6-47b4-a040-986b9fefc46c"

BODY = {
    "id": REPORT_ID,
    "reportType": "TRANSACTIONS",
    "groupByRowFields": ["Account.Name", "Product.Name"],
    "groupByColFields": [],
    "aggregateFields": ["FinalBalance.Money", "FinalBalance.Amount"],
    "filters": {
        "DateTime.OperDayFilter": {
            "filterType": "DateRange",
            "periodType": "CUSTOM",
            "from": "2026-02-01T00:00:00",
            "to": "2026-03-01T00:00:00",
            "includeLow": True,
            "includeHigh": False,
        },
        "Department": {
            "filterType": "IncludeValues",
            "values": ["PizzaYolo / Пицца Йоло (Московский)"],
        },
        "Account.Type": {
            "filterType": "IncludeValues",
            "values": ["INVENTORY_ASSETS"],
        },
    },
}


async def main():
    from iiko_auth import get_auth_token, get_base_url
    import httpx
    from config import IIKO_VERIFY_SSL

    key = await get_auth_token()
    base = get_base_url()
    url  = f"{base}/resto/api/v2/reports/olap"

    print(f"POST {url}")
    print("Body:", json.dumps(BODY, ensure_ascii=False, indent=2))

    t0 = time.monotonic()
    async with httpx.AsyncClient(verify=IIKO_VERIFY_SSL, timeout=60) as client:
        resp = await client.post(url, params={"key": key}, json=BODY)
    elapsed = time.monotonic() - t0

    print(f"\nHTTP {resp.status_code}  {elapsed:.1f}s  {len(resp.content)} bytes")

    if resp.status_code >= 400:
        print("ERROR body:", resp.text[:2000])
        return

    data = resp.json()

    out_path = Path(__file__).resolve().parent / "olap_balances_dump.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Сохранено → {out_path}  ({len(resp.content)} байт)")


if __name__ == "__main__":
    asyncio.run(main())
