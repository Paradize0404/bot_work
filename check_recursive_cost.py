"""Проверка: расчёт себестоимости 'Шу меренга цех' с учётом вложенности"""
import asyncio
import json
import sys
from collections import defaultdict
sys.path.insert(0, '.')

async def main():
    from adapters import iiko_api
    from use_cases._helpers import now_kgd
    from datetime import timedelta
    
    # Загружаем JSON
    with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Считаем GOODS costs (последний приход)
    invoices = data.get('raw_incoming_invoices', [])
    
    def _parse_date(inv):
        return inv.get("dateIncoming") or ""
    
    invoices_sorted = sorted(invoices, key=_parse_date)
    
    goods_costs = {}
    for inv in invoices_sorted:
        for item in inv.get("items", []):
            pid = item.get("productId", "").strip()
            price_str = str(item.get("price", "")).strip()
            if not pid or not price_str:
                continue
            try:
                price = float(price_str)
            except ValueError:
                continue
            if price > 0:
                goods_costs[pid] = price
    
    print("=" * 70)
    print("Расчёт себестоимости 'Шу меренга цех' с учётом вложенности")
    print("=" * 70)
    
    # 1. Находим техкарту "Шу меренга цех"
    DISH_ID = "53742a20-5f14-4f36-accf-33773fe8a1d7"
    PREPARED_ID = "cc4b73ec-3395-4767-97a0-3effadb08728"
    
    dish_chart = None
    prepared_chart = None
    
    for chart in data.get('raw_assembly_charts', []):
        if chart.get('assembledProductId') == DISH_ID:
            dish_chart = chart
        if chart.get('assembledProductId') == PREPARED_ID:
            prepared_chart = chart
    
    print("\n1. Техкарта 'Шу меренга цех':")
    print(f"   Ингредиенты: {len(dish_chart.get('items', []))}")
    for item in dish_chart.get('items', []):
        ing_id = item.get('productId')
        amount = item.get('amountOut')
        print(f"   - {ing_id}: {amount}")
    
    print("\n2. Техкарта 'п/ф Шу с меренгой':")
    print(f"   Ингредиенты: {len(prepared_chart.get('items', []))}")
    
    # Рекурсивный расчёт себестоимости п/ф
    prepared_cost = 0
    print("\n   Расчёт себестоимости п/ф:")
    for item in prepared_chart.get('items', []):
        ing_id = item.get('productId')
        amount = float(item.get('amountOut', 0))
        ing_cost = goods_costs.get(ing_id)
        line_cost = amount * ing_cost if ing_cost else None
        
        print(f"   - {ing_id}: {amount} x {ing_cost} = {line_cost}")
        
        if line_cost:
            prepared_cost += line_cost
    
    print(f"\n   Итого п/ф 'Шу с меренгой': {prepared_cost:.2f}")
    
    # Расчёт себестоимости блюда
    print("\n3. Расчёт себестоимости 'Шу меренга цех':")
    dish_cost = 0
    for item in dish_chart.get('items', []):
        ing_id = item.get('productId')
        amount = float(item.get('amountOut', 0))
        
        if ing_id == PREPARED_ID:
            # Используем рассчитанную себестоимость п/ф
            ing_cost = prepared_cost
        else:
            ing_cost = goods_costs.get(ing_id)
        
        line_cost = amount * ing_cost if ing_cost else None
        print(f"   - {ing_id}: {amount} x {ing_cost} = {line_cost}")
        
        if line_cost:
            dish_cost += line_cost
    
    print(f"\n   Итого 'Шу меренга цех': {dish_cost:.2f}")
    
    # Сравниваем с iiko (42.98)
    print("\n" + "=" * 70)
    print("Сравнение:")
    print("=" * 70)
    print(f"   Наш расчёт: {dish_cost:.2f}")
    print(f"   iiko (скриншот): 42.98")
    print(f"   Разница: {abs(dish_cost - 42.98):.2f}")

asyncio.run(main())
