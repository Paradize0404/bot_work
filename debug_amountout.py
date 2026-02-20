"""Проверяем amountOut в assemblyCharts — нужно ли делить на него."""
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
    from datetime import date
    from adapters.iiko_api import fetch_assembly_charts

    today = date.today().strftime("%Y-%m-%d")
    data = await fetch_assembly_charts(today, today, include_prepared=True)

    targets = [
        "фиш майо", "шу шоколад", "говяжья вырезка", "шу меренга",
        "гренки на цезарь", "сырники цех", "соус тоннато", "тар тар",
        "тальятелле", "ростбиф", "сырники", "боул", "форель",
    ]

    assembly_charts = data.get("assemblyCharts") or []
    prepared_charts = data.get("preparedCharts") or []

    print(f"Всего assemblyCharts: {len(assembly_charts)}, preparedCharts: {len(prepared_charts)}")
    print()

    # Сначала — статистика по amountOut в assemblyCharts
    amount_out_values = {}
    for c in assembly_charts:
        ao = c.get("amountOut")
        amount_out_values[ao] = amount_out_values.get(ao, 0) + 1

    print("=== Статистика amountOut в assemblyCharts ===")
    for v, cnt in sorted(amount_out_values.items(), key=lambda x: x[0] or 0):
        print(f"  amountOut={v}: {cnt} техкарт")

    # То же для preparedCharts
    prep_ao_values = {}
    for c in prepared_charts:
        ao = c.get("amountOut")
        prep_ao_values[ao] = prep_ao_values.get(ao, 0) + 1
    print("=== Статистика amountOut в preparedCharts ===")
    for v, cnt in sorted(prep_ao_values.items(), key=lambda x: x[0] or 0):
        print(f"  amountOut={v}: {cnt} техкарт")

    print()
    print("=== Целевые блюда ===")
    all_charts = [(c, "DISH", "amountOut") for c in assembly_charts] + \
                 [(c, "PREP", "amount")  for c in prepared_charts]

    shown = set()
    for c, chart_type, ingr_field in sorted(all_charts, key=lambda x: x[0].get("assembledProductName") or ""):
        name = (c.get("assembledProductName") or "").lower()
        for hint in targets:
            if hint in name and name not in shown:
                shown.add(name)
                orig_name = c.get("assembledProductName") or c.get("assembledProductId")
                ao = c.get("amountOut")
                items = c.get("items") or []
                total_ingr = sum(float(it.get(ingr_field) or 0) for it in items)
                print(f"  [{chart_type}] {orig_name}")
                print(f"    amountOut={ao}  ингредиентов={len(items)}  сумма_quantities={total_ingr:.4f}")
                # Примеры ингредиентов
                for it in items[:5]:
                    amt = it.get(ingr_field) or it.get("amount") or it.get("amountOut")
                    ingr_name = it.get("name") or it.get("productId") or "?"
                    print(f"      {ingr_name}: {amt}")
                if len(items) > 5:
                    print(f"      ... ещё {len(items)-5} ингредиентов")
                # Ключевой вопрос: если amountOut > 1, нужно делить на него
                if ao and float(ao) > 1:
                    print(f"    !!! amountOut={ao} > 1 — нужно делить итоговую стоимость на {ao}!")
                break


asyncio.run(main())
