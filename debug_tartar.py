"""Диагностика себестоимости 'Говяжья вырезка на тар тар цех'."""
import asyncio, os, sys
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
for _s in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(_s):
        os.environ[_s] = "stub"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa

DISH_ID   = "e4356263-ffa6-49ee-8fac-202618b0f234"  # Говяжья вырезка на тар тар цех
DATE_FROM = (date.today() - timedelta(days=180)).isoformat()
DATE_TO   = date.today().isoformat()


async def main():
    from adapters.iiko_api import fetch_assembly_charts, fetch_incoming_invoices, fetch_stock_balances
    from db.engine import get_session
    from db.models import Product
    from sqlalchemy import select

    # 1. Продукты
    async with get_session() as session:
        rows = await session.execute(select(Product))
        products = {str(p.id).lower(): p for p in rows.scalars().all()}

    def pname(uid):
        p = products.get(uid.lower())
        return p.name if p else uid

    # 2. Техкарты
    chart_data = await fetch_assembly_charts(DATE_FROM, DATE_TO, include_prepared=True)
    assembly   = chart_data.get("assemblyCharts") or []
    prepared   = chart_data.get("preparedCharts") or []

    dish_card = next(
        (c for c in assembly if c.get("assembledProductId", "").lower() == DISH_ID),
        None,
    )
    print(f"assemblyChart для блюда: {dish_card is not None}")
    if dish_card:
        items = dish_card.get("items") or []
        print(f"  dateFrom={dish_card.get('dateFrom','')[:10]}  items={len(items)}")
        for it in items:
            pid = it.get("productId", "").lower()
            print(f"  ингредиент: {pname(pid):<50} amountOut={it.get('amountOut')}  uuid={pid}")
            pf = next((c for c in prepared if c.get("assembledProductId", "").lower() == pid), None)
            if pf:
                print(f"    => п/ф preparedChart: items={len(pf.get('items') or [])}")
                for pi in pf.get("items") or []:
                    p2 = pi.get("productId", "").lower()
                    print(f"       {pname(p2):<50} amount={pi.get('amount')}  uuid={p2}")

    # Собираем все UUID из цепочки
    all_pids: set[str] = set()
    if dish_card:
        for it in dish_card.get("items") or []:
            pid = it.get("productId", "").lower()
            all_pids.add(pid)
            pf = next((c for c in prepared if c.get("assembledProductId", "").lower() == pid), None)
            if pf:
                for pi in pf.get("items") or []:
                    all_pids.add(pi.get("productId", "").lower())

    # 3. СЦС из остатков
    balances = await fetch_stock_balances()
    ps: dict[str, float] = {}
    pa: dict[str, float] = {}
    for e in balances:
        pid = (e.get("product") or "").lower()
        amt = float(e.get("amount") or 0)
        sm  = float(e.get("sum")    or 0)
        if pid and amt > 0 and sm > 0:
            ps[pid] = ps.get(pid, 0) + sm
            pa[pid] = pa.get(pid, 0) + amt
    scs = {pid: ps[pid] / pa[pid] for pid in ps if pa[pid] > 0}

    # 4. Накладные
    invoices = await fetch_incoming_invoices(DATE_FROM, DATE_TO)
    inv_last: dict[str, float] = {}
    inv_hist: dict[str, list]  = {}
    for inv in sorted(invoices, key=lambda x: x.get("dateIncoming") or ""):
        for it in inv.get("items") or []:
            pid = (it.get("productId") or "").lower()
            try:
                price = float(it.get("price") or 0)
            except Exception:
                price = 0.0
            if pid and price:
                inv_last[pid] = price
                inv_hist.setdefault(pid, []).append({
                    "date":  (inv.get("dateIncoming") or "")[:10],
                    "price": price,
                })

    # 5. Итог по каждому ингредиенту
    print("\n=== ЦЕНЫ ПО ИНГРЕДИЕНТАМ ЦЕПОЧКИ ===")
    for pid in sorted(all_pids):
        scs_v = scs.get(pid)
        inv_v = inv_last.get(pid)
        used  = scs_v if scs_v else inv_v
        flag  = "" if used else "  <<< НЕТ ЦЕНЫ"
        print(f"\n  {pname(pid)}")
        print(f"    uuid       = {pid}")
        print(f"    СЦС        = {round(scs_v, 4) if scs_v else 'нет (не в остатках)'}")
        print(f"    посл.накл  = {inv_v}")
        print(f"    ИСПОЛЬЗУЕТСЯ = {round(used, 4) if used else 'НЕТ'}{flag}")
        hist = inv_hist.get(pid) or []
        if hist:
            print(f"    История накладных (последние 5):")
            for h in sorted(hist, key=lambda x: x["date"])[-5:]:
                print(f"      {h['date']}  {h['price']:.2f}")
        else:
            print(f"    История накладных: пусто")

    # 6. Итоговый расчёт
    print("\n=== ИТОГ РАСЧЁТА ===")
    if dish_card:
        total = 0.0
        for it in dish_card.get("items") or []:
            pid = it.get("productId", "").lower()
            amt = float(it.get("amountOut") or 0)
            pf  = next((c for c in prepared if c.get("assembledProductId", "").lower() == pid), None)
            if pf:
                pf_total = 0.0
                for pi in pf.get("items") or []:
                    p2   = pi.get("productId", "").lower()
                    amt2 = float(pi.get("amount") or 0)
                    c2   = scs.get(p2) or inv_last.get(p2) or 0.0
                    pf_total += amt2 * c2
                asm = float(pf.get("assembledAmount") or 1) or 1
                unit_cost = pf_total / asm
                line = amt * unit_cost
                print(f"  п/ф {pname(pid)}: pf_cost={pf_total:.4f}, unit={unit_cost:.4f}, amountOut={amt}, line={line:.4f}")
            else:
                unit_cost = scs.get(pid) or inv_last.get(pid) or 0.0
                line = amt * unit_cost
                print(f"  GOODS {pname(pid)}: price={unit_cost:.4f}, amount={amt}, line={line:.4f}")
            total += line
        print(f"\n  ИТОГО наш расчёт: {total:.2f} руб")
        print(f"  В таблице сейчас: 2160.00 руб")
        print(f"  Разница: {total - 2160.0:+.2f} руб")


asyncio.run(main())
