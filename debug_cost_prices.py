"""
debug_cost_prices.py
====================
Диагностический скрипт:
  1. Вытаскивает техкарты (preparedCharts) из iiko через
     GET /resto/api/v2/assemblyCharts/getAll
  2. Вытаскивает себестоимость GOODS по приходным накладным за последние 90 дней
  3. Показывает, как получается себестоимость каждого блюда (DISH)
  4. Сохраняет всё в debug_cost_prices_output.json

Запуск:
    python debug_cost_prices.py
"""

import asyncio
import json
import time
from datetime import timedelta
import sys
import os

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: F401  — инициализирует переменные окружения / настройки


async def main() -> None:
    from adapters import iiko_api
    from use_cases._helpers import now_kgd

    today = now_kgd()
    date_today = today.strftime("%Y-%m-%d")
    date_from_goods = (today - timedelta(days=90)).strftime("%Y-%m-%d")

    output: dict = {}

    # ──────────────────────────────────────────────────
    # 1. Сырые техкарты из iiko
    # ──────────────────────────────────────────────────
    print(f"[1/3] Запрашиваю техкарты (дата={date_today})...")
    t0 = time.monotonic()
    chart_data = await iiko_api.fetch_assembly_charts(date_today, date_today, include_prepared=True)
    print(f"      Получено за {time.monotonic() - t0:.2f} сек")

    assembly_charts  = chart_data.get("assemblyCharts") or []   # ← техкарты (assemblyCharts, не preparedCharts!)
    prepared_charts  = chart_data.get("preparedCharts") or []    # п/ф (обычно пустой)

    output["raw_assembly_charts"]  = assembly_charts   # обычные техкарты
    output["raw_prepared_charts"]  = prepared_charts   # п/ф техкарты (includePreparedCharts=true)

    print(f"      assemblyCharts : {len(assembly_charts)}")
    print(f"      preparedCharts : {len(prepared_charts)}")

    # ──────────────────────────────────────────────────
    # 2. Себестоимость GOODS (последний приход за 90 дней)
    # ──────────────────────────────────────────────────
    print(f"\n[2/3] Запрашиваю приходные накладные ({date_from_goods} .. {date_today})...")
    t0 = time.monotonic()
    invoices = await iiko_api.fetch_incoming_invoices(date_from_goods, date_today)
    print(f"      Получено {len(invoices)} накладных за {time.monotonic() - t0:.2f} сек")

    output["raw_incoming_invoices"] = invoices  # все сырые приходные накладные

    # Повторяем ту же логику, что в calculate_goods_cost_prices
    def _parse_date(inv: dict) -> str:
        return (inv.get("dateIncoming") or "")[:10]

    invoices_sorted = sorted(invoices, key=_parse_date)

    goods_costs: dict[str, float] = {}
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

    output["goods_costs"] = goods_costs  # {product_id: последняя_цена_прихода}
    print(f"      goods_costs: {len(goods_costs)} уник. товаров")

    # ──────────────────────────────────────────────────
    # 3. Расчёт себестоимости блюд по техкартам — пошагово
    # ──────────────────────────────────────────────────
    print(f"\n[3/3] Считаю себестоимость блюд по {len(assembly_charts)} техкартам...")

    dish_cost_detail: list[dict] = []  # подробная разбивка по каждому блюду

    for chart in assembly_charts:
        dish_id   = chart.get("assembledProductId")
        dish_name = chart.get("assembledProductName") or chart.get("name") or ""
        items     = chart.get("items") or []

        if not dish_id:
            continue

        ingredients_detail: list[dict] = []
        total_cost = 0.0

        for item in items:
            ingredient_id   = item.get("productId")
            ingredient_name = item.get("productName") or item.get("name") or ""
            amount_raw      = item.get("amountOut", 0)  # нетто-выход ингредиента

            try:
                amount = float(amount_raw)
            except (ValueError, TypeError):
                amount = 0.0

            ingr_cost = goods_costs.get(ingredient_id) if ingredient_id else None
            line_cost = round(amount * ingr_cost, 6) if ingr_cost is not None else None

            if line_cost is not None:
                total_cost += line_cost

            ingredients_detail.append({
                "ingredient_id":   ingredient_id,
                "ingredient_name": ingredient_name,
                "amount":          amount,
                "unit":            item.get("unit") or item.get("measureUnit") or "",
                "cost_per_unit":   ingr_cost,        # None → нет в приходах
                "line_cost":       line_cost,         # None → пропущено
                "raw_item":        item,              # полный сырой элемент техкарты
            })

        dish_cost_detail.append({
            "dish_id":           dish_id,
            "dish_name":         dish_name,
            "calculated_cost":   round(total_cost, 2) if total_cost > 0 else None,
            "ingredients_count": len(items),
            "ingredients":       ingredients_detail,
            "raw_chart":         chart,  # полная сырая техкарта
        })

    output["dish_cost_detail"] = dish_cost_detail

    # Итоговая агрегированная карта (то, что уходит в прайс-лист)
    output["dish_costs_summary"] = {
        d["dish_id"]: d["calculated_cost"]
        for d in dish_cost_detail
        if d["calculated_cost"] is not None
    }

    dishes_with_cost    = sum(1 for d in dish_cost_detail if d["calculated_cost"] is not None)
    dishes_without_cost = len(dish_cost_detail) - dishes_with_cost
    print(f"      Блюд с себестоимостью     : {dishes_with_cost}")
    print(f"      Блюд без себестоимости    : {dishes_without_cost}")
    print(f"      (нет ингредиентов в приходах)")

    # ──────────────────────────────────────────────────
    # Сохранение
    # ──────────────────────────────────────────────────
    out_path = os.path.join(os.path.dirname(__file__), "debug_cost_prices_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✅ Данные сохранены → {out_path}")
    print(
        f"   Разделы в файле:\n"
        f"     raw_assembly_charts   — сырые обычные техкарты ({len(assembly_charts)} шт.)\n"
        f"     raw_prepared_charts   — сырые п/ф техкарты      ({len(prepared_charts)} шт., обычно пусты)\n"
        f"     raw_incoming_invoices — приходные накладные      ({len(invoices)} шт.)\n"
        f"     goods_costs           — себестоимость GOODS      ({len(goods_costs)} товаров)\n"
        f"     dish_cost_detail      — разбивка по блюдам       ({len(dish_cost_detail)} блюд)\n"
        f"     dish_costs_summary    — итог {{dish_id: cost}}     ({dishes_with_cost} блюд)\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
