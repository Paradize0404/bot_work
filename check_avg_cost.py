"""Проверка: как iiko считает себестоимость - по средней или по последней цене"""
import asyncio
import json
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from adapters import iiko_api
    from use_cases._helpers import now_kgd
    from datetime import timedelta
    
    today = now_kgd()
    date_from = (today - timedelta(days=365)).strftime("%Y-%m-%d")  # Берём год для статистики
    date_to = today.strftime("%Y-%m-%d")
    
    # 1. Получаем все приходные накладные за год
    print("=" * 70)
    print("Анализ цен приходов для расчёта себестоимости")
    print("=" * 70)
    
    invoices = await iiko_api.fetch_incoming_invoices(date_from, date_to)
    print(f"Найдено накладных: {len(invoices)}")
    
    # Собираем все цены по каждому продукту
    product_prices = defaultdict(list)
    for inv in invoices:
        inv_date = inv.get("dateIncoming", "")[:10]
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
                product_prices[pid].append({
                    'date': inv_date,
                    'price': price,
                    'amount': float(item.get('amount', 0)),
                })
    
    print(f"Продуктов с приходами: {len(product_prices)}")
    
    # Анализируем несколько продуктов
    print("\n" + "=" * 70)
    print("Статистика цен по продуктам (последние 5 приходов)")
    print("=" * 70)
    
    for pid, prices in list(product_prices.items())[:5]:
        # Сортируем по дате
        prices_sorted = sorted(prices, key=lambda x: x['date'])
        last_price = prices_sorted[-1]['price'] if prices_sorted else None
        
        # Считаем среднюю взвешенную
        total_amount = sum(p['amount'] for p in prices_sorted)
        weighted_avg = sum(p['amount'] * p['price'] for p in prices_sorted) / total_amount if total_amount > 0 else 0
        
        print(f"\nПродукт: {pid}")
        print(f"  Всего приходов: {len(prices_sorted)}")
        print(f"  Последняя цена: {last_price}")
        print(f"  Средняя взвешенная: {weighted_avg:.2f}")
        avg_simple = sum(p['price'] for p in prices_sorted[-5:])/min(5,len(prices_sorted))
        print(f"  Простая средняя: {avg_simple:.2f}")
        
        # Последние 5 приходов
        print("  Последние приходы:")
        for p in prices_sorted[-5:]:
            print(f"    {p['date']}: {p['amount']} x {p['price']:.2f}")
    
    # 2. Получаем техкарты и считаем себестоимость разными методами
    print("\n" + "=" * 70)
    print("Сравнение методов расчёта себестоимости блюд")
    print("=" * 70)
    
    chart_data = await iiko_api.fetch_assembly_charts(date_to, date_to, include_prepared=True)
    assembly = chart_data.get('assemblyCharts', [])
    
    # Метод 1: последняя цена
    last_price_map = {pid: prices[-1]['price'] for pid, prices in product_prices.items()}
    
    # Метод 2: средняя взвешенная
    weighted_avg_map = {}
    for pid, prices in product_prices.items():
        total_amount = sum(p['amount'] for p in prices)
        if total_amount > 0:
            weighted_avg_map[pid] = sum(p['amount'] * p['price'] for p in prices) / total_amount
    
    # Считаем для первых 5 блюд
    for chart in assembly[:5]:
        if not chart.get('items'):
            continue
            
        dish_id = chart.get('assembledProductId')
        items = chart.get('items', [])
        
        # Последняя цена
        cost_last = 0
        for item in items:
            ing_id = item.get('productId')
            amount = float(item.get('amountOut', 0))
            ing_cost = last_price_map.get(ing_id)
            if ing_cost:
                cost_last += amount * ing_cost
        
        # Средняя взвешенная
        cost_weighted = 0
        for item in items:
            ing_id = item.get('productId')
            amount = float(item.get('amountOut', 0))
            ing_cost = weighted_avg_map.get(ing_id)
            if ing_cost:
                cost_weighted += amount * ing_cost
        
        print(f"\nБлюдо: {dish_id}")
        print(f"  Ингредиентов: {len(items)}")
        print(f"  Себестоимость (последняя цена): {cost_last:.2f}")
        print(f"  Себестоимость (средняя взвешенная): {cost_weighted:.2f}")
        diff = abs(cost_last - cost_weighted)
        pct = diff/max(cost_last,cost_weighted)*100 if max(cost_last,cost_weighted) > 0 else 0
        print(f"  Разница: {diff:.2f} ({pct:.1f}%)")

if __name__ == '__main__':
    asyncio.run(main())
