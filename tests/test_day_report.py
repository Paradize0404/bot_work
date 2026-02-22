"""
Тесты: отчёт дня (use_cases/day_report.py + adapters/google_sheets.append_day_report_row).

Запуск: pytest tests/test_day_report.py -v
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from use_cases.day_report import (
    fetch_day_report_data,
    format_day_report,
    DayReportData,
    SalesLine,
    CostLine,
)


# ═══════════════════════════════════════════════════════
# Вспомогательные фикстуры
# ═══════════════════════════════════════════════════════

DEPT_ID = "aaaaaaaa-0000-0000-0000-000000000001"

# Реальная структура ответа iiko: каждая строка содержит ОБА поля +
# поле Department с полным путём подразделения
SAMPLE_ROWS = [
    # Московский филиал
    {
        "Department": "PizzaYolo / Пицца Йоло (Московский)",
        "CookingPlaceType": "Кухня",
        "PayTypes": "Наличные",
        "DishDiscountSumInt": 10000.0,
        "ProductCostBase.ProductCost": 3000.0,
    },
    {
        "Department": "PizzaYolo / Пицца Йоло (Московский)",
        "CookingPlaceType": "Бар",
        "PayTypes": "Наличные",
        "DishDiscountSumInt": 2000.0,
        "ProductCostBase.ProductCost": 400.0,
    },
    # Клинический филиал (другое подразделение)
    {
        "Department": "Клиническая PizzaYolo",
        "CookingPlaceType": "Кухня",
        "PayTypes": "Наличные",
        "DishDiscountSumInt": 5000.0,
        "ProductCostBase.ProductCost": 1500.0,
    },
    {
        "Department": "Клиническая PizzaYolo",
        "CookingPlaceType": "Бар",
        "PayTypes": "Карта",
        "DishDiscountSumInt": 3000.0,
        "ProductCostBase.ProductCost": 600.0,
    },
]


# ═══════════════════════════════════════════════════════
# 1. fetch_day_report_data — передача department_id в iiko
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_day_report_passes_department_id():
    """fetch_day_report_data должен передавать department_ids=[dept_id] в fetch_olap_by_preset."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        await fetch_day_report_data(department_id=DEPT_ID)

    mock_fetch.assert_awaited_once()
    _, kwargs = mock_fetch.call_args
    assert kwargs.get("department_ids") == [DEPT_ID]


@pytest.mark.asyncio
async def test_fetch_day_report_no_department_id():
    """Без department_id должен передаваться department_ids=None."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        await fetch_day_report_data(department_id=None)

    mock_fetch.assert_awaited_once()
    _, kwargs = mock_fetch.call_args
    assert kwargs.get("department_ids") is None


# ═══════════════════════════════════════════════════════
# 1b. Клиентская фильтрация по полю Department
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_day_report_filters_by_department_name():
    """
    При передаче department_name — строки фильтруются по ТОЧНОМУ совпадению
    с полем Department (как в iiko_department.name).
    Из SAMPLE_ROWS (два филиала) должны остаться только строки Московского.
    """
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data(
            department_name="PizzaYolo / Пицца Йоло (Московский)"
        )

    assert result.error is None
    # Только Московский: 10000 + 2000 = 12000 выручка
    assert result.total_sales == pytest.approx(12000.0)
    assert result.total_cost == pytest.approx(3400.0)  # 3000 + 400


@pytest.mark.asyncio
async def test_fetch_day_report_no_department_name_shows_all():
    """Без department_name — возвращаются данные всех подразделений."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data(department_name=None)

    assert result.total_sales == pytest.approx(20000.0)  # все 4 строки


@pytest.mark.asyncio
async def test_fetch_day_report_filters_klinicheskaya():
    """Клиническая PizzaYolo — фильтр по точному имени."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data(department_name="Клиническая PizzaYolo")

    assert result.error is None
    # Только Клиническая: 5000 + 3000 = 8000
    assert result.total_sales == pytest.approx(8000.0)


@pytest.mark.asyncio
async def test_fetch_day_report_substring_no_longer_matches():
    """Подстрока (не полное имя) НЕ совпадает — данных нет (exact match)."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data(department_name="Московский")

    # Подстрока != точное имя → 0 строк
    assert result.total_sales == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_fetch_day_report_filters_case_insensitive():
    """Фильтрация по Department не чувствительна к регистру (сравнение через .lower())."""
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result_lower = await fetch_day_report_data(
            department_name="pizzayolo / пицца йоло (московский)"
        )
        result_upper = await fetch_day_report_data(
            department_name="PIZZAYOLO / ПИЦЦА ЙОЛО (МОСКОВСКИЙ)"
        )

    assert result_lower.total_sales == result_upper.total_sales
    assert result_lower.total_sales == pytest.approx(12000.0)


# ═══════════════════════════════════════════════════════
# 2. fetch_day_report_data — правильный расчёт себестоимости
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_day_report_cost_calculation():
    """
    Себестоимость берётся из ProductCostBase.ProductCost напрямую (рубли).
    Кухня: cost_rub=4500, sales=15000 → 30%.
    Бар: cost_rub=1000, sales=5000 → 20%.
    """
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.error is None

    # продажи: суммируем по PayTypes (4 строки, 2 типа оплаты)
    assert result.total_sales == pytest.approx(20000.0)
    pay_names = {sl.pay_type for sl in result.sales_lines}
    assert "Наличные" in pay_names
    assert "Карта" in pay_names
    nalichnie = next(sl for sl in result.sales_lines if sl.pay_type == "Наличные")
    assert nalichnie.amount == pytest.approx(17000.0)  # 10000+2000+5000

    # себестоимость: суммируем по CookingPlaceType
    assert len(result.cost_lines) == 2

    kitchen = next(cl for cl in result.cost_lines if cl.place == "Кухня")
    assert kitchen.cost_rub == pytest.approx(4500.0)  # 3000 + 1500
    assert kitchen.sales == pytest.approx(15000.0)  # 10000 + 5000
    assert kitchen.cost_pct == pytest.approx(30.0)

    bar = next(cl for cl in result.cost_lines if cl.place == "Бар")
    assert bar.cost_rub == pytest.approx(1000.0)  # 400 + 600
    assert bar.sales == pytest.approx(5000.0)  # 2000 + 3000
    assert bar.cost_pct == pytest.approx(20.0)

    # общая себестоимость
    assert result.total_cost == pytest.approx(5500.0)


@pytest.mark.asyncio
async def test_fetch_day_report_cost_not_zero_when_cost_present():
    """Себестоимость не должна быть нулём, если ProductCostBase.ProductCost > 0."""
    rows = [
        {
            "CookingPlaceType": "Кухня",
            "PayTypes": "Карта",
            "DishDiscountSumInt": 50000.0,
            "ProductCostBase.ProductCost": 15000.0,
        },
    ]
    mock_fetch = AsyncMock(return_value=rows)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.total_cost > 0
    assert result.cost_lines[0].cost_pct == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_fetch_day_report_cost_zero_when_no_cost_field():
    """Если ProductCostBase.ProductCost отсутствует — себестоимость 0, без ошибок."""
    rows = [
        {
            "CookingPlaceType": "Кухня",
            "PayTypes": "Карта",
            "DishDiscountSumInt": 10000.0,
        },
    ]
    mock_fetch = AsyncMock(return_value=rows)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.total_cost == pytest.approx(0.0)
    assert result.cost_lines[0].cost_pct == pytest.approx(0.0)
    assert result.error is None


# ═══════════════════════════════════════════════════════
# 3. fetch_day_report_data — ошибка iiko
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_day_report_iiko_error():
    """При исключении от iiko возвращается DayReportData с полем error."""
    mock_fetch = AsyncMock(side_effect=Exception("iiko недоступен"))

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.error is not None
    assert "iiko" in result.error.lower()
    assert result.total_sales == 0
    assert result.total_cost == 0


# ═══════════════════════════════════════════════════════
# 4. format_day_report — корректное форматирование
# ═══════════════════════════════════════════════════════


def test_format_day_report_with_data():
    """format_day_report должен содержать имя сотрудника, продажи, себестоимость."""
    iiko_data = DayReportData(
        sales_lines=[SalesLine(pay_type="Карта", amount=20000.0)],
        total_sales=20000.0,
        cost_lines=[
            CostLine(place="Кухня", sales=20000.0, cost_rub=6000.0, cost_pct=30.0)
        ],
        total_cost=6000.0,
        avg_cost_pct=30.0,
    )

    text = format_day_report(
        employee_name="Иванов Иван",
        date_str="2026-02-22",
        positives="Команда работала отлично",
        negatives="Задержка поставки",
        iiko_data=iiko_data,
    )

    assert "Иванов Иван" in text
    assert "2026-02-22" in text
    assert "20" in text  # сумма продаж
    assert "Карта" in text
    assert "Кухня" in text
    assert "30.0" in text  # себестоимость %
    assert "6" in text  # себестоимость руб


def test_format_day_report_with_error():
    """При ошибке iiko report должен содержать предупреждение."""
    iiko_data = DayReportData(
        sales_lines=[],
        total_sales=0,
        cost_lines=[],
        total_cost=0,
        avg_cost_pct=0,
        error="Ошибка iiko: timeout",
    )

    text = format_day_report(
        employee_name="Петров",
        date_str="2026-02-22",
        positives="+",
        negatives="-",
        iiko_data=iiko_data,
    )

    assert "⚠️" in text
    assert "timeout" in text.lower() or "ошибка" in text.lower()


# ═══════════════════════════════════════════════════════
# 5. _build_full_headers — динамические заголовки GSheets
# ═══════════════════════════════════════════════════════


def test_build_full_headers_basic():
    """Базовый случай: нет существующих заголовков."""
    from adapters.google_sheets import _build_full_headers

    headers = _build_full_headers(
        existing=[],
        pay_types=["Наличные", "Карта"],
        places=["Кухня", "Бар"],
    )

    assert headers[:5] == ["Дата", "Сотрудник", "Подразделение", "Плюсы", "Минусы"]
    assert "Карта, ₽" in headers
    assert "Наличные, ₽" in headers
    assert "Выручка ИТОГО, ₽" in headers
    assert "Бар выр, ₽" in headers
    assert "Кухня себест, %" in headers
    assert "Себестоимость ИТОГО, ₽" in headers
    assert "Средняя себестоимость, %" in headers
    assert headers[-1] == "Средняя себестоимость, %"
    assert headers[-2] == "Себестоимость ИТОГО, ₽"


def test_build_full_headers_preserves_existing_and_adds_new():
    """Новые pay_type и place добавляются к уже существующим."""
    from adapters.google_sheets import _build_full_headers

    existing = [
        "Дата",
        "Сотрудник",
        "Подразделение",
        "Плюсы",
        "Минусы",
        "Наличные, ₽",
        "Выручка ИТОГО, ₽",
        "Кухня выр, ₽",
        "Кухня себест, ₽",
        "Кухня себест, %",
        "Себестоимость ИТОГО, ₽",
        "Средняя себестоимость, %",
    ]

    headers = _build_full_headers(
        existing=existing,
        pay_types=["Карта"],  # новый тип оплаты
        places=["Кухня", "Бар"],  # Бар — новое место
    )

    assert "Наличные, ₽" in headers
    assert "Карта, ₽" in headers
    assert "Кухня выр, ₽" in headers
    assert "Бар выр, ₽" in headers
    assert headers[-1] == "Средняя себестоимость, %"
    assert headers[-2] == "Себестоимость ИТОГО, ₽"


def test_build_full_headers_sorted_order():
    """Типы оплаты и места приготовления отсортированы по алфавиту."""
    from adapters.google_sheets import _build_full_headers

    headers = _build_full_headers(
        existing=[],
        pay_types=["Яндекс", "Карта", "Наличные"],
        places=["Пицца", "Бар", "Кухня"],
    )

    pay_cols = [
        h
        for h in headers
        if h.endswith(", ₽")
        and " выр" not in h
        and " себест" not in h
        and h not in ("Выручка ИТОГО, ₽", "Себестоимость ИТОГО, ₽")
    ]
    assert pay_cols == sorted(pay_cols)

    bar_idx = headers.index("Бар выр, ₽")
    kuhnya_idx = headers.index("Кухня выр, ₽")
    pizza_idx = headers.index("Пицца выр, ₽")
    assert bar_idx < kuhnya_idx < pizza_idx


# ═══════════════════════════════════════════════════════
# 6. append_day_report_row — запись в Google Sheets
# ═══════════════════════════════════════════════════════

_GSHEETS_DATA = {
    "date": "2026-02-22",
    "employee_name": "Сидоров",
    "department_name": "Московский",
    "positives": "Хорошо",
    "negatives": "Плохо",
    "sales_lines": [
        {"pay_type": "Наличные", "amount": 70000.0},
        {"pay_type": "Карта", "amount": 30000.0},
    ],
    "cost_lines": [
        {"place": "Кухня", "sales": 80000.0, "cost_rub": 24000.0, "cost_pct": 30.0},
        {"place": "Бар", "sales": 20000.0, "cost_rub": 4000.0, "cost_pct": 20.0},
    ],
    "total_sales": 100000.0,
    "total_cost": 28000.0,
    "avg_cost_pct": 28.0,
}


def test_append_day_report_row_calls_append_row():
    """
    append_day_report_row должен:
    - вызвать ws.update() чтобы записать динамические заголовки
    - вызвать ws.append_row() с корректными значениями в нужных позициях
    """
    mock_ws = MagicMock()
    mock_ws.get_all_values.return_value = []  # пустой лист

    mock_ss = MagicMock()
    mock_ss.worksheet.return_value = mock_ws
    mock_client = MagicMock()
    mock_client.open_by_key.return_value = mock_ss

    with patch("adapters.google_sheets._get_client", return_value=mock_client):
        from adapters.google_sheets import append_day_report_row, _build_full_headers

        append_day_report_row(_GSHEETS_DATA)

    mock_ws.append_row.assert_called_once()
    row = mock_ws.append_row.call_args[0][0]

    # Вычисляем ожидаемые заголовки чтобы найти позиции
    expected_headers = _build_full_headers([], ["Наличные", "Карта"], ["Кухня", "Бар"])
    assert row[expected_headers.index("Дата")] == "2026-02-22"
    assert row[expected_headers.index("Сотрудник")] == "Сидоров"
    assert row[expected_headers.index("Подразделение")] == "Московский"
    assert row[expected_headers.index("Плюсы")] == "Хорошо"
    assert row[expected_headers.index("Минусы")] == "Плохо"
    assert row[expected_headers.index("Наличные, ₽")] == pytest.approx(70000.0)
    assert row[expected_headers.index("Карта, ₽")] == pytest.approx(30000.0)
    assert row[expected_headers.index("Выручка ИТОГО, ₽")] == pytest.approx(100000.0)
    assert row[expected_headers.index("Кухня выр, ₽")] == pytest.approx(80000.0)
    assert row[expected_headers.index("Кухня себест, ₽")] == pytest.approx(24000.0)
    assert row[expected_headers.index("Кухня себест, %")] == pytest.approx(30.0)
    assert row[expected_headers.index("Бар выр, ₽")] == pytest.approx(20000.0)
    assert row[expected_headers.index("Бар себест, ₽")] == pytest.approx(4000.0)
    assert row[expected_headers.index("Бар себест, %")] == pytest.approx(20.0)
    assert row[expected_headers.index("Себестоимость ИТОГО, ₽")] == pytest.approx(
        28000.0
    )
    assert row[expected_headers.index("Средняя себестоимость, %")] == pytest.approx(
        28.0
    )


def test_append_day_report_row_creates_sheet_if_missing():
    """Если лист «Отчёт дня» не существует — он должен быть создан."""
    import gspread

    mock_ws = MagicMock()
    mock_ws.get_all_values.return_value = []

    mock_ss = MagicMock()
    mock_ss.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("not found")
    mock_ss.add_worksheet.return_value = mock_ws

    mock_client = MagicMock()
    mock_client.open_by_key.return_value = mock_ss

    with patch("adapters.google_sheets._get_client", return_value=mock_client):
        from adapters.google_sheets import append_day_report_row

        append_day_report_row(_GSHEETS_DATA)

    mock_ss.add_worksheet.assert_called_once()
    mock_ws.append_row.assert_called_once()
