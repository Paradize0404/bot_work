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

# Проверочные UUID (в разных регистрах — как они приходят из API)
CHECK_NAMES = ["говяж", "гренки на цезарь", "шу меренга"]


async def main():
    from use_cases.outgoing_invoice import (
        calculate_goods_cost_prices,
        calculate_dish_cost_prices,
        sync_price_sheet,
    )

    print("=== Проверка расчёта себестоимости ===")

    goods_costs = await calculate_goods_cost_prices(days_back=90)
    print(f"\nGOODS цен рассчитано: {len(goods_costs)}")

    dish_costs = await calculate_dish_cost_prices(goods_costs)
    print(f"DISH себестоимостей рассчитано: {len(dish_costs)}")

    # Дебаг: показать конкретные блюда
    from db.engine import get_session
    from db.models import Product
    from sqlalchemy import select
    async with get_session() as session:
        rows = await session.execute(select(Product))
        products = {str(p.id).lower(): p for p in rows.scalars().all()}

    print("\n=== Целевые блюда ===")
    for uid, p in products.items():
        for hint in CHECK_NAMES:
            if hint in (p.name or "").lower():
                cost = dish_costs.get(uid) or goods_costs.get(uid)
                print(f"  [{p.product_type}] {p.name}")
                print(f"    uuid={uid}")
                print(f"    себестоимость = {cost}")
                # Проверим что ключ реально есть в dish_costs
                print(f"    в dish_costs: {uid in dish_costs}")
                print(f"    в goods_costs: {uid in goods_costs}")
                break

    print("\n=== Синхронизация в Google Sheet ===")
    result = await sync_price_sheet(days_back=90)
    print(f"Готово: {result}")


asyncio.run(main())
