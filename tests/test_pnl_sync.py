"""
Тесты для use_cases/pnl_sync.py — WRITEOFF-логика и агрегация.

Запуск:
    pytest tests/test_pnl_sync.py -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from collections import defaultdict


# ═══════════════════════════════════════════════════════
# Тест 1: fetch_iiko_accounts_from_preset — фильтр value <= 0
# ═══════════════════════════════════════════════════════


# Сырые строки как они приходят из iiko OLAP по пресету
OLAP_RAW_ROWS = [
    # INVOICE — нормальная строка
    {
        "Account.Name": "Кухня (Клиническая)",
        "Department": "Клиническая PizzaYolo",
        "Product.SecondParent": "Молоко/Яйца",
        "Sum.Incoming": "10000",
        "TransactionType": "INVOICE",
    },
    # INVOICE — ещё
    {
        "Account.Name": "Бар (Московский)",
        "Department": "PizzaYolo / Пицца Йоло (Московский)",
        "Product.SecondParent": "Алкоголь",
        "Sum.Incoming": "5000",
        "TransactionType": "INVOICE",
    },
    # WRITEOFF — Sum.Incoming > 0 (должен пройти)
    {
        "Account.Name": "Кухня (Клиническая)",
        "Department": "Клиническая PizzaYolo",
        "Product.SecondParent": "Молоко/Яйца",
        "Sum.Incoming": "1500",
        "TransactionType": "WRITEOFF",
    },
    # WRITEOFF — Sum.Incoming = 0, но Sum.Outgoing > 0 (должен пройти через фолбэк!)
    {
        "Account.Name": "Кухня (Клиническая)",
        "Department": "Клиническая PizzaYolo",
        "Product.SecondParent": "Сыр",
        "Sum.Incoming": "0",
        "Sum.Outgoing": "800",
        "TransactionType": "WRITEOFF",
    },
    # WRITEOFF — все суммы = 0 — отброшен (правильно)
    {
        "Account.Name": "Кухня (Клиническая)",
        "Department": "Клиническая PizzaYolo",
        "Product.SecondParent": "Мусор",
        "Sum.Incoming": "0",
        "TransactionType": "WRITEOFF",
    },
    # INCOMING_SERVICE
    {
        "Account.Name": "Доставка (курьер)",
        "Department": "Клиническая PizzaYolo",
        "Sum.Incoming": "3000",
        "TransactionType": "INCOMING_SERVICE",
    },
    # Задолженность — Sum.Incoming = 0, не WRITEOFF → отброшена
    {
        "Account.Name": "Задолженность перед поставщиками",
        "Department": "Клиническая PizzaYolo",
        "Sum.Incoming": "0",
        "TransactionType": "INVOICE",
    },
]


@pytest.mark.asyncio
async def test_fetch_filters_correctly():
    """Проверяем: WRITEOFF проходит через фильтр (включая фолбэк на Sum.Outgoing)."""
    with patch("use_cases.pnl_sync.iiko_api") as mock_iiko:
        mock_iiko.fetch_olap_by_preset = AsyncMock(return_value=OLAP_RAW_ROWS)

        from use_cases.pnl_sync import fetch_iiko_accounts_from_preset

        rows = await fetch_iiko_accounts_from_preset(
            "2026-03-01T00:00:00", "2026-04-01T00:00:00"
        )

    # Должны пройти: 10000 INVOICE, 5000 INVOICE, 1500 WRITEOFF, 800 WRITEOFF(outgoing), 3000 INCOMING_SERVICE
    assert (
        len(rows) == 5
    ), f"Ожидали 5 строк, получили {len(rows)}: {[r['account_name'] + '/' + str(r['transaction_type']) + '/' + str(r['sum']) for r in rows]}"

    writeoff_rows = [r for r in rows if r["transaction_type"] == "WRITEOFF"]
    assert len(writeoff_rows) == 2, f"Должно быть 2 WRITEOFF, получили: {writeoff_rows}"
    assert writeoff_rows[0]["sum"] == 1500.0
    assert writeoff_rows[1]["sum"] == 800.0, "Sum.Outgoing фолбэк не сработал"


# ═══════════════════════════════════════════════════════
# Тест 2: Агрегация — WRITEOFF вычитается из исходной категории
# ═══════════════════════════════════════════════════════


# Данные после fetch_iiko_accounts_from_preset (уже отфильтрованные)
PARSED_ROWS = [
    {
        "account_name": "Кухня (Клиническая)",
        "product_group": "Молоко/Яйца",
        "department": "Клиническая PizzaYolo",
        "transaction_type": "INVOICE",
        "sum": 10000.0,
    },
    {
        "account_name": "Бар (Московский)",
        "product_group": "Алкоголь",
        "department": "PizzaYolo / Пицца Йоло (Московский)",
        "transaction_type": "INVOICE",
        "sum": 5000.0,
    },
    {
        "account_name": "Кухня (Клиническая)",
        "product_group": "Молоко/Яйца",
        "department": "Клиническая PizzaYolo",
        "transaction_type": "WRITEOFF",
        "sum": 1500.0,
    },
    {
        "account_name": "Доставка (курьер)",
        "product_group": None,
        "department": "Клиническая PizzaYolo",
        "transaction_type": "INCOMING_SERVICE",
        "sum": 3000.0,
    },
]


# Маппинг: группа/счёт → FT-категория
MOCK_MAPPINGS = [
    # Группа "Молоко/Яйца" → "Сырьевая себестоимость" (id=100)
    {
        "iiko_account_name": "[Группа] Молоко/Яйца",
        "ft_pnl_category_id": 100,
        "ft_pnl_category_name": "Сырьевая себестоимость",
    },
    # Счёт "Бар (Московский)" → "Сырьевая себестоимость" (id=100)
    {
        "iiko_account_name": "Бар (Московский)",
        "ft_pnl_category_id": 100,
        "ft_pnl_category_name": "Сырьевая себестоимость",
    },
    # Группа "Алкоголь" → "Сырьевая себестоимость" (id=100)
    {
        "iiko_account_name": "[Группа] Алкоголь",
        "ft_pnl_category_id": 100,
        "ft_pnl_category_name": "Сырьевая себестоимость",
    },
    # Счёт "Доставка (курьер)" → "Доставка (курьер)" (id=200)
    {
        "iiko_account_name": "Доставка (курьер)",
        "ft_pnl_category_id": 200,
        "ft_pnl_category_name": "Доставка (курьер)",
    },
]


def test_aggregation_writeoff_deduction():
    """
    WRITEOFF должен:
    1. Добавиться в «Списания продуктов» (writeoff_cat_id=999)
    2. Вычесться из «Сырьевая себестоимость» (cat_id=100) — потому что
       маппинг для [Группа] Молоко/Яйца → 100

    Итого:
    - cat=100 (Сырьевая себест.): INVOICE 10000 + 5000 = 15000, WRITEOFF -1500 → 13500
    - cat=999 (Списания): WRITEOFF 1500
    - cat=200 (Доставка): 3000
    """
    mapping_index = {
        m["iiko_account_name"]: (m["ft_pnl_category_id"], m["ft_pnl_category_name"])
        for m in MOCK_MAPPINGS
    }
    writeoff_cat_id = 999
    direction_map = {"Клиническая": 1, "Московский": 2}
    dept_dir_map = [
        {"dept_name": "Клиническая", "ft_direction_name": "Клиническая"},
        {"dept_name": "Московский", "ft_direction_name": "Московский"},
    ]

    # Импортируем нормализатор
    from use_cases.pnl_sync import _resolve_department_direction

    ft_totals = defaultdict(float)
    ft_names = {}
    unmapped_keys = set()
    unmapped_sums = defaultdict(float)
    writeoff_deductions = defaultdict(float)

    for row in PARSED_ROWS:
        value = row["sum"]
        account_name = row["account_name"]
        product_group = row["product_group"]
        department = row["department"]
        tx_type = (row["transaction_type"] or "").upper()

        dir_name = (
            _resolve_department_direction(department, dept_dir_map)
            if department
            else None
        )
        dir_id = direction_map.get(dir_name) if dir_name else None

        # Стандартный маппинг
        mapped_cat_id = None
        mapped_cat_name = ""
        if product_group:
            group_key = f"[Группа] {product_group}"
            if group_key in mapping_index:
                mapped_cat_id, mapped_cat_name = mapping_index[group_key]
        if mapped_cat_id is None and account_name in mapping_index:
            mapped_cat_id, mapped_cat_name = mapping_index[account_name]

        if tx_type == "WRITEOFF":
            # 1. Записываем в Списания
            ft_totals[(writeoff_cat_id, dir_id)] += value
            ft_names[writeoff_cat_id] = "Списания продуктов"
            # 2. Вычитаем из исходной категории
            if mapped_cat_id is not None:
                writeoff_deductions[(mapped_cat_id, dir_id)] += value
                ft_names[mapped_cat_id] = mapped_cat_name
            continue

        if mapped_cat_id is None:
            unmapped_keys.add(account_name)
            unmapped_sums[account_name] += value
            continue

        ft_totals[(mapped_cat_id, dir_id)] += value
        ft_names[mapped_cat_id] = mapped_cat_name

    # Применяем вычеты
    for key, deduction in writeoff_deductions.items():
        if key in ft_totals:
            ft_totals[key] -= deduction

    # Округляем
    for key in ft_totals:
        ft_totals[key] = round(ft_totals[key], 2)

    # Убираем нулевые
    ft_totals = {k: v for k, v in ft_totals.items() if v > 0.005}

    # ── Проверки ──
    # У нас direction_id=1 для "Клиническая..."
    print("\nft_totals:")
    for k, v in ft_totals.items():
        print(f"  {k} → {v}  ({ft_names.get(k[0], '?')})")

    # Сырьевая себестоимость (cat=100):
    #   - dir=1 (Клиническая): INVOICE 10000, WRITEOFF deducted -1500 → 8500
    #   - dir=2 (Московский): INVOICE 5000 → 5000
    assert (100, 1) in ft_totals, f"Cat 100 dir 1 не найден: {ft_totals}"
    assert (
        ft_totals[(100, 1)] == 8500.0
    ), f"Cat 100 dir 1 == {ft_totals[(100, 1)]}, ожидали 8500"
    assert (100, 2) in ft_totals
    assert ft_totals[(100, 2)] == 5000.0

    # Списания (cat=999, dir=1): 1500
    assert (999, 1) in ft_totals
    assert ft_totals[(999, 1)] == 1500.0

    # Доставка (cat=200, dir=1): 3000
    assert (200, 1) in ft_totals
    assert ft_totals[(200, 1)] == 3000.0

    print("\n✅ WRITEOFF-вычет работает корректно!")


# ═══════════════════════════════════════════════════════
# Тест 3: _normalize_dept и _resolve_department_direction
# ═══════════════════════════════════════════════════════


def test_normalize_dept_matching():
    """Проверяем что маппинг 'Клиническая' совпадает с 'Клиническая PizzaYolo'."""
    from use_cases.pnl_sync import _normalize_dept, _resolve_department_direction

    # Нормализация
    assert _normalize_dept("Клиническая PizzaYolo") == "клиническая"
    assert _normalize_dept("PizzaYolo / Пицца Йоло (Московский)") == "московский"
    assert _normalize_dept("Клиническая") == "клиническая"
    assert _normalize_dept("Московский") == "московский"

    dept_dir_map = [
        {"dept_name": "Клиническая", "ft_direction_name": "Клиническая"},
        {"dept_name": "Московский", "ft_direction_name": "Московский"},
    ]

    # Точное совпадение
    assert _resolve_department_direction("Клиническая", dept_dir_map) == "Клиническая"
    assert _resolve_department_direction("Московский", dept_dir_map) == "Московский"

    # OLAP-вид → должен совпасть через нормализацию
    result_klin = _resolve_department_direction("Клиническая PizzaYolo", dept_dir_map)
    assert result_klin == "Клиническая", f"Got: {result_klin}"

    result_mosk = _resolve_department_direction(
        "PizzaYolo / Пицца Йоло (Московский)", dept_dir_map
    )
    assert result_mosk == "Московский", f"Got: {result_mosk}"

    print("✅ Department matching работает!")


# ═══════════════════════════════════════════════════════
# Тест 4: Проверяем что Sum.Incoming для WRITEOFF может быть в разных форматах
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_writeoff_sum_fields():
    """
    Проверяем что WRITEOFF строки корректно парсятся.

    OLAP может вернуть сумму в разных полях. Нас интересует Sum.Incoming.
    Для списаний (WRITEOFF) Sum.Incoming это сумма списания — она должна быть > 0.
    """
    writeoff_rows = [
        # Нормальный WRITEOFF с положительной суммой
        {
            "Account.Name": "Кухня (Клиническая)",
            "Department": "Клиническая PizzaYolo",
            "Product.SecondParent": "Молоко/Яйца",
            "Sum.Incoming": "2500.50",
            "TransactionType": "WRITEOFF",
        },
        # WRITEOFF — Sum.Incoming пуст, но Sum.Outgoing есть → фолбэк
        {
            "Account.Name": "Кухня (Клиническая)",
            "Department": "Клиническая PizzaYolo",
            "Product.SecondParent": "Сыр",
            "Sum.Incoming": "",
            "Sum.Outgoing": "1200",
            "TransactionType": "WRITEOFF",
        },
        # WRITEOFF — все суммы пусты → пропускается
        {
            "Account.Name": "Кухня (Клиническая)",
            "Department": "Клиническая PizzaYolo",
            "Product.SecondParent": "Грибы",
            "TransactionType": "WRITEOFF",
        },
    ]

    with patch("use_cases.pnl_sync.iiko_api") as mock_iiko:
        mock_iiko.fetch_olap_by_preset = AsyncMock(return_value=writeoff_rows)

        from use_cases.pnl_sync import fetch_iiko_accounts_from_preset

        rows = await fetch_iiko_accounts_from_preset(
            "2026-03-01T00:00:00", "2026-04-01T00:00:00"
        )

    # Должны пройти: 2500.50 (Sum.Incoming) и 1200 (Sum.Outgoing фолбэк)
    assert len(rows) == 2, f"Ожидали 2 строки, получили {len(rows)}: {rows}"
    assert rows[0]["transaction_type"] == "WRITEOFF"
    assert rows[0]["sum"] == 2500.50
    assert (
        rows[1]["sum"] == 1200.0
    ), f"Sum.Outgoing фолбэк: ожидали 1200, получили {rows[1]['sum']}"

    print("✅ WRITEOFF sum parsing OK!")


# ═══════════════════════════════════════════════════════
# Тест 5: end-to-end update_opiu (mock all externals)
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_opiu_writeoff_e2e():
    """
    End-to-end тест: update_opiu должен:
    1. Записать WRITEOFF в «Списания продуктов»
    2. Вычесть из «Сырьевая себестоимость»
    """
    from datetime import datetime

    olap_rows = [
        # INVOICE → Сырьевая себестоимость
        {
            "Account.Name": "Кухня (Клиническая)",
            "Department": "Клиническая PizzaYolo",
            "Product.SecondParent": "Молоко/Яйца",
            "Sum.Incoming": "10000",
            "TransactionType": "INVOICE",
        },
        # WRITEOFF → Списания + вычет из Сырьевой
        {
            "Account.Name": "Кухня (Клиническая)",
            "Department": "Клиническая PizzaYolo",
            "Product.SecondParent": "Молоко/Яйца",
            "Sum.Incoming": "2000",
            "TransactionType": "WRITEOFF",
        },
    ]

    mock_mappings = {
        "opiu": [
            {
                "iiko_account_name": "Кухня (Клиническая)",
                "ft_pnl_category_id": 100,
                "ft_pnl_category_name": "Сырьевая себестоимость",
            },
        ],
        "dept_direction": [
            {"dept_name": "Клиническая", "ft_direction_name": "Клиническая"},
        ],
    }

    created_items = []

    async def mock_create_pnl_item(
        category_id, value, date_mm_yyyy, *, comment=None, direction_id=None, nds=None
    ):
        created_items.append(
            {
                "category_id": category_id,
                "value": value,
                "date": date_mm_yyyy,
                "comment": comment,
                "direction_id": direction_id,
            }
        )
        return {"id": len(created_items)}

    with (
        patch("use_cases.pnl_sync.iiko_api") as mock_iiko,
        patch(
            "use_cases.pnl_sync.read_fintab_all_mappings",
            new_callable=AsyncMock,
            return_value=mock_mappings,
        ),
        patch(
            "use_cases.pnl_sync._get_writeoff_category_id",
            new_callable=AsyncMock,
            return_value=999,
        ),
        patch(
            "use_cases.pnl_sync._get_direction_map",
            new_callable=AsyncMock,
            return_value={"Клиническая": 1},
        ),
        patch("use_cases.pnl_sync.fintablo_api") as mock_ft,
    ):
        mock_iiko.fetch_olap_by_preset = AsyncMock(return_value=olap_rows)
        mock_ft.fetch_pnl_items = AsyncMock(return_value=[])  # нет текущих записей
        mock_ft.delete_pnl_item = AsyncMock()
        mock_ft.create_pnl_item = mock_create_pnl_item

        from use_cases.pnl_sync import update_opiu

        result = await update_opiu(
            triggered_by="test",
            target_date=datetime(2026, 3, 15),
        )

    print(f"\nresult = {result}")
    print(f"\ncreated_items = {created_items}")

    # Должно быть 2 записи:
    # 1. Сырьевая себестоимость (cat=100) = 10000 - 2000 = 8000
    # 2. Списания продуктов (cat=999) = 2000
    assert result["errors"] == 0, f"Errors: {result['errors']}"
    assert result["updated"] == 2, f"Expected 2 updates, got {result['updated']}"
    assert (
        result["writeoff_deducted"] == 2000.0
    ), f"writeoff_deducted = {result['writeoff_deducted']}"

    cat_100_items = [it for it in created_items if it["category_id"] == 100]
    cat_999_items = [it for it in created_items if it["category_id"] == 999]

    assert len(cat_100_items) == 1, f"Expected 1 item for cat=100, got {cat_100_items}"
    assert (
        cat_100_items[0]["value"] == 8000.0
    ), f"Сырьевая = {cat_100_items[0]['value']}, ожидали 8000"

    assert len(cat_999_items) == 1, f"Expected 1 item for cat=999, got {cat_999_items}"
    assert (
        cat_999_items[0]["value"] == 2000.0
    ), f"Списания = {cat_999_items[0]['value']}, ожидали 2000"

    print("\n✅ E2E: WRITEOFF записан в Списания и вычтен из Сырьевой!")


if __name__ == "__main__":
    # Запуск без pytest
    print("=" * 60)
    print("  Тест 1: _normalize_dept + _resolve_department_direction")
    print("=" * 60)
    test_normalize_dept_matching()

    print("\n" + "=" * 60)
    print("  Тест 2: Агрегация WRITEOFF-вычетов")
    print("=" * 60)
    test_aggregation_writeoff_deduction()

    print("\n" + "=" * 60)
    print("  Тест 3: fetch filter")
    print("=" * 60)
    asyncio.run(test_fetch_filters_correctly())

    print("\n" + "=" * 60)
    print("  Тест 4: WRITEOFF sum fields")
    print("=" * 60)
    asyncio.run(test_writeoff_sum_fields())

    print("\n" + "=" * 60)
    print("  Тест 5: E2E update_opiu")
    print("=" * 60)
    asyncio.run(test_update_opiu_writeoff_e2e())

    print("\n\n✅✅✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
