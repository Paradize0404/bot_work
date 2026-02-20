"""Смотрим реальную структуру assemblyCharts рецепта."""
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
    import json
    from datetime import date
    from adapters.iiko_api import fetch_assembly_charts
    from db.engine import async_session_factory
    from db.models import Product
    from sqlalchemy import select

    today = date.today().strftime("%Y-%m-%d")
    data = await fetch_assembly_charts(today, today, include_prepared=True)

    # Загрузим имена из БД
    async with async_session_factory() as sess:
        rows = await sess.execute(select(Product.id, Product.name, Product.product_type))
        names = {str(r.id).lower(): r.name for r in rows.all()}

    assembly = data.get("assemblyCharts") or []
    prepared = data.get("preparedCharts") or []

    print(f"assemblyCharts: {len(assembly)}, preparedCharts: {len(prepared)}")
    print()

    # Показываем ключи первой записи
    if assembly:
        first = assembly[0]
        print("=== Ключи в assemblyCharts[0] ===")
        for k, v in first.items():
            if k != "items":
                print(f"  {k}: {v!r}")
        print(f"  items[0] keys: {list(first['items'][0].keys()) if first.get('items') else 'нет'}")
        print()

    if prepared:
        first = prepared[0]
        print("=== Ключи в preparedCharts[0] ===")
        for k, v in first.items():
            if k != "items":
                print(f"  {k}: {v!r}")
        print(f"  items[0] keys: {list(first['items'][0].keys()) if first.get('items') else 'нет'}")
        print()

    # Ищем конкретные блюда по UUID из БД
    targets = ["фиш майо", "шу шоколад", "гренки на цезарь", "говяжья вырезка", "сырники цех"]

    # Создаём обратную карту: name_lower → uuid
    name_to_id = {v.lower(): k for k, v in names.items()}

    print("=== Целевые блюда: ключи assembledProductId ===")
    for hint in targets:
        # Ищем в именах
        found_ids = [uid for name_l, uid in name_to_id.items() if hint in name_l]
        for fid in found_ids[:3]:
            # Ищем в assemblyCharts
            for c in assembly + prepared:
                pid = (c.get("assembledProductId") or "").lower()
                if pid == fid:
                    print(f"\n  {names.get(fid, fid)}")
                    print(f"  UUID={fid}")
                    for k, v in c.items():
                        if k != "items":
                            print(f"    {k}: {v!r}")
                    items = c.get("items") or []
                    print(f"  Ингредиентов: {len(items)}")
                    for it in items[:8]:
                        iid = (it.get("productId") or "").lower()
                        iname = names.get(iid, "???")
                        # Все численные поля
                        num_fields = {k: v for k, v in it.items() if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace('.','').replace('-','').isdigit())}
                        print(f"    {iname}: {num_fields}")
                    break


asyncio.run(main())
