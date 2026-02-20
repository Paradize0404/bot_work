"""Проверка: есть ли у ингредиентов п/ф свои техкарты и приходы"""
import json

with open('debug_cost_prices_output.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# Считаем GOODS costs
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
print("Проверка ингредиентов п/ф 'Шу с меренгой'")
print("=" * 70)

# Ингредиенты п/ф
ingredients = [
    ("704afeb4-d149-4c88-bc71-96d4562552cf", "п/ф Шу пустой ванильный"),
    ("a800bb67-b27f-4b27-a59f-7c0de4fd747d", "п/ф Лимонный курд"),
    ("60177252-8aad-4127-b9a4-7ec6eca39a4e", "п/ф Меренга итальянская"),
]

for ing_id, ing_name in ingredients:
    print(f"\n{ing_name} ({ing_id}):")
    
    # 1. Есть ли цена прихода?
    cost = goods_costs.get(ing_id)
    print(f"  Цена прихода: {cost}")
    
    # 2. Есть ли техкарта?
    chart = None
    for c in data.get('raw_assembly_charts', []):
        if c.get('assembledProductId') == ing_id:
            chart = c
            break
    
    if chart:
        items = chart.get('items', [])
        print(f"  Техкарта: есть, {len(items)} ингредиентов")
        
        # Проверяем ингредиенты этой техкарты
        for i, item in enumerate(items, 1):
            sub_id = item.get('productId')
            sub_amount = item.get('amountOut')
            sub_cost = goods_costs.get(sub_id)
            print(f"    {i}. {sub_id}: {sub_amount} x {sub_cost} = {sub_amount * sub_cost if sub_cost else None}")
    else:
        print(f"  Техкарта: нет")

# 3. Ищем в БД названия этих продуктов
print("\n" + "=" * 70)
print("Названия из БД:")
print("=" * 70)

import asyncio
import sys
sys.path.insert(0, '.')

async def check_db():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product
    
    async with get_session() as sess:
        for ing_id, _ in ingredients:
            stmt = select(Product).where(Product.id == ing_id)
            result = await sess.execute(stmt)
            p = result.scalar_one_or_none()
            if p:
                print(f"  {ing_id}: {p.name} ({p.product_type})")
            else:
                print(f"  {ing_id}: не найдено")

asyncio.run(check_db())
