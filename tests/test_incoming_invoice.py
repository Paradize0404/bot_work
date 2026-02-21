"""
Тесты: подготовка и форматирование приходных накладных.

Запуск: pytest tests/test_incoming_invoice.py -v
"""

import sys
import os
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from use_cases.incoming_invoice import (
    _store_suffix,
    format_invoice_preview,
    format_send_result,
)


# ─────────────────────────────────────────────────────
# Хелперы — готовые словари
# ─────────────────────────────────────────────────────


def _make_invoice(
    doc_id="doc-001",
    doc_number="УПД-1",
    store_type="кухня",
    store_name="Кухня (Московский)",
    store_id="11111111-0000-0000-0000-000000000000",
    supplier_name="ООО Поставщик",
    supplier_id="22222222-0000-0000-0000-000000000000",
    items=None,
) -> dict:
    if items is None:
        items = [
            {
                "productId": "aaaaaaaa-0000-0000-0000-000000000001",
                "raw_name": "Молоко",
                "iiko_name": "Молоко 3.2% 1л",
                "amount": 10.0,
                "price": 80.0,
                "sum": 800.0,
                "measureUnitId": "bbbbbbbb-0000-0000-0000-000000000001",
            }
        ]
    return {
        "ocr_doc_id": doc_id,
        "ocr_doc_number": doc_number,
        "documentNumber": f"{doc_number}-КУХ",
        "dateIncoming": "15.02.2026",
        "supplierId": supplier_id,
        "supplier_name": supplier_name,
        "storeId": store_id,
        "store_name": store_name,
        "store_type": store_type,
        "items": items,
    }


# ─────────────────────────────────────────────────────
# Tests: _store_suffix
# ─────────────────────────────────────────────────────


class TestStoreSuffix:
    def test_bar(self):
        assert _store_suffix("бар") == "БАР"

    def test_kitchen(self):
        assert _store_suffix("кухня") == "КУХ"

    def test_tmc(self):
        assert _store_suffix("тмц") == "ТМЦ"

    def test_khoz(self):
        assert _store_suffix("хозы") == "ХОЗ"

    def test_unknown(self):
        # Неизвестный тип → первые 3 буквы upper
        result = _store_suffix("магазин")
        assert result == "МАГ"

    def test_empty(self):
        result = _store_suffix("")
        assert result == "СКЛ"


# ─────────────────────────────────────────────────────
# Tests: format_invoice_preview
# ─────────────────────────────────────────────────────


class TestFormatInvoicePreview:
    def test_single_invoice(self):
        inv = _make_invoice()
        text = format_invoice_preview([inv])
        assert "📦" in text
        assert "УПД-1-КУХ" in text
        assert "15.02.2026" in text
        assert "Кухня (Московский)" in text
        assert "ООО Поставщик" in text
        assert "800" in text

    def test_empty_invoices(self):
        text = format_invoice_preview([])
        assert "⚠️" in text
        assert "Нет накладных" in text

    def test_warnings_shown(self):
        inv = _make_invoice()
        text = format_invoice_preview([inv], warnings=["Тест предупреждения"])
        assert "Тест предупреждения" in text
        assert "⚠️" in text

    def test_many_warnings_truncated(self):
        inv = _make_invoice()
        warnings = [f"Предупреждение {i}" for i in range(20)]
        text = format_invoice_preview([inv], warnings=warnings)
        # Не должно выводить все 20
        assert "и ещё" in text

    def test_multiple_invoices(self):
        inv1 = _make_invoice(doc_id="doc-001", doc_number="УПД-1")
        inv2 = _make_invoice(
            doc_id="doc-002",
            doc_number="УПД-2",
            store_type="бар",
            store_name="Бар (Московский)",
        )
        text = format_invoice_preview([inv1, inv2])
        assert "1." in text
        assert "2." in text
        assert "Кухня" in text
        assert "Бар" in text

    def test_footer_prompt(self):
        inv = _make_invoice()
        text = format_invoice_preview([inv])
        assert "Отправить в iiko" in text


# ─────────────────────────────────────────────────────
# Tests: format_send_result
# ─────────────────────────────────────────────────────


class TestFormatSendResult:
    def test_all_success(self):
        inv = _make_invoice()
        results = [{"invoice": inv, "ok": True, "error": ""}]
        text = format_send_result(results)
        assert "✅" in text
        assert "1" in text
        assert "❌" not in text

    def test_all_fail(self):
        inv = _make_invoice()
        results = [{"invoice": inv, "ok": False, "error": "Сервер недоступен"}]
        text = format_send_result(results)
        assert "❌" in text
        assert "Сервер недоступен" in text
        assert "✅" not in text

    def test_mixed(self):
        inv1 = _make_invoice(doc_number="УПД-1")
        inv2 = _make_invoice(
            doc_number="УПД-2", store_type="бар", store_name="Бар (Московский)"
        )
        results = [
            {"invoice": inv1, "ok": True, "error": ""},
            {"invoice": inv2, "ok": False, "error": "Неверный UUID склада"},
        ]
        text = format_send_result(results)
        assert "✅" in text
        assert "❌" in text
        assert "Неверный UUID склада" in text


# ─────────────────────────────────────────────────────
# Tests: XML builder (из adapters/iiko_api.py)
# ─────────────────────────────────────────────────────


class TestBuildIncomingInvoiceXml:
    """Тесты генерации XML-документа приходной накладной."""

    def _build(self, document: dict) -> ET.Element:
        from adapters.iiko_api import _build_incoming_invoice_xml

        xml_str = _build_incoming_invoice_xml(document)
        assert xml_str.startswith("<?xml")
        return ET.fromstring(xml_str.split("\n", 1)[1])  # пропускаем XML-декларацию

    def _make_doc(self, **kwargs) -> dict:
        base = {
            "documentNumber": "УПД-1-КУХ",
            "dateIncoming": "15.02.2026",
            "status": "NEW",
            "storeId": "11111111-0000-0000-0000-000000000000",
            "supplierId": "22222222-0000-0000-0000-000000000000",
            "items": [
                {
                    "productId": "aaaaaaaa-0000-0000-0000-000000000001",
                    "amount": 10.0,
                    "price": 80.0,
                    "sum": 800.0,
                    "measureUnitId": "bbbbbbbb-0000-0000-0000-000000000001",
                }
            ],
        }
        base.update(kwargs)
        return base

    def test_root_element(self):
        root = self._build(self._make_doc())
        assert root.tag == "document"

    def test_document_number(self):
        root = self._build(self._make_doc())
        assert root.findtext("documentNumber") == "УПД-1-КУХ"

    def test_date_incoming(self):
        root = self._build(self._make_doc())
        assert root.findtext("dateIncoming") == "15.02.2026"

    def test_store(self):
        root = self._build(self._make_doc())
        assert root.findtext("defaultStore") == "11111111-0000-0000-0000-000000000000"

    def test_supplier(self):
        root = self._build(self._make_doc())
        assert root.findtext("supplier") == "22222222-0000-0000-0000-000000000000"

    def test_single_item(self):
        root = self._build(self._make_doc())
        items_el = root.find("items")
        assert items_el is not None
        item_els = items_el.findall("item")
        assert len(item_els) == 1

    def test_item_product(self):
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert item.findtext("product") == "aaaaaaaa-0000-0000-0000-000000000001"

    def test_item_amount(self):
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert float(item.findtext("amount")) == 10.0

    def test_item_price(self):
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert float(item.findtext("price")) == 80.0

    def test_item_sum(self):
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert float(item.findtext("sum")) == 800.0

    def test_item_amount_unit(self):
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert item.findtext("amountUnit") == "bbbbbbbb-0000-0000-0000-000000000001"

    def test_item_num(self):
        """Номер позиции начинается с 1."""
        root = self._build(self._make_doc())
        item = root.find("items/item")
        assert item.findtext("num") == "1"

    def test_multiple_items(self):
        doc = self._make_doc(
            items=[
                {
                    "productId": "aaa",
                    "amount": 1.0,
                    "price": 100.0,
                    "sum": 100.0,
                    "measureUnitId": "",
                },
                {
                    "productId": "bbb",
                    "amount": 2.0,
                    "price": 200.0,
                    "sum": 400.0,
                    "measureUnitId": "",
                },
            ]
        )
        root = self._build(doc)
        items = root.findall("items/item")
        assert len(items) == 2
        assert items[0].findtext("num") == "1"
        assert items[1].findtext("num") == "2"

    def test_no_amount_unit_omitted(self):
        """Если measureUnitId пуст — тег amountUnit не должен добавляться."""
        doc = self._make_doc(
            items=[
                {
                    "productId": "aaa",
                    "amount": 1.0,
                    "price": 100.0,
                    "sum": 100.0,
                    "measureUnitId": "",
                },
            ]
        )
        root = self._build(doc)
        item = root.find("items/item")
        assert item.find("amountUnit") is None

    def test_comment_added(self):
        doc = self._make_doc(comment="Тестовый комментарий")
        root = self._build(doc)
        assert root.findtext("comment") == "Тестовый комментарий"

    def test_status_new(self):
        root = self._build(self._make_doc())
        assert root.findtext("status") == "NEW"

    def test_auto_document_number_if_missing(self):
        """Если documentNumber не передан — генерируем INC-xxxxxxxx."""
        doc = self._make_doc()
        del doc["documentNumber"]
        root = self._build(doc)
        doc_num = root.findtext("documentNumber") or ""
        assert doc_num.startswith("INC-")
        assert len(doc_num) > 4
