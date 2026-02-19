"""
Тесты: нормализация типов складов (extract_store_type / _normalize_store_type).

Запуск: pytest tests/test_store_type.py -v
"""

import sys
import os

# Добавляем корень проекта в path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from use_cases.product_request import extract_store_type, _normalize_store_type


class TestNormalizeStoreType:
    """Тесты нормализации 'сырых' названий к каноническим типам."""

    def test_bar_simple(self):
        assert _normalize_store_type("бар") == "бар"

    def test_kitchen_simple(self):
        assert _normalize_store_type("кухня") == "кухня"

    def test_tmc_simple(self):
        assert _normalize_store_type("тмц") == "тмц"

    def test_khoz_simple(self):
        assert _normalize_store_type("хозы") == "хозы"

    # Хоз. товары → хозы
    def test_khoz_with_dot(self):
        assert _normalize_store_type("хоз. товары") == "хозы"

    def test_khoz_no_dot(self):
        assert _normalize_store_type("хоз товары") == "хозы"

    def test_khoz_combined(self):
        assert _normalize_store_type("хозтовары") == "хозы"

    def test_khoz_hyphen(self):
        assert _normalize_store_type("хоз-товары") == "хозы"

    def test_khoz_full(self):
        assert _normalize_store_type("хозяйственные товары") == "хозы"

    # ТМЦ с суффиксами → тмц
    def test_tmc_with_suffix(self):
        assert _normalize_store_type("тмц сельма") == "тмц"

    def test_tmc_with_brackets(self):
        assert _normalize_store_type("тмц (московский)") == "тмц"

    # Хоз-prefix
    def test_khoz_prefix(self):
        assert _normalize_store_type("хоз. нужды") == "хозы"

    # Неизвестный тип — возвращаем как есть
    def test_unknown_type(self):
        assert _normalize_store_type("новый склад") == "новый склад"

    def test_empty_string(self):
        assert _normalize_store_type("") == ""


class TestExtractStoreType:
    """Тесты полного флоу extract_store_type (name parsing + normalization)."""

    def test_pizzayolo_kitchen(self):
        assert extract_store_type("PizzaYolo: Кухня (Московский)") == "кухня"

    def test_pizzayolo_bar(self):
        assert extract_store_type("PizzaYolo: Бар (Московский)") == "бар"

    def test_pizzayolo_tmc(self):
        assert extract_store_type("PizzaYolo: ТМЦ (Гайдара)") == "тмц"

    def test_bare_kitchen(self):
        assert extract_store_type("Кухня (Клиническая)") == "кухня"

    def test_tmc_selma(self):
        """ТМЦ Сельма — iiko-реальный кейс из скриншота."""
        assert extract_store_type("ТМЦ Сельма") == "тмц"

    def test_khoz_tovary_moskovsky(self):
        """Хоз. товары (Московский) — iiko-реальный кейс из скриншота."""
        assert extract_store_type("Хоз. товары (Московский)") == "хозы"

    def test_khoz_tovary_klinicheskaya(self):
        assert extract_store_type("Хоз. товары (Клиническая)") == "хозы"

    def test_bar_no_suffix(self):
        assert extract_store_type("Бар") == "бар"

    def test_new_store_unknown(self):
        """'Новый склад' не совпадает ни с одним типом — возвращаем как есть."""
        result = extract_store_type("Новый склад")
        assert result == "новый склад"

    def test_tmc_klinicheskaya(self):
        assert extract_store_type("ТМЦ (Клиническая)") == "тмц"

    def test_brand_prefix_strip(self):
        result = extract_store_type("Гайдара PizzaYolo: ТМЦ Сельма")
        assert result == "тмц"

    def test_whitespace_padding(self):
        assert extract_store_type("  Кухня  ") == "кухня"
