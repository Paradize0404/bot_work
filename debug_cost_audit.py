"""
Полный аудит себестоимости блюд:
- Берём техкарты из assemblyCharts
- Смотрим остатки на складах подразделения из Настроек
- Раскладываем каждое блюдо на ингредиенты с трассировкой
- Большая выборка + детали по "фиш майо" и "шу шоколад"
"""
import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

# Принудительно UTF-8 для stdout
sys.stdout.reconfigure(encoding="utf-8")

OUT_FILE = Path(__file__).resolve().parent / "audit_output.txt"
_orig_stdout = sys.stdout

class Tee:
    """Пишет одновременно в stdout и в файл."""
    def __init__(self, *files):
        self.files = files
    def write(self, data):
        for f in self.files:
            f.write(data)
    def flush(self):
        for f in self.files:
            f.flush()

_file_out = open(OUT_FILE, "w", encoding="utf-8")
sys.stdout = Tee(_orig_stdout, _file_out)

load_dotenv(Path(__file__).resolve().parent / ".env")
for s in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET"):
    if not os.environ.get(s):
        os.environ[s] = "stub"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa


async def main():
    from datetime import date, timedelta
    from adapters.iiko_api import fetch_assembly_charts, fetch_stock_balances, fetch_incoming_invoices
    from use_cases.product_request import get_request_stores
    from db.engine import async_session_factory
    from db.models import Store, Product
    from sqlalchemy import select

    today = date.today().strftime("%Y-%m-%d")
    date_from = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

    # ── 1. Склады подразделения из Настроек ──────────────────────────────────
    dept_store_ids: set[str] = set()
    dept_name = "все склады"
    try:
        selected = await get_request_stores()
        if selected:
            dept_uuid = selected[0]["id"]
            dept_name = selected[0]["name"]
            async with async_session_factory() as sess:
                res = await sess.execute(
                    select(Store.id, Store.name)
                    .where(Store.deleted.is_(False))
                    .where(Store.parent_id == dept_uuid)
                    .order_by(Store.name)
                )
                stores = [{"id": str(r.id), "name": r.name} for r in res.all()]
                dept_store_ids = {s["id"].lower() for s in stores}
                print(f"Подразделение: {dept_name}")
                print(f"Склады ({len(dept_store_ids)}):")
                for s in stores:
                    print(f"  {s['name']}  [{s['id']}]")
        else:
            print("[WARN] Zavedenie ne vybrano — vse sklady")
    except Exception as e:
        print(f"  [ERR] Oshibka zagruzki skladov: {e}")

    print()

    # ── 2. Остатки → СЦС (сначала все склады, потом фильтр по подразделению) ───────────
    balances = await fetch_stock_balances()

    def _build_ccs(entries, filter_stores=None):
        p_sum: dict[str, float] = {}
        p_amt: dict[str, float] = {}
        for entry in entries:
            sid = (entry.get("store") or "").lower()
            if filter_stores and sid not in filter_stores:
                continue
            pid = (entry.get("product") or "").lower()
            amt = float(entry.get("amount") or 0)
            sm  = float(entry.get("sum")    or 0)
            if pid and amt > 0 and sm > 0:
                p_sum[pid] = p_sum.get(pid, 0.0) + sm
                p_amt[pid] = p_amt.get(pid, 0.0) + amt
        return (
            {pid: p_sum[pid] / p_amt[pid] for pid in p_sum if p_amt[pid] > 0},
            p_sum, p_amt
        )

    # СЦС по всем складам (фоллбэк уровень 1)
    goods_costs_all, _, _ = _build_ccs(balances)

    if dept_store_ids:
        # СЦС только склады подразделения
        goods_costs_dept, product_sum, product_amt = _build_ccs(balances, dept_store_ids)
        # Начинаем с dept, дополняем всеми для отсутствующих
        goods_costs = dict(goods_costs_dept)
        for pid, price in goods_costs_all.items():
            if pid not in goods_costs:
                goods_costs[pid] = price
        print(f"СЦС подразделение: {len(goods_costs_dept)} товаров, "
              f"all-stores fallback: {len(goods_costs) - len(goods_costs_dept)} товаров")
    else:
        goods_costs = goods_costs_all
        goods_costs_dept = goods_costs_all
        product_sum, product_amt = {}, {}
        print(f"СЦС (все склады): {len(goods_costs)} товаров")

    # ── 3. Fallback: последняя накладная ──────────────────────────────────────
    invoices = await fetch_incoming_invoices(date_from, today)
    invoices.sort(key=lambda x: x.get("dateIncoming") or "")
    fallback: dict[str, float] = {}
    for inv in invoices:
        for item in inv.get("items", []):
            pid = (item.get("productId") or "").strip().lower()
            try:
                price = float(item.get("price") or 0)
            except (ValueError, TypeError):
                price = 0
            if pid and price > 0:
                fallback[pid] = price
    for pid, price in fallback.items():
        if pid not in goods_costs:
            goods_costs[pid] = price

    # Имена продуктов из БД
    async with async_session_factory() as sess:
        rows = await sess.execute(select(Product.id, Product.name, Product.product_type))
        all_products = {str(r.id).lower(): {"name": r.name, "type": r.product_type} for r in rows.all()}

    print(f"СЦС (все склады): {len(goods_costs_all)} товаров |СЦС итого (dept+all): {len(goods_costs)} товаров")
    print(f"Fallback (накладные): {len(fallback)} товаров")
    print(f"Итого goods_costs: {len(goods_costs)} товаров")
    print()

    # ── 4. Техкарты ──────────────────────────────────────────────────────────
    chart_data = await fetch_assembly_charts(today, today, include_prepared=True)
    assembly = chart_data.get("assemblyCharts") or []
    prepared = chart_data.get("preparedCharts") or []

    # Строим chart_map (самая свежая действующая техкарта)
    def pick_effective(charts, amount_field):
        best = {}
        for c in charts:
            pid = (c.get("assembledProductId") or "").lower()
            if not pid:
                continue
            df = (c.get("dateFrom") or "")[:10]
            dt = (c.get("dateTo")   or "")[:10]
            if dt and dt < today:
                continue
            if df > today:
                continue
            if pid not in best or df > best[pid][0]:
                best[pid] = (df, c, amount_field)
        return {pid: (v[1], v[2]) for pid, v in best.items()}

    chart_map = {}
    chart_map.update(pick_effective(prepared, "amountOut"))
    chart_map.update(pick_effective(assembly, "amountOut"))

    # Итеративный расчёт всех блюд (включая п/ф)
    product_types = {pid: info["type"] for pid, info in all_products.items()}
    all_costs = {pid: c for pid, c in goods_costs.items() if product_types.get(pid) != "DISH"}

    for _ in range(10):
        changes = False
        for dish_id, (chart, amt_field) in chart_map.items():
            if dish_id in all_costs:
                continue
            items = chart.get("items") or []
            total = 0.0
            ready = True
            for item in items:
                ingr_id = (item.get("productId") or "").lower()
                try:
                    amt = float(item.get(amt_field) or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                cost = all_costs.get(ingr_id)
                if cost is None:
                    if ingr_id in chart_map:
                        ready = False
                        break
                    continue
                total += amt * cost
            if ready and total > 0:
                assembled_amount = float(chart.get("assembledAmount") or 1.0)
                if assembled_amount <= 0:
                    assembled_amount = 1.0
                all_costs[dish_id] = round(total / assembled_amount, 4)
                changes = True
        if not changes:
            break

    dish_costs = {pid: c for pid, c in all_costs.items() if product_types.get(pid) == "DISH"}

    # ── 5. Трассировка: детальная разбивка по ингредиентам ───────────────────
    def trace_dish(dish_id: str, depth: int = 0) -> float:
        indent = "  " * depth
        chart_entry = chart_map.get(dish_id)
        if not chart_entry:
            cost = all_costs.get(dish_id)
            name = all_products.get(dish_id, {}).get("name", dish_id)
            print(f"{indent}  [нет техкарты] {name} -> {cost}")
            return cost or 0

        chart, amt_field = chart_entry
        assembled_amount_c = float(chart.get("assembledAmount") or 1.0)
        if assembled_amount_c <= 0:
            assembled_amount_c = 1.0
        items = chart.get("items") or []
        total = 0.0
        for item in items:
            ingr_id = (item.get("productId") or "").lower()
            ingr_name = all_products.get(ingr_id, {}).get("name") or item.get("name") or ingr_id
            ingr_type = product_types.get(ingr_id, "?")
            try:
                amt = float(item.get(amt_field) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            unit_cost = all_costs.get(ingr_id)
            in_balance = ingr_id in goods_costs_dept
            source = "СЦС-отдел" if in_balance else ("СЦС-all" if ingr_id in goods_costs_all else ("накладная" if ingr_id in fallback else "нет"))

            if unit_cost is None:
                print(f"{indent}  [NO PRICE] {ingr_name} [{ingr_type}] x {amt} = НЕТ ЦЕНЫ ({source})")
                # Если это п/ф — рекурсивно
                if ingr_id in chart_map:
                    trace_dish(ingr_id, depth + 2)
            else:
                contrib = unit_cost * amt
                total += contrib
                balance_info = ""
                print(f"{indent}  [OK] {ingr_name} [{ingr_type}] x {amt} x {unit_cost:.4f} = {contrib:.4f}  [{source}{balance_info}]")
                # Если ингредиент сам является п/ф — покажем его состав
                if ingr_id in chart_map and ingr_type in ("PREPARED",):
                    trace_dish(ingr_id, depth + 2)

        per_unit = total / assembled_amount_c
        if assembled_amount_c != 1.0:
            print(f"{indent}  >> assembledAmount={assembled_amount_c}: итог_ингредиентов={total:.4f} / {assembled_amount_c} = {per_unit:.4f} за ед.")
        return per_unit

    # ── 6. Выборка: все DISH с техкартами, сортировка по имени ───────────────
    dish_with_charts = [
        (pid, all_products.get(pid, {}).get("name", pid), dish_costs.get(pid))
        for pid in chart_map
        if product_types.get(pid) == "DISH"
    ]
    dish_with_charts.sort(key=lambda x: x[1].lower() if x[1] else "")

    print(f"{'='*80}")
    print(f"ИТОГО: {len(dish_with_charts)} блюд с техкартами, {len(dish_costs)} с рассчитанной себестоимостью")
    print(f"{'='*80}")
    print()

    # ── 7. Краткая таблица всех блюд ─────────────────────────────────────────
    print(f"{'Блюдо':<55} {'Себестоимость':>14}  {'Статус'}")
    print("-" * 80)
    no_cost = []
    has_cost = []
    for pid, name, cost in dish_with_charts:
        if cost is None:
            no_cost.append((pid, name))
            status = "[NO]"
        else:
            has_cost.append((pid, name, cost))
            status = "[OK]"
        cost_str = f"{cost:.2f}" if cost is not None else "-"
        print(f"  {name:<53} {cost_str:>14}  {status}")

    print()
    print(f"Рассчитано: {len(has_cost)}, без цены: {len(no_cost)}")
    print()

    # ── 8. Детальная трассировка: целевые блюда + выборка ────────────────────
    # Целевые: фиш майо + шу шоколад + 5 случайных для проверки
    target_hints = ["фиш майо", "шу шоколад", "говяжья вырезка", "шу меренга", "гренки на цезарь"]

    # Добавим 10 случайных блюд для репрезентативности
    import random
    random.seed(42)
    sample_extra = random.sample(has_cost, min(10, len(has_cost)))

    detail_targets = []
    for hint in target_hints:
        for pid, name, cost in has_cost:
            if hint in (name or "").lower():
                detail_targets.append((pid, name, cost))
                break
        for pid, name in no_cost:
            if hint in (name or "").lower():
                detail_targets.append((pid, name, None))
                break

    # Убираем дубли
    seen = set()
    unique_targets = []
    for item in detail_targets + sample_extra:
        pid = item[0]
        if pid not in seen:
            seen.add(pid)
            unique_targets.append(item)

    print(f"{'='*80}")
    print("ДЕТАЛЬНАЯ ТРАССИРОВКА")
    print(f"{'='*80}")

    for pid, name, cost in unique_targets:
        print(f"\n{'─'*80}")
        print(f"БЛЮДО: {name}")
        print(f"UUID:  {pid}")
        print(f"Итоговая себестоимость: {cost:.4f}" if cost is not None else "Итоговая себестоимость: НЕТ")
        print(f"{'─'*40}")
        traced_total = trace_dish(pid)
        print(f"  → Пересчитано вручную: {traced_total:.4f}")
        if cost is not None:
            diff = abs(cost - traced_total)
            if diff > 0.02:
                print(f"  [!] Rasskhozhdenie: {diff:.4f}")

    # ── 9. Блюда без цены — почему? ───────────────────────────────────────────
    if no_cost:
        print(f"\n{'='*80}")
        print(f"БЛЮДА БЕЗ СЕБЕСТОИМОСТИ ({len(no_cost)}) — детали первых 10")
        print(f"{'='*80}")
        for pid, name in no_cost[:10]:
            print(f"\n  БЛЮДО: {name}  [{pid}]")
            chart_entry = chart_map.get(pid)
            if not chart_entry:
                print("    нет техкарты!")
                continue
            chart, amt_field = chart_entry
            items = chart.get("items") or []
            for item in items:
                ingr_id = (item.get("productId") or "").lower()
                ingr_name = all_products.get(ingr_id, {}).get("name") or ingr_id
                ingr_type = product_types.get(ingr_id, "?")
                unit_cost = all_costs.get(ingr_id)
                in_bal = "да" if ingr_id in product_sum else "нет"
                in_fal = "да" if ingr_id in fallback else "нет"
                print(f"    {ingr_name} [{ingr_type}] cost={unit_cost}  остаток={in_bal}  накладная={in_fal}")


asyncio.run(main())
sys.stdout = _orig_stdout
_file_out.close()
print(f"Результаты записаны в: {OUT_FILE}")
