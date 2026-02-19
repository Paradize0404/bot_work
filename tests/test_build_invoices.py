"""
Тесты: build_iiko_invoices — группировка OCR-документов в iiko-накладные.

Запуск: pytest tests/test_build_invoices.py -v
"""

import sys
import os
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from use_cases.incoming_invoice import build_iiko_invoices


# ─────────────────────────────────────────────────────
# Фикстуры
# ─────────────────────────────────────────────────────

DEPT_ID = "ffffffff-0000-0000-0000-000000000001"

# Маппинг type → store  для тестового подразделения
STORE_TYPE_MAP = {
    "кухня": {"id": "aaaaaaaa-0000-0000-0000-000000000001", "name": "Кухня (Московский)"},
    "бар":   {"id": "bbbbbbbb-0000-0000-0000-000000000001", "name": "Бар (Московский)"},
    "тмц":   {"id": "cccccccc-0000-0000-0000-000000000001", "name": "ТМЦ (Московский)"},
    "хозы":  {"id": "dddddddd-0000-0000-0000-000000000001", "name": "Хоз. товары (Московский)"},
}

BASE_MAPPING = {
    "молоко":   {"iiko_id": "11111111-0000-0000-0000-000000000001", "iiko_name": "Молоко 1л", "store_type": "кухня", "type": "товар"},
    "масло":    {"iiko_id": "22222222-0000-0000-0000-000000000001", "iiko_name": "Масло 82%", "store_type": "кухня", "type": "товар"},
    "пиво":     {"iiko_id": "33333333-0000-0000-0000-000000000001", "iiko_name": "Пиво разл.", "store_type": "бар",   "type": "товар"},
    "поставщик ооо": {"iiko_id": "99999999-0000-0000-0000-000000000001", "iiko_name": "ООО Поставщик", "store_type": "", "type": "поставщик"},
}

UNIT_MAP = {
    "11111111-0000-0000-0000-000000000001": "unit-0001",
    "22222222-0000-0000-0000-000000000001": "unit-0001",
    "33333333-0000-0000-0000-000000000001": "unit-0002",
}

SUPPLIER_DB = {
    "ооо поставщик": "99999999-0000-0000-0000-000000000001",
}


def _doc(
    doc_id="doc-001",
    doc_number="УПД-1",
    supplier_name="ООО Поставщик",
    supplier_id="",
    items=None,
    doc_date=None,
) -> dict:
    if items is None:
        items = [
            {"id": "i1", "raw_name": "Молоко", "iiko_id": "11111111-0000-0000-0000-000000000001",
             "iiko_name": "Молоко 1л", "store_type": "кухня", "qty": 5.0, "price": 80.0, "sum": 400.0, "unit": "л"},
        ]
    return {
        "id":            doc_id,
        "doc_number":    doc_number,
        "doc_date":      doc_date or datetime(2026, 2, 15),
        "doc_type":      "upd",
        "supplier_name": supplier_name,
        "supplier_id":   supplier_id,
        "department_id": DEPT_ID,
        "items":         items,
    }


# ─────────────────────────────────────────────────────
# Тесты
# ─────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_single_invoice_single_store(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Один документ, один тип склада → одна накладная."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    docs     = [_doc()]
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    inv = invoices[0]
    assert inv["store_type"]    == "кухня"
    assert inv["storeId"]       == STORE_TYPE_MAP["кухня"]["id"]
    assert inv["store_name"]    == STORE_TYPE_MAP["кухня"]["name"]
    assert inv["dateIncoming"]  == "15.02.2026"
    assert len(inv["items"])    == 1
    assert inv["items"][0]["amount"] == 5.0


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_split_by_store_type(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Один документ с товарами двух складов → две накладные."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    items = [
        {"id": "i1", "raw_name": "Молоко", "iiko_id": "11111111-0000-0000-0000-000000000001",
         "iiko_name": "Молоко 1л", "store_type": "кухня", "qty": 5.0, "price": 80.0, "sum": 400.0, "unit": "л"},
        {"id": "i2", "raw_name": "Пиво", "iiko_id": "33333333-0000-0000-0000-000000000001",
         "iiko_name": "Пиво разл.", "store_type": "бар", "qty": 10.0, "price": 150.0, "sum": 1500.0, "unit": "л"},
    ]
    docs     = [_doc(items=items)]
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 2
    store_types = {inv["store_type"] for inv in invoices}
    assert "кухня" in store_types
    assert "бар"   in store_types

    # Суффиксы добавлены т.к. несколько складов
    for inv in invoices:
        assert "-" in inv["documentNumber"]


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_item_without_iiko_id_skipped(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Товар без iiko_id (и не найден в маппинге) → пропускается с предупреждением."""
    mock_mapping.return_value   = {}  # Пустой маппинг
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = {}
    mock_suppliers.return_value = SUPPLIER_DB

    items = [
        {"id": "i1", "raw_name": "Неизвестный товар", "iiko_id": "",
         "iiko_name": "", "store_type": "кухня", "qty": 1.0, "price": 100.0, "sum": 100.0, "unit": "шт"},
    ]
    docs     = [_doc(items=items)]
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping={})

    # Документ пропущен — в нём нет товаров с iiko_id
    assert len(invoices) == 0
    # Предупреждения должны быть
    assert len(warnings) > 0
    assert any("Неизвестный товар" in w for w in warnings)


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_supplier_resolved_from_db_fallback(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Если supplier_id не задан → резолвим через iiko_supplier по имени."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB  # "ооо поставщик" → UUID

    docs     = [_doc(supplier_id="")]  # supplier_id не задан
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    # supplier_id должен быть резолвлен из БД
    assert invoices[0]["supplierId"] == SUPPLIER_DB["ооо поставщик"]


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_reenrich_item_from_mapping(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Товар без iiko_id в БД → дообогащается из base_mapping."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    items = [
        # iiko_id пустой — должен быть взят из BASE_MAPPING по raw_name
        {"id": "i1", "raw_name": "Молоко", "iiko_id": "",
         "iiko_name": "", "store_type": "", "qty": 5.0, "price": 80.0, "sum": 400.0, "unit": "л"},
    ]
    docs     = [_doc(items=items)]
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    assert invoices[0]["items"][0]["productId"] == "11111111-0000-0000-0000-000000000001"
    assert invoices[0]["store_type"] == "кухня"


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_measure_unit_attached(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """unit_map корректно присваивается каждой позиции."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    docs     = [_doc()]
    invoices, _ = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    item = invoices[0]["items"][0]
    assert item["measureUnitId"] == UNIT_MAP["11111111-0000-0000-0000-000000000001"]


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_empty_docs_returns_empty(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Пустой список документов → пустые накладные без ошибок."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = {}
    mock_suppliers.return_value = {}

    invoices, warnings = await build_iiko_invoices([], DEPT_ID, base_mapping=BASE_MAPPING)
    assert invoices == []
    assert warnings == []


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_no_date_warns_and_uses_today(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Документ без даты → предупреждение, используем сегодня."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = STORE_TYPE_MAP
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    docs = [_doc(doc_date=None)]
    docs[0]["doc_date"] = None  # обнулили
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    assert any("дата не распознана" in w for w in warnings)
    # dateIncoming должна быть в формате dd.mm.YYYY
    import re
    assert re.match(r"\d{2}\.\d{2}\.\d{4}", invoices[0]["dateIncoming"])


@pytest.mark.asyncio
@patch("use_cases.incoming_invoice._load_supplier_ids_from_db", new_callable=AsyncMock)
@patch("use_cases.incoming_invoice._load_product_units", new_callable=AsyncMock)
@patch("use_cases.product_request.build_store_type_map", new_callable=AsyncMock)
@patch("use_cases.ocr_mapping.get_base_mapping", new_callable=AsyncMock)
async def test_unknown_store_type_warns(
    mock_mapping, mock_store_map, mock_units, mock_suppliers
):
    """Неизвестный store_type → предупреждение, накладная всё равно создаётся."""
    mock_mapping.return_value   = BASE_MAPPING
    mock_store_map.return_value = {}  # Пустой маппинг → ни один тип не найден
    mock_units.return_value     = UNIT_MAP
    mock_suppliers.return_value = SUPPLIER_DB

    docs     = [_doc()]  # items со store_type="кухня", но store_type_map пуст
    invoices, warnings = await build_iiko_invoices(docs, DEPT_ID, base_mapping=BASE_MAPPING)

    assert len(invoices) == 1
    assert invoices[0]["storeId"] == ""  # не найден
    assert any("кухня" in w for w in warnings)
