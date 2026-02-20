"""Диагностика себестоимости 'Гренки на цезарь цех'."""
import asyncio, os, sys, json
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env и добавляем заглушки для переменных, не нужных для дебага
load_dotenv(Path(__file__).resolve().parent / ".env")
for _stub in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_stub):
        os.environ[_stub] = "stub"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa

DISH_NAME_HINT = "гренки на цезарь"
DATE_FROM = (date.today() - timedelta(days=90)).isoformat()
DATE_TO   = date.today().isoformat()
TODAY     = date.today().isoformat()

async def main():
    from adapters.iiko_api import fetch_assembly_charts, fetch_incoming_invoices, fetch_stock_balances
    from db.engine import get_session
    from db.models import Product
    from sqlalchemy import select

    # 1. Продукты из БД
    async with get_session() as session:
        rows = await session.execute(select(Product))
        products = {str(p.id).lower(): p for p in rows.scalars().all()}

    print("Все продукты 'Гренки на цезарь':")
    for uid, p in products.items():
        if DISH_NAME_HINT in (p.name or "").lower():
            print(f"  {str(p.product_type):12} {p.name}  {uid}")

    dish = next(
        (p for p in products.values()
         if DISH_NAME_HINT in (p.name or "").lower() and p.product_type == "DISH"),
        None,
    )
    if not dish:
        print("DISH не найден!"); return

    DISH_ID = str(dish.id).lower()
    print(f"\nДиагностируем: {dish.name}  ({DISH_ID})")

    def pname(uid):
        p = products.get(uid.lower())
        return p.name if p else uid

    # 2. Техкарты
    chart_data     = await fetch_assembly_charts(DATE_FROM, DATE_TO, include_prepared=True)
    assembly_charts = chart_data.get("assemblyCharts") or []
    prepared_charts = chart_data.get("preparedCharts") or []

    dish_card = next(
        (c for c in assembly_charts if c.get("assembledProductId","").lower() == DISH_ID),
        None,
    )
    print(f"\nassemblyChart блюда: {dish_card is not None}")
    if dish_card:
        print(f"  dateFrom={dish_card.get('dateFrom','')[:10]}  items={len(dish_card.get('items') or [])}")
        for it in dish_card.get("items") or []:
            pid = it.get("productId","").lower()
            print(f"    ingredient: {pname(pid)}  amountOut={it.get('amountOut')}  ({pid})")

    # 3. СЦС из остатков
    balances = await fetch_stock_balances()
    ps, pa = {}, {}
    for e in balances:
        pid = (e.get("product") or "").lower()
        amt = float(e.get("amount") or 0)
        sm  = float(e.get("sum") or 0)
        if pid and amt > 0 and sm > 0:
            ps[pid] = ps.get(pid,0) + sm
            pa[pid] = pa.get(pid,0) + amt
    scs = {pid: ps[pid]/pa[pid] for pid in ps if pa[pid]>0}

    # 4. Fallback накладные
    invoices = await fetch_incoming_invoices(DATE_FROM, DATE_TO)
    inv_last: dict[str,float] = {}
    inv_hist: dict[str,list]  = {}
    for inv in sorted(invoices, key=lambda x: x.get("dateIncoming") or ""):
        for it in inv.get("items") or []:
            pid = (it.get("productId") or "").lower()
            try: price = float(it.get("price") or 0)
            except: price = 0.0
            if pid and price:
                inv_last[pid] = price
                inv_hist.setdefault(pid,[]).append({
                    "date": (inv.get("dateIncoming") or "")[:10], "price": price
                })

    goods_costs = {**inv_last, **scs}

    # 5. Рассчитываем цепочку через п/ф (итеративно)
    print("\n=== Расчёт себестоимости ===")

    def calc_chart_cost(card, amount_field, goods_costs, depth=0):
        items = card.get("items") or []
        total = 0.0
        for it in items:
            pid = (it.get("productId") or "").lower()
            amt = float(it.get(amount_field) or 0)
            cost = goods_costs.get(pid, 0.0)
            line = amt * cost
            total += line
            scs_val = scs.get(pid)
            inv_val = inv_last.get(pid)
            print("  "*depth + f"  {pname(pid):<45} amt={amt:.6f}  СЦС={scs_val}  накл={inv_val}  →line={line:.4f}")
        return total

    if dish_card:
        dish_items = dish_card.get("items") or []
        dish_total = 0.0
        for it in dish_items:
            pid = (it.get("productId") or "").lower()
            amt = float(it.get("amountOut") or 0)

            # Проверяем — это п/ф?
            pf_card = next(
                (c for c in prepared_charts if c.get("assembledProductId","").lower() == pid),
                None,
            )
            if pf_card:
                print(f"\n  п/ф: {pname(pid)} ({pid})")
                print(f"       preparedChart: items={len(pf_card.get('items') or [])}")
                pf_cost = calc_chart_cost(pf_card, "amount", goods_costs, depth=1)
                asm = float(pf_card.get("assembledAmount") or 1) or 1
                unit_cost = pf_cost / asm
                print(f"       pf_total={pf_cost:.4f}  assembledAmount={asm}  unit_cost={unit_cost:.4f}")
                line = amt * unit_cost
            else:
                unit_cost = goods_costs.get(pid, 0.0)
                line = amt * unit_cost
                print(f"\n  GOODS: {pname(pid)}  СЦС={scs.get(pid)}  накл={inv_last.get(pid)}  amt={amt}  line={line:.4f}")
            dish_total += line

        print(f"\n  ИТОГО блюдо: {dish_total:.4f} руб")
        print(f"  Айко показывает: 434.81 руб")
        print(f"  Разница: {dish_total - 434.81:+.4f} руб")

asyncio.run(main())
