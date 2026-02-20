"""
Тест авто-перемещения расходных материалов.
Запускать:
  python test_negative_transfer.py           — только диагностика (не отправляет в iiko)
  python test_negative_transfer.py --execute — реальный запуск (отправит перемещения в iiko!)
"""
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env (если есть), стабируем необязательные для теста переменные
load_dotenv(Path(__file__).resolve().parent / ".env")
for _stub in ("REDIS_URL", "IIKO_CLOUD_WEBHOOK_SECRET", "BOT_TOKEN", "FINTABLO_API_KEY"):
    if not os.environ.get(_stub):
        os.environ[_stub] = "stub"

EXECUTE = "--execute" in sys.argv


async def main():
    print("=" * 60)
    print(f"ТЕСТ АВТО-ПЕРЕМЕЩЕНИЯ (execute={EXECUTE})")
    print("=" * 60)

    # ── 1. Склады из БД ──────────────────────────────────────────
    print("\n[1] Загружаем склады из БД…")
    from use_cases.negative_transfer import _load_stores, _build_restaurant_map
    from config import (
        NEGATIVE_TRANSFER_SOURCE_PREFIX,
        NEGATIVE_TRANSFER_TARGET_PREFIXES,
        NEGATIVE_TRANSFER_PRODUCT_GROUP,
    )

    stores = await _load_stores()  # list[tuple[str, str]]  (id, name)
    print(f"    Складов всего: {len(stores)}")
    for sid, sname in sorted(stores, key=lambda x: x[1]):
        print(f"      • {sname}")

    # ── 2. Карта ресторанов ──────────────────────────────────────
    print(f"\n[2] Строим карту ресторанов…")
    print(f"    SOURCE prefix : '{NEGATIVE_TRANSFER_SOURCE_PREFIX}'")
    print(f"    TARGET prefixes: '{NEGATIVE_TRANSFER_TARGET_PREFIXES}'")
    target_prefixes = [p.strip() for p in NEGATIVE_TRANSFER_TARGET_PREFIXES.split(",")]
    rest_map = _build_restaurant_map(stores, NEGATIVE_TRANSFER_SOURCE_PREFIX, target_prefixes)

    if not rest_map:
        print("    ❌ Карта пуста — проверьте названия складов в БД!")
        return

    print(f"    Найдено ресторанов: {len(rest_map)}")
    for rest, data in sorted(rest_map.items()):
        src_id, src_name = data["source"]
        print(f"      {rest}:")
        print(f"        source  → {src_name}  [{src_id[:8]}…]")
        for t_id, t_name in data["targets"]:
            print(f"        target  → {t_name}  [{t_id[:8]}…]")

    # ── 3. OLAP-запрос ──────────────────────────────────────────
    print("\n[3] OLAP-запрос v1 за сегодня…")
    from adapters import iiko_api
    today = date.today().strftime("%d.%m.%Y")
    print(f"    Дата: {today}")
    rows = await iiko_api.fetch_olap_transactions_v1(today, today)
    print(f"    Получено строк: {len(rows)}")
    if rows:
        print(f"    Поля: {list(rows[0].keys())}")
        print(f"    Пример: {json.dumps(rows[0], ensure_ascii=False)}")

    # ── 4. Отрицательные позиции ────────────────────────────────
    print(f"\n[4] Фильтруем по группе '{NEGATIVE_TRANSFER_PRODUCT_GROUP}'…")
    from use_cases.negative_transfer import _collect_negative_items

    all_target_names = {
        t_name
        for data in rest_map.values()
        for _, t_name in data["targets"]
    }
    print(f"    Target склады: {sorted(all_target_names)}")

    items = _collect_negative_items(rows, all_target_names, NEGATIVE_TRANSFER_PRODUCT_GROUP)
    print(f"    Отрицательных позиций: {sum(len(v) for v in items.values())} по {len(items)} складам")

    for store_name, products in sorted(items.items()):
        print(f"      {store_name} ({len(products)} поз.):")
        for p in products[:8]:
            print(f"        − {p['product_name']}: {p['amount']}")
        if len(products) > 8:
            print(f"        … ещё {len(products) - 8}")

    if not items:
        print("\n    ℹ️  Нет отрицательных остатков — нечего перемещать")
        print("\n✅ Диагностика завершена (данных для перемещения нет)")
        return

    # ── 5. Резолв товаров из БД ──────────────────────────────────
    print(f"\n[5] Загружаем товары из БД…")
    from use_cases.negative_transfer import _load_products_by_name

    all_names = {p["product_name"] for prods in items.values() for p in prods}
    product_map = await _load_products_by_name(all_names)
    missing = all_names - set(product_map.keys())
    print(f"    Найдено: {len(product_map)} / {len(all_names)}")
    if missing:
        print(f"    ❌ Не найдены в БД ({len(missing)}):")
        for n in sorted(missing):
            print(f"      • {n}")

    # ── 6. Preview или реальный запуск ──────────────────────────
    if not EXECUTE:
        print(f"\n[6] PREVIEW документов (без отправки)…")
        for rest, data in sorted(rest_map.items()):
            src_id, src_name = data["source"]
            for tgt_id, tgt_name in data["targets"]:
                neg = items.get(tgt_name, [])
                if not neg:
                    continue
                transfer_items = [
                    {
                        "productId": product_map[p["product_name"]]["id"],
                        "amount": round(p["amount"], 6),
                        "measureUnitId": product_map[p["product_name"]].get("main_unit") or "",
                    }
                    for p in neg
                    if p["product_name"] in product_map
                       and product_map[p["product_name"]].get("main_unit")
                ]
                if not transfer_items:
                    print(f"      {rest} ({tgt_name}): нет позиций с main_unit — пропуск")
                    continue
                print(f"\n      {rest}: {src_name} → {tgt_name}  ({len(transfer_items)} поз.)")
                print(f"      Документ (preview):")
                doc = {
                    "storeFromId": src_id,
                    "storeToId":   tgt_id,
                    "status":      "PROCESSED",
                    "items":       transfer_items,
                }
                print("      " + json.dumps(doc, ensure_ascii=False, indent=2).replace("\n", "\n      "))

        print("\n✅ Preview завершён. Для реального запуска: python test_negative_transfer.py --execute")

    else:
        print(f"\n[6] РЕАЛЬНЫЙ ЗАПУСК через run_negative_transfer_once…")
        from use_cases.negative_transfer import run_negative_transfer_once
        result = await run_negative_transfer_once(triggered_by="test_script")
        print(f"\nРезультат:")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

