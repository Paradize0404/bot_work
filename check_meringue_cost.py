"""Проверка себестоимости 'Шу меренга цех'"""
import asyncio
import json
import sys
sys.path.insert(0, '.')

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product, PriceProduct
    from use_cases.outgoing_invoice import calculate_goods_cost_prices, calculate_dish_cost_prices
    
    # ID продукта "Шу меренга цех"
    DISH_ID = "53742a20-5f14-4f36-accf-33773fe8a1d7"
    
    # 1. Считаем GOODS
    print("=" * 70)
    print("1. Расчёт себестоимости GOODS (последний приход)")
    print("=" * 70)
    goods_costs = await calculate_goods_cost_prices(days_back=90)
    print(f"   Товаров с себестоимостью: {len(goods_costs)}")
    
    # 2. Считаем DISH
    print("\n" + "=" * 70)
    print("2. Расчёт себестоимости DISH (по техкартам)")
    print("=" * 70)
    dish_costs = await calculate_dish_cost_prices(goods_costs)
    print(f"   Блюд с себестоимостью: {len(dish_costs)}")
    
    # 3. Проверяем "Шу меренга цех"
    print("\n" + "=" * 70)
    print(f"3. Проверка: Шу меренга цех ({DISH_ID})")
    print("=" * 70)
    
    cost = dish_costs.get(DISH_ID)
    print(f"   Рассчитанная себестоимость: {cost}")
    
    # 4. Смотрим техкарту в JSON
    print("\n" + "=" * 70)
    print("4. Техкарта из JSON")
    print("=" * 70)
    
    with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    for chart in data.get('raw_assembly_charts', []):
        if chart.get('assembledProductId') == DISH_ID:
            print(f"   Chart ID: {chart.get('id')}")
            print(f"   Ингредиенты ({len(chart.get('items', []))} шт):")
            
            total_calc = 0
            for i, item in enumerate(chart.get('items', []), 1):
                ing_id = item.get('productId')
                amount = float(item.get('amountOut', 0))
                ing_cost = goods_costs.get(ing_id)
                line_cost = amount * ing_cost if ing_cost else None
                
                print(f"\n   {i}. Ингредиент: {ing_id}")
                print(f"      amountOut: {amount}")
                print(f"      Цена прихода: {ing_cost}")
                print(f"      Стоимость строки: {line_cost}")
                
                if line_cost:
                    total_calc += line_cost
            
            print(f"\n   ИТОГО: {total_calc:.2f}")
            break
    
    # 5. Проверяем PriceProduct в БД
    print("\n" + "=" * 70)
    print("5. PriceProduct в БД")
    print("=" * 70)
    
    async with get_session() as sess:
        stmt = select(PriceProduct).where(PriceProduct.product_id == DISH_ID)
        result = await sess.execute(stmt)
        pp = result.scalar_one_or_none()
        
        if pp:
            print(f"   product_id: {pp.product_id}")
            print(f"   product_name: {pp.product_name}")
            print(f"   cost_price: {pp.cost_price}")
        else:
            print("   Запись не найдена")

asyncio.run(main())
