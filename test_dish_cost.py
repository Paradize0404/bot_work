"""Проверка себестоимости для 'п/ф Шу с меренгой' в полном цикле"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product, PriceProduct
    from use_cases.outgoing_invoice import calculate_goods_cost_prices, calculate_dish_cost_prices

    # ID продукта
    PREPARED_ID = "cc4b73ec-3395-4767-97a0-3effadb08728"  # п/ф Шу с меренгой
    
    # 1. Считаем себестоимость GOODS
    print("=" * 60)
    print("1. Расчёт себестоимости GOODS (последний приход)")
    print("=" * 60)
    goods_costs = await calculate_goods_cost_prices(days_back=90)
    print(f"   Всего товаров с себестоимостью: {len(goods_costs)}")
    
    # 2. Считаем себестоимость DISH
    print("\n" + "=" * 60)
    print("2. Расчёт себестоимости DISH (по техкартам)")
    print("=" * 60)
    dish_costs = await calculate_dish_cost_prices(goods_costs)
    print(f"   Всего блюд с себестоимостью: {len(dish_costs)}")
    
    # 3. Проверяем наш продукт
    print("\n" + "=" * 60)
    print(f"3. Проверка: п/ф Шу с меренгой ({PREPARED_ID})")
    print("=" * 60)
    
    combined_costs = {**goods_costs, **dish_costs}
    cost = combined_costs.get(PREPARED_ID)
    print(f"   Себестоимость из расчёта: {cost}")
    
    if cost is None:
        print("   [X] Себестоимость НЕ рассчитана!")
        print("   Причина: у ингредиентов нет цен в приходах за 90 дней")
        
        # Проверяем ингредиенты
        with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        for chart in data.get('raw_assembly_charts', []):
            if chart.get('assembledProductId') == PREPARED_ID:
                print(f"\n   Ингредиенты техкарты:")
                for item in chart.get('items', []):
                    ing_id = item.get('productId')
                    ing_cost = goods_costs.get(ing_id)
                    print(f"     - {ing_id}: {item.get('amountOut')} × {ing_cost} = {item.get('amountOut') * ing_cost if ing_cost else None}")
                break
    else:
        print(f"   [+] Себестоимость рассчитана: {cost:.2f} руб.")
    
    # 4. Проверяем, что записано в БД
    print("\n" + "=" * 60)
    print("4. Проверка PriceProduct в БД")
    print("=" * 60)
    
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
            print("   [X] Запись в PriceProduct не найдена!")
            print("   Нужно запустить синхронизацию прайс-листа")

if __name__ == '__main__':
    asyncio.run(main())
