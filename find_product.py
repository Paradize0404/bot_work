"""Полная информация по техкарте 'п/ф Шу с меренгой'"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product

    # ID техкарты из БД
    PREPARED_ID = "cc4b73ec-3395-4767-97a0-3effadb08728"  # п/ф Шу с меренгой
    
    async with get_session() as sess:
        # Получаем названия всех продуктов
        stmt = select(Product)
        result = await sess.execute(stmt)
        all_products = result.scalars().all()
        
        # Создаём словарь для быстрого поиска
        product_names = {str(p.id): p.name for p in all_products}
        
        # Загружаем JSON
        with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Находим техкарту
        target_chart = None
        for i, chart in enumerate(data.get('raw_assembly_charts', [])):
            if chart.get('assembledProductId') == PREPARED_ID:
                target_chart = chart
                print(f"\n{'='*60}")
                print(f"ТЕХКАРТА: п/ф Шу с меренгой")
                print(f"{'='*60}")
                print(f"ID техкарты: {chart.get('id')}")
                print(f"ID продукта: {chart.get('assembledProductId')}")
                print(f"Дата вступления: {chart.get('dateFrom')}")
                print(f"Дата окончания: {chart.get('dateTo') or 'бессрочно'}")
                print(f"\nИНГРЕДИЕНТЫ ({len(chart.get('items', []))} шт):")
                print("-" * 60)
                
                for idx, item in enumerate(chart.get('items', []), 1):
                    prod_id = item.get('productId')
                    prod_name = product_names.get(prod_id, f"Неизвестно ({prod_id})")
                    print(f"\n{idx}. {prod_name}")
                    print(f"   ID: {prod_id}")
                    print(f"   amountIn: {item.get('amountIn')}")
                    print(f"   amountOut: {item.get('amountOut')}")
                
                break
        
        if not target_chart:
            print("Техкарта не найдена!")
            return
        
        # Теперь ищем себестоимость в dish_cost_detail
        print(f"\n{'='*60}")
        print("СЕБЕСТОИМОСТЬ ИЗ dish_cost_detail")
        print(f"{'='*60}")
        
        for dish in data.get('dish_cost_detail', []):
            if dish.get('dish_id') == PREPARED_ID:
                print(f"\nБлюдо: {dish.get('dish_name')}")
                print(f"ID: {dish.get('dish_id')}")
                print(f"Расчётная себестоимость: {dish.get('calculated_cost')} руб.")
                print(f"Кол-во ингредиентов: {dish.get('ingredients_count')}")
                
                print(f"\nДетализация по ингредиентам:")
                for ingr in dish.get('ingredients', []):
                    print(f"\n  - {ingr.get('ingredient_name')}")
                    print(f"    ID: {ingr.get('ingredient_id')}")
                    print(f"    Количество: {ingr.get('amount')} {ingr.get('unit')}")
                    print(f"    Цена за ед: {ingr.get('cost_per_unit')}")
                    print(f"    Стоимость строки: {ingr.get('line_cost')}")
                
                break
        
        # Итоговая сводка
        print(f"\n{'='*60}")
        print("ИТОГОВАЯ СЕБЕСТОИМОСТЬ (dish_costs_summary)")
        print(f"{'='*60}")
        summary = data.get('dish_costs_summary', {})
        cost = summary.get(PREPARED_ID)
        print(f"\nп/ф Шу с меренгой: {cost} руб." if cost else "Себестоимость не рассчитана")

if __name__ == '__main__':
    asyncio.run(main())
