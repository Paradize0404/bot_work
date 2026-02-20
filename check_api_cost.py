"""Сравнение себестоимостей из iiko API и нашего расчёта"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from adapters import iiko_api
    from use_cases._helpers import now_kgd
    from datetime import timedelta
    
    today = now_kgd()
    date_from = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    
    # 1. Получаем техкарты напрямую из API
    print("=" * 70)
    print("1. Запрос техкарт из iiko API")
    print("=" * 70)
    
    chart_data = await iiko_api.fetch_assembly_charts(date_to, date_to, include_prepared=True)
    
    # Проверяем полную структуру ответа
    print(f"\nКлючи в ответе API: {list(chart_data.keys())}")
    
    # Проверяем assemblyCharts
    assembly = chart_data.get('assemblyCharts', [])
    print(f"assemblyCharts: {len(assembly)} шт.")
    
    # Проверяем preparedCharts
    prepared = chart_data.get('preparedCharts', [])
    print(f"preparedCharts: {len(prepared)} шт.")
    
    # Смотрим полную структуру первой техкарты с ингредиентами
    print("\n" + "=" * 70)
    print("2. Структура первой техкарты с ингредиентами")
    print("=" * 70)
    
    for i, chart in enumerate(assembly[:20]):
        if chart.get('items'):
            print(f"\nТехкарта #{i}:")
            print(f"  assembledProductId: {chart.get('assembledProductId')}")
            print(f"  assembledProductName: {chart.get('assembledProductName')}")
            print(f"  name: {chart.get('name')}")
            print(f"  assembledAmount: {chart.get('assembledAmount')}")
            
            # Все поля техкарты
            all_keys = list(chart.keys())
            print(f"  Все поля: {all_keys}")
            
            # Проверяем поля с cost/price
            for key in all_keys:
                val = chart.get(key)
                if val and any(x in str(key).lower() for x in ['cost', 'price', 'sum']):
                    print(f"  >>> {key}: {val}")
            
            # Ингредиенты
            items = chart.get('items', [])
            print(f"\n  Ингредиенты ({len(items)} шт.):")
            for j, item in enumerate(items[:5]):
                print(f"    [{j}] {item.get('productName', 'N/A')}")
                print(f"        productId: {item.get('productId')}")
                print(f"        amountOut: {item.get('amountOut')}")
                
                # Все поля ингредиента
                item_keys = list(item.keys())
                for key in item_keys:
                    val = item.get(key)
                    if val and any(x in str(key).lower() for x in ['cost', 'price', 'sum']):
                        print(f"        >>> {key}: {val}")
            
            break
    
    # 2. Проверяем приходные накладные
    print("\n" + "=" * 70)
    print("3. Проверка приходных накладных")
    print("=" * 70)
    
    invoices = await iiko_api.fetch_incoming_invoices(date_from, date_to)
    print(f"Найдено накладных: {len(invoices)}")
    
    # Считаем goods_costs как в коде
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
    
    print(f"Товаров с ценами: {len(goods_costs)}")
    
    # Проверяем первые 10 цен
    print("\nПримеры цен прихода:")
    for pid, price in list(goods_costs.items())[:10]:
        print(f"  {pid}: {price:.2f}")
    
    # 3. Считаем себестоимость блюда вручную
    print("\n" + "=" * 70)
    print("4. Расчёт себестоимости первого блюда")
    print("=" * 70)
    
    for chart in assembly[:20]:
        if chart.get('items'):
            dish_id = chart.get('assembledProductId')
            dish_name = chart.get('assembledProductName') or chart.get('name', 'N/A')
            
            total_cost = 0.0
            missing = []
            
            for item in chart.get('items', []):
                ing_id = item.get('productId')
                amount = float(item.get('amountOut', 0))
                ing_cost = goods_costs.get(ing_id)
                
                if ing_cost:
                    total_cost += amount * ing_cost
                else:
                    missing.append(ing_id)
            
            print(f"\nБлюдо: {dish_name}")
            print(f"ID: {dish_id}")
            print(f"Рассчитанная себестоимость: {total_cost:.2f}")
            print(f"Ингредиентов всего: {len(chart.get('items', []))}")
            print(f"Ингредиентов с ценами: {len(chart.get('items', [])) - len(missing)}")
            print(f"Ингредиентов без цен: {len(missing)}")
            
            if missing:
                print(f"Без цен: {missing[:5]}...")
            
            break

if __name__ == '__main__':
    asyncio.run(main())
