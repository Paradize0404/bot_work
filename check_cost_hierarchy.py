"""Проверка: откуда берётся 48.67 для 'Шу меренга цех'"""
import asyncio
import json
import sys
sys.path.insert(0, '.')

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product, PriceProduct
    
    DISH_ID = "53742a20-5f14-4f36-accf-33773fe8a1d7"
    
    # 1. Проверяем PriceProduct
    print("=" * 70)
    print("1. PriceProduct в БД")
    print("=" * 70)
    
    async with get_session() as sess:
        stmt = select(PriceProduct).where(PriceProduct.product_id == DISH_ID)
        result = await sess.execute(stmt)
        pp = result.scalar_one_or_none()
        
        if pp:
            print(f"   product_id: {pp.product_id}")
            print(f"   product_name: {pp.product_name}")
            print(f"   product_type: {pp.product_type}")
            print(f"   cost_price: {pp.cost_price}")
            print(f"   synced_at: {pp.synced_at}")
        else:
            print("   Запись не найдена")
    
    # 2. Проверяем иерархию техкарт
    print("\n" + "=" * 70)
    print("2. Иерархия техкарт")
    print("=" * 70)
    
    with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Находим все техкарты, где участвует наш продукт
    print("\nГде используется 'Шу меренга цех' (как ингредиент):")
    for i, chart in enumerate(data.get('raw_assembly_charts', [])):
        for item in chart.get('items', []):
            if item.get('productId') == DISH_ID:
                print(f"  [{i}] {chart.get('assembledProductId')}: {item.get('amountOut')}")
    
    # 3. Проверяем п/ф "Шу с меренгой"
    print("\n" + "=" * 70)
    print("3. п/ф Шу с меренгой (cc4b73ec...) - техкарта")
    print("=" * 70)
    
    PREPARED_ID = "cc4b73ec-3395-4767-97a0-3effadb08728"
    
    for chart in data.get('raw_assembly_charts', []):
        if chart.get('assembledProductId') == PREPARED_ID:
            print(f"   Chart ID: {chart.get('id')}")
            items = chart.get('items', [])
            print(f"   Ингредиенты: {len(items)}")
            
            # Рекурсивно проверяем ингредиенты
            for j, item in enumerate(items, 1):
                ing_id = item.get('productId')
                amount = item.get('amountOut')
                print(f"\n   {j}. {ing_id} (amount: {amount})")
                
                # Проверяем, есть ли у этого ингредиента своя техкарта
                for sub_chart in data.get('raw_assembly_charts', []):
                    if sub_chart.get('assembledProductId') == ing_id:
                        sub_items = sub_chart.get('items', [])
                        print(f"       -> Имеет техкарту с {len(sub_items)} ингр.")
                        for k, sub_item in enumerate(sub_items, 1):
                            sub_ing_id = sub_item.get('productId')
                            sub_amount = sub_item.get('amountOut')
                            print(f"          {k}. {sub_ing_id}: {sub_amount}")
                        break
    
    # 4. Проверяем, есть ли в БД себестоимость у п/ф
    print("\n" + "=" * 70)
    print("4. п/ф Шу с меренгой в PriceProduct")
    print("=" * 70)
    
    async with get_session() as sess:
        stmt = select(PriceProduct).where(PriceProduct.product_id == PREPARED_ID)
        result = await sess.execute(stmt)
        pp = result.scalar_one_or_none()
        
        if pp:
            print(f"   product_id: {pp.product_id}")
            print(f"   product_name: {pp.product_name}")
            print(f"   product_type: {pp.product_type}")
            print(f"   cost_price: {pp.cost_price}")
        else:
            print("   Запись не найдена (PREPARED не синхронизируется в прайс-лист)")

asyncio.run(main())
