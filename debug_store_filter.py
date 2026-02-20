"""Сравниваем цены ингредиентов С фильтром складов и БЕЗ."""
import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(Path(__file__).resolve().parent / ".env")
for s in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(s):
        os.environ[s] = "stub"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa


async def main():
    from adapters.iiko_api import fetch_stock_balances, fetch_incoming_invoices
    from use_cases.product_request import get_request_stores
    from db.engine import async_session_factory
    from db.models import Store, Product
    from sqlalchemy import select
    from datetime import date, timedelta

    today = date.today().strftime("%Y-%m-%d")
    date_from = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

    # Склады подразделения
    selected = await get_request_stores()
    dept_store_ids: set[str] = set()
    if selected:
        dept_uuid = selected[0]["id"]
        dept_name = selected[0]["name"]
        async with async_session_factory() as sess:
            res = await sess.execute(
                select(Store.id, Store.name)
                .where(Store.deleted.is_(False))
                .where(Store.parent_id == dept_uuid)
            )
            stores_dept = [{"id": str(r.id).lower(), "name": r.name} for r in res.all()]
            dept_store_ids = {s["id"] for s in stores_dept}
        print(f"Подразделение: {dept_name}")
        print(f"Склады: {[s['name'] for s in stores_dept]}")
    print()

    # ВСЕ склады
    balances = await fetch_stock_balances()
    store_ids_all = set((b.get("store") or "").lower() for b in balances if b.get("store"))
    print(f"Всего складов в остатках: {len(store_ids_all)}")
    print(f"Складов в подразделении: {len(dept_store_ids)}")
    print()

    # Имена продуктов
    async with async_session_factory() as sess:
        rows = await sess.execute(select(Product.id, Product.name))
        names = {str(r.id).lower(): r.name for r in rows.all()}

    # Считаем СЦС двумя способами
    def calc_ccs(balances, filter_stores=None):
        p_sum, p_amt = {}, {}
        for e in balances:
            sid = (e.get("store") or "").lower()
            if filter_stores and sid not in filter_stores:
                continue
            pid = (e.get("product") or "").lower()
            amt = float(e.get("amount") or 0)
            sm = float(e.get("sum") or 0)
            if pid and amt > 0 and sm > 0:
                p_sum[pid] = p_sum.get(pid, 0.0) + sm
                p_amt[pid] = p_amt.get(pid, 0.0) + amt
        return {pid: p_sum[pid] / p_amt[pid] for pid in p_sum if p_amt[pid] > 0}

    ccs_all = calc_ccs(balances)
    ccs_dept = calc_ccs(balances, dept_store_ids)

    # Fallback: накладные
    from adapters.iiko_api import fetch_incoming_invoices
    invoices = await fetch_incoming_invoices(date_from, today)
    invoices.sort(key=lambda x: x.get("dateIncoming") or "")
    fallback = {}
    for inv in invoices:
        for item in inv.get("items", []):
            pid = (item.get("productId") or "").lower()
            try:
                price = float(item.get("price") or 0)
            except (ValueError, TypeError):
                price = 0
            if pid and price > 0:
                fallback[pid] = price

    # Ключевые ингредиенты блюд с расхождениями
    targets = [
        "хлеб", "чиабатта", "фокачча", "майонез", "шоколад", "сливки",
        "какао", "кракелин", "шу пустой", "сюр тесто", "заварное тесто",
        "меренга", "сыр", "фиш рыбный", "шрирача",
    ]

    print(f"{'Продукт':<50} {'СЦС всё':>12}  {'СЦС отдел':>12}  {'Разница':>12}  {'Fallback':>12}")
    print("-" * 102)

    # Все продукты у которых есть расхождение
    all_pids = set(ccs_all.keys()) | set(ccs_dept.keys())
    differing = []
    for pid in all_pids:
        name = names.get(pid, pid)
        v_all = ccs_all.get(pid)
        v_dept = ccs_dept.get(pid)
        fall = fallback.get(pid)
        if v_all != v_dept:
            differing.append((name, v_all, v_dept, fall))

    differing.sort(key=lambda x: x[0].lower() if x[0] else "")

    # Сначала целевые
    for hint in targets:
        for item in differing:
            name, v_all, v_dept, fall = item
            if hint in (name or "").lower():
                diff = (v_dept or 0) - (v_all or 0) if v_all and v_dept else None
                va_s = f"{v_all:.2f}" if v_all else "—"
                vd_s = f"{v_dept:.2f}" if v_dept else "—"
                df_s = f"{diff:+.2f}" if diff is not None else "ПРОПАЛ"
                fb_s = f"{fall:.2f}" if fall else "—"
                print(f"  {name:<50} {va_s:>12}  {vd_s:>12}  {df_s:>12}  {fb_s:>12}")
                break

    print()
    print(f"Итого продуктов с расхождением: {len(differing)}")
    print(f"Есть в СЦС все: {len(ccs_all)}, есть в СЦС отдел: {len(ccs_dept)}")
    only_all = len(set(ccs_all.keys()) - set(ccs_dept.keys()))
    only_dept = len(set(ccs_dept.keys()) - set(ccs_all.keys()))
    print(f"Только в all (пропадают при фильтре): {only_all}")
    print(f"Только в dept (новые при фильтре): {only_dept}")
    print()
    print("=== ТОП-20 продуктов, которые ИСЧЕЗЛИ при фильтре по складам подразделения ===")
    vanished = [(names.get(pid, pid), ccs_all[pid], fallback.get(pid)) for pid in ccs_all if pid not in ccs_dept]
    vanished.sort(key=lambda x: x[0].lower() if x[0] else "")
    for name, v_all, fall in vanished[:50]:
        fb_s = f"fallback→{fall:.2f}" if fall else "нет fallback!"
        print(f"  {name:<50} СЦС={v_all:.2f}  {fb_s}")


asyncio.run(main())
