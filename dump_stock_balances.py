"""
Получить актуальные остатки по всем складам из iiko API и сохранить в JSON.
Запуск:  python dump_stock_balances.py
Результат: stock_balances_dump.json
"""
import asyncio, os, sys, json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
for _stub in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_stub):
        os.environ[_stub] = "stub"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa


async def main():
    from adapters.iiko_api import fetch_stock_balances
    from db.engine import async_session_factory
    from db.models import Store, Product
    from sqlalchemy import select

    print("1. Загружаю справочники складов и товаров из БД...")
    async with async_session_factory() as session:
        store_rows = (await session.execute(select(Store.id, Store.name))).all()
        product_rows = (await session.execute(select(Product.id, Product.name))).all()

    store_map   = {str(r.id).lower(): r.name for r in store_rows}
    product_map = {str(r.id).lower(): r.name for r in product_rows}
    print(f"   Складов в БД: {len(store_map)}, товаров: {len(product_map)}")

    print("2. Запрашиваю актуальные остатки из iiko API...")
    raw = await fetch_stock_balances()  # timestamp=None → текущий момент
    print(f"   Получено строк из API: {len(raw)}")

    print("3. Обогащаю данные названиями...")
    enriched = []
    for item in raw:
        store_id   = (item.get("store")   or "").lower()
        product_id = (item.get("product") or "").lower()
        amount     = item.get("amount")
        money      = item.get("sum")

        enriched.append({
            "store_id":    store_id,
            "store_name":  store_map.get(store_id, f"unknown:{store_id}"),
            "product_id":  product_id,
            "product_name": product_map.get(product_id, f"unknown:{product_id}"),
            "amount":      amount,
            "sum":         money,
        })

    # Сортировка: по складу, потом по товару
    enriched.sort(key=lambda x: (x["store_name"], x["product_name"]))

    out_path = Path(__file__).resolve().parent / "stock_balances_dump.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)

    print(f"4. Готово! Сохранено {len(enriched)} строк → {out_path}")

    # Краткая статистика по складам
    from collections import Counter
    counts = Counter(r["store_name"] for r in enriched)
    print("\nСтрок по складам:")
    for name, cnt in sorted(counts.items()):
        print(f"   {cnt:4d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
