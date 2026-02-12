"""Debug: compare ingredient IDs from prepared charts vs goods_costs."""
import asyncio, sys, time
sys.path.insert(0, ".")


async def main():
    from use_cases.outgoing_invoice import sync_price_sheet
    result = await sync_price_sheet(days_back=90)
    print(f"\nResult: {result}")

asyncio.run(main())
