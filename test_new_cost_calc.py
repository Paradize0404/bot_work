import asyncio
import sys
sys.path.insert(0, '.')

async def test():
    from use_cases.outgoing_invoice import calculate_goods_cost_prices, calculate_dish_cost_prices
    
    goods = await calculate_goods_cost_prices(90)
    print(f'GOODS: {len(goods)} товаров с себестоимостью')
    
    dish = await calculate_dish_cost_prices(goods)
    print(f'DISH: {len(dish)} блюд с себестоимостью')
    
    # Проверяем "Шу меренга цех"
    meringue_cost = dish.get("53742a20-5f14-4f36-accf-33773fe8a1d7")
    print(f'Шу меренга цех: {meringue_cost}')
    
    # Проверяем п/ф "Шу с меренгой" (должен быть рассчитан, но не возвращён)
    prepared_cost = dish.get("cc4b73ec-3395-4767-97a0-3effadb08728")
    print(f'п/ф Шу с меренгой (в dish_costs): {prepared_cost}')

asyncio.run(test())
