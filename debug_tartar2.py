"""Debug why dish_costs returns 2160 for "Говяжья вырезка на тар тар цех"."""
import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
for s in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(s):
        os.environ[s] = "stub"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa

DISH_UUID = "e4356263-ffa6-49ee-8fac-202618b0f234"
GOODS_UUID = "ad8bc83b-e43b-4875-8cdf-6d6f8d3f3729"


async def main():
    from adapters.iiko_api import fetch_assembly_charts, fetch_stock_balances

    # 1. Assembly chart
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    charts = await fetch_assembly_charts(today, today)
    target = [
        c for c in charts.get("assemblyCharts", [])
        if (c.get("assembledProductId") or "").lower() == DISH_UUID
    ]
    print("=== assemblyCharts for Говяжья вырезка ===")
    print(f"Найдено техкарт: {len(target)}")
    if target:
        ch = target[0]
        print(f"assembledProductId: {ch.get('assembledProductId')}")
        items = ch.get("items") or []
        print(f"Количество ингредиентов: {len(items)}")
        for it in items:
            pid = it.get("productId", "")
            amt = it.get("amount")
            amt_out = it.get("amountOut")
            name = it.get("name", "")
            print(f"  pid={pid}")
            print(f"    amount={amt}  amountOut={amt_out}  name={name}")

    # preparedCharts
    prep_target = [
        c for c in charts.get("preparedCharts", [])
        if (c.get("assembledProductId") or "").lower() == DISH_UUID
    ]
    print(f"\nНайдено в preparedCharts: {len(prep_target)}")
    for ch in prep_target:
        print(f"  assembledProductId: {ch.get('assembledProductId')}")

    # 2. Stock balances
    balances = await fetch_stock_balances()
    print("\n=== Stock balances ===")
    dish_balances = [b for b in balances if (b.get("product") or "").lower() == DISH_UUID]
    goods_balances = [b for b in balances if (b.get("product") or "").lower() == GOODS_UUID]
    print(f"DISH {DISH_UUID}: {dish_balances}")
    print(f"GOODS {GOODS_UUID}: {goods_balances}")

    # 3. Run goods cost prices to see what it gives
    from use_cases.outgoing_invoice import calculate_goods_cost_prices
    goods_costs = await calculate_goods_cost_prices(days_back=90)
    print("\n=== goods_costs ===")
    print(f"goods_costs[{DISH_UUID}] = {goods_costs.get(DISH_UUID)}")
    print(f"goods_costs[{GOODS_UUID}] = {goods_costs.get(GOODS_UUID)}")

    # 4. Run dish cost prices with verbose trace
    from use_cases.outgoing_invoice import calculate_dish_cost_prices
    dish_costs = await calculate_dish_cost_prices(goods_costs)
    print("\n=== dish_costs ===")
    print(f"dish_costs[{DISH_UUID}] = {dish_costs.get(DISH_UUID)}")

    # 5. Explore what's in assemblyCharts for this DISH manually
    print("\n=== Manual calculation trace ===")
    if target:
        ch = target[0]
        items = ch.get("items") or []
        total = 0.0
        for it in items:
            pid = (it.get("productId") or "").lower()
            amt_out = it.get("amountOut") or it.get("amount") or 0
            unit_cost = goods_costs.get(pid)
            contrib = (unit_cost or 0) * amt_out
            print(f"  ingredient pid={pid} amountOut={amt_out} unit_cost={unit_cost} -> contrib={contrib:.4f}")
            total += contrib
        print(f"  TOTAL = {total:.4f}")


asyncio.run(main())
