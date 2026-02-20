"""Поиск продукта 'п/ф Шу с меренгой' в БД и debug_cost_prices_output.json"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product

    async with get_session() as sess:
        # Ищем по названию
        stmt = select(Product).where(
            Product.name.ilike('%Шу%меренг%')
        )
        result = await sess.execute(stmt)
        products = result.scalars().all()
        
        print("=== Найдено в БД iiko_product ===")
        for p in products:
            print(f"\nID: {p.id}")
            print(f"Name: {p.name}")
            print(f"Type: {p.product_type}")
            print(f"Code: {p.code}")
            print(f"Article: {getattr(p, 'article', 'N/A')}")
        
        # Если не найдено, ищем просто по "меренг"
        if not products:
            print("\n=== Поиск по '%меренг%' ===")
            stmt = select(Product).where(
                Product.name.ilike('%меренг%')
            )
            result = await sess.execute(stmt)
            products = result.scalars().all()
            for p in products[:10]:
                print(f"  {p.id}: {p.name}")

        # Теперь ищем в JSON по UUID
        print("\n=== Поиск в debug_cost_prices_output.json ===")
        with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        for p in products:
            pid = str(p.id)
            print(f"\n--- Поиск для {p.name} (ID: {pid}) ---")
            
            # Ищем в assembly_charts
            for i, chart in enumerate(data.get('raw_assembly_charts', [])):
                if chart.get('assembledProductId') == pid:
                    print(f"  Найдено в raw_assembly_charts[{i}]")
                    print(f"    Chart ID: {chart.get('id')}")
                    print(f"    Items: {len(chart.get('items', []))}")
                    for item in chart.get('items', [])[:5]:
                        print(f"      - {item.get('productName')}: {item.get('amountOut')}")
                
                # Ищем в ингредиентах
                for item in chart.get('items', []):
                    if item.get('productId') == pid:
                        print(f"  Найдено в ингредиентах raw_assembly_charts[{i}]")
                        print(f"    Для блюда: {chart.get('assembledProductId')}")
                        print(f"    Amount: {item.get('amountOut')}")

if __name__ == '__main__':
    asyncio.run(main())
