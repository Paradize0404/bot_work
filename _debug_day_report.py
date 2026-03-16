"""Debug: dump raw OLAP rows for yesterday to see Department names and amounts."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


async def main():
    from adapters.iiko_api import fetch_olap_by_preset
    from use_cases.day_report import SALES_PRESET
    from use_cases._helpers import now_kgd
    from datetime import timedelta

    now = now_kgd()
    # Вчерашний день
    yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    date_from = yesterday.strftime("%Y-%m-%dT%H:%M:%S")
    date_to = (yesterday + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

    print(f"=== OLAP запрос: {date_from} → {date_to} ===\n")

    rows = await fetch_olap_by_preset(SALES_PRESET, date_from, date_to)
    print(f"Всего строк: {len(rows)}\n")

    # Уникальные Department
    depts = set()
    for r in rows:
        d = r.get("Department", "")
        if d:
            depts.add(d)

    print("=== Уникальные Department ===")
    for d in sorted(depts):
        print(f"  {d!r}")

    # Фильтруем по гайдара (case-insensitive substring)
    print("\n=== Строки содержащие 'гайдара' в Department ===")
    gaidara_rows = [r for r in rows if "гайдара" in r.get("Department", "").lower()]
    print(f"Строк: {len(gaidara_rows)}\n")

    total_sales = 0.0
    for r in gaidara_rows:
        dept = r.get("Department", "")
        pay = r.get("PayTypes", "")
        place = r.get("CookingPlaceType", "")
        amount = r.get("DishDiscountSumInt", 0) or 0
        cost = r.get("ProductCostBase.ProductCost", 0) or 0
        print(
            f"  Dept={dept!r}  PayType={pay!r}  Place={place!r}  Amount={amount}  Cost={cost}"
        )
        if pay:
            total_sales += amount

    print(f"\n=== Итого продаж (PayTypes) для Гайдара: {total_sales:.2f} ===")

    # Также покажем ВСЕ строки без фильтра Department для понимания масштаба
    all_sales = 0.0
    for r in rows:
        pay = r.get("PayTypes", "")
        if pay:
            all_sales += r.get("DishDiscountSumInt", 0) or 0
    print(f"=== Итого продаж ВСЕ подразделения: {all_sales:.2f} ===")

    # Также: что department_id отдаёт для Гайдара
    from db.engine import async_session_factory
    from sqlalchemy import select, text
    from db.models import Department

    async with async_session_factory() as session:
        stmt = select(Department).where(Department.name.ilike("%гайдара%"))
        res = await session.execute(stmt)
        deps = res.scalars().all()
        print("\n=== Подразделения в БД с 'Гайдара' ===")
        for d in deps:
            print(f"  id={d.id}  name={d.name!r}  type={d.department_type}")


asyncio.run(main())
