"""
debug_shu_meringa.py
====================
Детальная диагностика себестоимости блюда "Шу меренга цех".

DISH_ID = "53742a20-5f14-4f36-accf-33773fe8a1d7"  # Шу меренга цех (DISH)
PF_ID   = "cc4b73ec-3395-4767-97a0-3effadb08728"  # п/ф Шу с меренгой (PREPARED)

Запуск: python debug_shu_meringa.py
Результат: debug_shu_meringa_output.json
"""

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

# Путь к корню проекта
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Загружаем .env и добавляем заглушки для переменных, не нужных для дебага
load_dotenv(Path(__file__).resolve().parent / ".env")
for _stub in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_stub):
        os.environ[_stub] = "stub"

import config  # noqa – инициализирует переменные окружения

DISH_ID = "53742a20-5f14-4f36-accf-33773fe8a1d7"  # Шу меренга цех (DISH)
PF_ID   = "cc4b73ec-3395-4767-97a0-3effadb08728"  # п/ф Шу с меренгой (PREPARED)

DATE_FROM = (date.today() - timedelta(days=90)).isoformat()
DATE_TO   = date.today().isoformat()
TODAY     = date.today().isoformat()


async def main():
    from adapters.iiko_api import (
        fetch_assembly_charts,
        fetch_incoming_invoices,
        fetch_stock_balances,
    )
    from db.engine import get_session
    from db.models import Product

    output = {}

    # ── 1. Загружаем все техкарты ──────────────────────────────────────────────
    print("Загружаем техкарты из iiko...")
    chart_data = await fetch_assembly_charts(
        date_from=DATE_FROM,
        date_to=DATE_TO,
        include_prepared=True,
    )

    assembly_charts  = chart_data.get("assemblyCharts")  or []
    prepared_charts  = chart_data.get("preparedCharts")  or []

    print(f"  assemblyCharts: {len(assembly_charts)}  preparedCharts: {len(prepared_charts)}")

    # ── 2. Ищем карту блюда (DISH) ─────────────────────────────────────────────
    dish_card = None
    for c in assembly_charts:
        if c.get("assembledProductId", "").lower().startswith(DISH_ID[:8].lower()):
            dish_card = c
            break
    # попробуем точное совпадение если не нашли
    if dish_card is None:
        for c in assembly_charts:
            if c.get("assembledProductId", "").lower() == DISH_ID.lower():
                dish_card = c
                break

    output["raw_dish_assemblyChart"] = dish_card
    if dish_card is None:
        print(f"!!! assemblyChart для DISH {DISH_ID} НЕ НАЙДЕН")
    else:
        print(f"  Нашли assemblyChart блюда: dateFrom={dish_card.get('dateFrom','?')[:10]}  items={len(dish_card.get('items') or [])}")

    # ── 3. Ищем карту п/ф (PREPARED) ──────────────────────────────────────────
    pf_card = None
    for c in prepared_charts:
        if c.get("assembledProductId", "").lower().startswith(PF_ID[:8].lower()):
            pf_card = c
            break
    if pf_card is None:
        for c in prepared_charts:
            if c.get("assembledProductId", "").lower() == PF_ID.lower():
                pf_card = c
                break

    output["raw_pf_preparedChart"] = pf_card
    if pf_card is None:
        print(f"!!! preparedChart для п/ф {PF_ID} НЕ НАЙДЕН")
    else:
        print(f"  Нашли preparedChart п/ф: dateFrom={pf_card.get('dateFrom','?')[:10]}  items={len(pf_card.get('items') or [])}")

    # ── 4. Собираем все UUID продуктов, задействованных в цепочке ─────────────
    involved_ids: set[str] = {DISH_ID, PF_ID}
    if dish_card:
        for item in (dish_card.get("items") or []):
            involved_ids.add(item.get("productId", ""))
    if pf_card:
        for item in (pf_card.get("items") or []):
            involved_ids.add(item.get("productId", ""))
    involved_ids.discard("")

    # ── 5. Получаем имена/типы продуктов из БД ────────────────────────────────
    print("Загружаем информацию о продуктах из БД...")
    product_info: dict[str, dict] = {}
    try:
        async with get_session() as session:
            from sqlalchemy import select
            rows = await session.execute(select(Product))
            products = rows.scalars().all()
            for p in products:
                uid = str(p.id or "").lower()
                product_info[uid] = {
                    "name": p.name,
                    "type": p.product_type,
                }
        print(f"  В БД продуктов: {len(product_info)}")
    except Exception as e:
        print(f"  Ошибка БД: {e}")

    def pname(uid: str) -> str:
        info = product_info.get(uid.lower())
        return info["name"] if info else uid

    output["product_info"] = {
        uid: product_info.get(uid.lower(), {"name": "???", "type": "???"})
        for uid in sorted(involved_ids)
    }

    # ── 6. Загружаем остатки склада для СЦС ───────────────────────────────────
    print("Загружаем остатки склада (СЦС)...")
    balances = await fetch_stock_balances()
    print(f"  Записей остатков: {len(balances)}")

    product_sum: dict[str, float] = {}
    product_amt: dict[str, float] = {}
    for entry in balances:
        pid = (entry.get("product") or "").lower().strip()
        amt = float(entry.get("amount") or 0)
        sm  = float(entry.get("sum")    or 0)
        if pid and amt > 0 and sm > 0:
            product_sum[pid] = product_sum.get(pid, 0.0) + sm
            product_amt[pid] = product_amt.get(pid, 0.0) + amt

    goods_costs_scs: dict[str, float] = {
        pid: product_sum[pid] / product_amt[pid]
        for pid in product_sum if product_amt[pid] > 0
    }
    print(f"  Товаров с СЦС: {len(goods_costs_scs)}")

    # ── 7. Загружаем накладные (fallback для товаров без остатка) ──────────────
    print("Загружаем накладные (fallback)...")
    invoices_raw = await fetch_incoming_invoices(date_from=DATE_FROM, date_to=DATE_TO)
    print(f"  Накладных загружено: {len(invoices_raw)}")

    invoice_history: dict[str, list[dict]] = {}
    for inv in invoices_raw:
        inv_date = (inv.get("incomingDate") or inv.get("date") or "")[:10]
        for item in (inv.get("items") or []):
            pid = (item.get("productId") or "").lower()
            price = item.get("priceWithVat") or item.get("price") or 0
            try:
                price = float(price)
            except Exception:
                price = 0.0
            if pid and price:
                invoice_history.setdefault(pid, []).append({
                    "date": inv_date,
                    "price": price,
                    "invoice_id": inv.get("id") or inv.get("invoiceNumber"),
                })

    goods_costs_inv: dict[str, float] = {}
    for pid, entries in invoice_history.items():
        entries.sort(key=lambda x: x["date"])
        goods_costs_inv[pid] = entries[-1]["price"]

    # Объединяем: СЦС приоритетнее
    goods_costs: dict[str, float] = {**goods_costs_inv, **goods_costs_scs}

    # Фильтруем историю только по нужным продуктам
    output["invoice_price_history"] = {
        uid: invoice_history.get(uid.lower(), [])
        for uid in sorted(involved_ids)
    }
    output["scs_prices"] = {
        uid: goods_costs_scs.get(uid.lower())
        for uid in sorted(involved_ids)
    }

    # ── 7. Шаг A: себестоимость п/ф (cc4b73ec) ────────────────────────────────
    pf_lines: list[dict] = []
    pf_total = 0.0

    if pf_card:
        pf_items = pf_card.get("items") or []
        for item in pf_items:
            pid       = (item.get("productId") or "").lower()
            amount    = float(item.get("amount") or 0)
            unit_cost = goods_costs.get(pid, 0.0)
            line_cost = amount * unit_cost
            pf_total += line_cost
            pf_lines.append({
                "productId":   pid,
                "name":        pname(pid),
                "amount":      amount,
                "unit_cost":   unit_cost,
                "line_cost":   round(line_cost, 4),
                "has_price":   unit_cost > 0,
            })
        # Если в карте есть assembledAmount — нормируем (на 1 штуку)
        assembled_amount = float(pf_card.get("assembledAmount") or 1) or 1
        pf_cost_per_unit = pf_total / assembled_amount
    else:
        pf_cost_per_unit = 0.0
        assembled_amount = 1.0

    output["step_A_pf_calculation"] = {
        "pf_id":              PF_ID,
        "pf_name":            pname(PF_ID),
        "assembledAmount":    assembled_amount,
        "items":              pf_lines,
        "total_batch_cost":   round(pf_total, 4),
        "cost_per_unit":      round(pf_cost_per_unit, 4),
    }
    print(f"  Себестоимость п/ф: total={pf_total:.4f}  assembledAmount={assembled_amount}  per_unit={pf_cost_per_unit:.4f}")

    # ── 8. Шаг B: себестоимость блюда (53742a20) ──────────────────────────────
    dish_lines: list[dict] = []
    dish_total = 0.0

    if dish_card:
        for item in (dish_card.get("items") or []):
            pid       = (item.get("productId") or "").lower()
            amount    = float(item.get("amountOut") or item.get("amount") or 0)
            # Если это п/ф — берём только что вычисленную стоимость
            if pid.startswith(PF_ID[:8].lower()) or pid == PF_ID.lower():
                unit_cost = pf_cost_per_unit
            else:
                unit_cost = goods_costs.get(pid, 0.0)
            line_cost = amount * unit_cost
            dish_total += line_cost
            dish_lines.append({
                "productId": pid,
                "name":      pname(pid),
                "amount":    amount,
                "unit_cost": round(unit_cost, 4),
                "line_cost": round(line_cost, 4),
                "is_pf":     pid.startswith(PF_ID[:8].lower()) or pid == PF_ID.lower(),
            })

    output["step_B_dish_calculation"] = {
        "dish_id":    DISH_ID,
        "dish_name":  pname(DISH_ID),
        "items":      dish_lines,
        "total_cost": round(dish_total, 4),
    }
    print(f"  Себестоимость блюда (наш расчёт): {dish_total:.4f} руб")

    # ── 9. Итоги ──────────────────────────────────────────────────────────────
    iiko_reference = 43.0  # значение из iiko UI
    diff = round(dish_total - iiko_reference, 4)

    output["comparison"] = {
        "our_calculation":      round(dish_total, 4),
        "iiko_reference":       iiko_reference,
        "difference":           diff,
        "top_expensive_pf_ingredients": sorted(
            [l for l in pf_lines if l["line_cost"] > 0],
            key=lambda x: x["line_cost"],
            reverse=True,
        )[:5],
    }

    print(f"\n=== Сравнение ===")
    print(f"  Наш расчёт : {dish_total:.2f} руб")
    print(f"  Айко (UI)  : {iiko_reference:.2f} руб")
    print(f"  Разница    : {diff:+.2f} руб")

    # ── 10. Сохраняем в файл ─────────────────────────────────────────────────
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_shu_meringa_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nРезультат сохранён: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
