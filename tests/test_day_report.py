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

# Пример OLAP-строк от iiko:
# PayTypes-строки → продажи, CookingPlaceType-строки → себестоимость
SAMPLE_ROWS = [
    # продажи по типам оплаты
    {"PayTypes": "Наличные", "DishDiscountSumInt": 10000.0, "ProductCostBase.Cost": 0},
    {"PayTypes": "Карта", "DishDiscountSumInt": 5000.0, "ProductCostBase.Cost": 0},
    # себестоимость по местам приготовления
    {
        "CookingPlaceType": "Кухня",
        "DishDiscountSumInt": 12000.0,
        "ProductCostBase.Cost": 3600.0,
    },
    {
        "CookingPlaceType": "Бар",
        "DishDiscountSumInt": 3000.0,
        "ProductCostBase.Cost": 600.0,
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
# 2. fetch_day_report_data — правильный расчёт себестоимости
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_day_report_cost_calculation():
    """
    Себестоимость должна браться из ProductCostBase.Cost напрямую (рубли).
    Кухня: cost_rub=3600, sales=12000 → 30%.
    Бар: cost_rub=600, sales=3000 → 20%.
    """
    mock_fetch = AsyncMock(return_value=SAMPLE_ROWS)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.error is None

    # продажи
    assert result.total_sales == pytest.approx(15000.0)
    pay_names = {sl.pay_type for sl in result.sales_lines}
    assert "Наличные" in pay_names
    assert "Карта" in pay_names

    # себестоимость
    assert len(result.cost_lines) == 2

    kitchen = next(cl for cl in result.cost_lines if cl.place == "Кухня")
    assert kitchen.cost_rub == pytest.approx(3600.0)
    assert kitchen.cost_pct == pytest.approx(30.0)

    bar = next(cl for cl in result.cost_lines if cl.place == "Бар")
    assert bar.cost_rub == pytest.approx(600.0)
    assert bar.cost_pct == pytest.approx(20.0)

    # общая себестоимость
    assert result.total_cost == pytest.approx(4200.0)


@pytest.mark.asyncio
async def test_fetch_day_report_cost_not_zero_when_cost_present():
    """Себестоимость не должна быть нулём, если ProductCostBase.Cost > 0."""
    rows = [
        {
            "CookingPlaceType": "Кухня",
            "DishDiscountSumInt": 50000.0,
            "ProductCostBase.Cost": 15000.0,
        },
    ]
    mock_fetch = AsyncMock(return_value=rows)

    with patch("use_cases.day_report.fetch_olap_by_preset", mock_fetch):
        result = await fetch_day_report_data()

    assert result.total_cost > 0
    assert result.cost_lines[0].cost_pct == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_fetch_day_report_cost_zero_when_no_cost_field():
    """Если ProductCostBase.Cost отсутствует или 0 — себестоимость 0, без ошибок."""
    rows = [
        {"CookingPlaceType": "Кухня", "DishDiscountSumInt": 10000.0},
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
# 5. append_day_report_row — запись в Google Sheets
# ═══════════════════════════════════════════════════════


def test_append_day_report_row_calls_append_row():
    """
    append_day_report_row должен вызывать ws.append_row с корректными данными.
    Лист уже существует (worksheet() не бросает исключений).
    """
    mock_ws = MagicMock()
    mock_ws.get_all_values.return_value = [["Дата", "Сотрудник"]]  # заголовки есть

    mock_ss = MagicMock()
    mock_ss.worksheet.return_value = mock_ws

    mock_client = MagicMock()
    mock_client.open_by_key.return_value = mock_ss

    with patch("adapters.google_sheets._get_client", return_value=mock_client):
        from adapters.google_sheets import append_day_report_row

        append_day_report_row(
            {
                "date": "2026-02-22",
                "employee_name": "Сидоров",
                "department_name": "Московский",
                "positives": "Хорошо",
                "negatives": "Плохо",
                "total_sales": 100000.0,
                "total_cost": 30000.0,
                "avg_cost_pct": 30.0,
            }
        )

    mock_ws.append_row.assert_called_once()
    row = mock_ws.append_row.call_args[0][0]

    assert row[0] == "2026-02-22"
    assert row[1] == "Сидоров"
    assert row[2] == "Московский"
    assert row[3] == "Хорошо"
    assert row[4] == "Плохо"
    assert row[5] == pytest.approx(100000.0)
    assert row[6] == pytest.approx(30000.0)
    assert row[7] == pytest.approx(30.0)


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

        append_day_report_row(
            {
                "date": "2026-02-22",
                "employee_name": "Test",
                "department_name": "Dept",
                "positives": "+",
                "negatives": "-",
                "total_sales": 0,
                "total_cost": 0,
                "avg_cost_pct": 0,
            }
        )

    mock_ss.add_worksheet.assert_called_once()
    mock_ws.append_row.assert_called_once()
