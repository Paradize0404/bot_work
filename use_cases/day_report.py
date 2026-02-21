"""
Use-case: отчёт дня (смены).

Поток:
  1. Сотрудник заполняет плюсы и минусы смены (FSM в handler'е)
  2. Бизнес-логика запрашивает два OLAP-отчёта из iiko:
     - Продажи по типам оплаты (preset SALES_PRESET)
     - Себестоимость по типам мест приготовления (preset COST_PRESET)
  3. Собирает итоговое сообщение: плюсы + минусы + продажи + себестоимость
  4. Отправляет всем подписчикам (👑 Админ + сотрудники с правом «📊 Отчёт дня»)

Отчёт привязан к подразделению (department) сотрудника.
Данные из iiko фильтруются по дате (сегодня 00:00→завтра 00:00).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from adapters.iiko_api import fetch_olap_by_preset
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# Preset IDs (из iiko → Отчёты → сохранённые отчёты)
# ═══════════════════════════════════════════════════════

# «Выручка себестоимость бот» — продажи по типам оплаты
SALES_PRESET = "96df1c31-a77f-4b7c-94db-55db656aae6a"

# Себестоимость — тот же отчёт, данные группируются по CookingPlaceType
# (один preset содержит оба среза: PayTypes и CookingPlaceType)
COST_PRESET = SALES_PRESET


# ═══════════════════════════════════════════════════════
# Dataclass для результатов
# ═══════════════════════════════════════════════════════


@dataclass(slots=True)
class SalesLine:
    """Строка продаж по типу оплаты."""

    pay_type: str
    amount: float


@dataclass(slots=True)
class CostLine:
    """Строка себестоимости по месту приготовления."""

    place: str
    sales: float
    cost_rub: float
    cost_pct: float


@dataclass(slots=True)
class DayReportData:
    """Данные отчёта дня из iiko."""

    sales_lines: list[SalesLine]
    total_sales: float
    cost_lines: list[CostLine]
    total_cost: float
    avg_cost_pct: float
    error: str | None = None


# ═══════════════════════════════════════════════════════
# Получение данных из iiko
# ═══════════════════════════════════════════════════════


async def fetch_day_report_data() -> DayReportData:
    """
    Получить данные продаж и себестоимости за сегодня из iiko OLAP.

    Использует один preset «Выручка себестоимость бот» (96df1c31-...),
    который содержит группировки PayTypes и CookingPlaceType.
    """
    t0 = time.monotonic()
    logger.info("[day_report] Запрашиваю данные из iiko...")

    now = now_kgd()
    date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    date_to = date_from + timedelta(days=1)

    date_from_str = date_from.strftime("%Y-%m-%dT%H:%M:%S")
    date_to_str = date_to.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        rows = await fetch_olap_by_preset(SALES_PRESET, date_from_str, date_to_str)
    except Exception as exc:
        logger.exception("[day_report] Ошибка получения данных из iiko")
        return DayReportData(
            sales_lines=[],
            total_sales=0,
            cost_lines=[],
            total_cost=0,
            avg_cost_pct=0,
            error=f"Ошибка iiko: {exc}",
        )

    # ── Разбираем строки: продажи (по PayTypes) и себестоимость (по CookingPlaceType) ──
    sales_by_pay: dict[str, float] = {}
    cost_by_place: dict[str, dict[str, float]] = {}

    for row in rows:
        pay_type = row.get("PayTypes")
        place = row.get("CookingPlaceType")
        amount = row.get("DishDiscountSumInt", 0) or 0
        cost_pct_raw = row.get("ProductCostBase.Percent", 0) or 0

        # Строки с PayTypes → продажи
        if pay_type:
            sales_by_pay[pay_type] = sales_by_pay.get(pay_type, 0) + amount

        # Строки с CookingPlaceType → себестоимость
        if place:
            if place not in cost_by_place:
                cost_by_place[place] = {"sales": 0, "cost_pct_raw": 0, "count": 0}
            cost_by_place[place]["sales"] += amount
            # Взвешенная средняя: суммируем sales * pct для каждой строки
            cost_by_place[place]["cost_pct_raw"] += amount * cost_pct_raw
            cost_by_place[place]["count"] += 1

    # ── Формируем результаты продаж ──
    sales_lines = [
        SalesLine(pay_type=pt, amount=amt)
        for pt, amt in sorted(sales_by_pay.items(), key=lambda x: -x[1])
    ]
    total_sales = sum(s.amount for s in sales_lines)

    # ── Формируем результаты себестоимости ──
    cost_lines: list[CostLine] = []
    total_cost_rub = 0.0
    total_cost_sales = 0.0

    for place, data in sorted(cost_by_place.items()):
        place_sales = data["sales"]
        # Средневзвешенный % себестоимости
        weighted_pct = (data["cost_pct_raw"] / place_sales) if place_sales else 0
        cost_rub = place_sales * weighted_pct
        cost_pct = weighted_pct * 100

        cost_lines.append(
            CostLine(
                place=place,
                sales=place_sales,
                cost_rub=cost_rub,
                cost_pct=cost_pct,
            )
        )
        total_cost_rub += cost_rub
        total_cost_sales += place_sales

    avg_cost_pct = (total_cost_rub / total_cost_sales * 100) if total_cost_sales else 0

    elapsed = time.monotonic() - t0
    logger.info(
        "[day_report] Данные получены: %d продаж, %d мест, %.1f сек",
        len(sales_lines),
        len(cost_lines),
        elapsed,
    )

    return DayReportData(
        sales_lines=sales_lines,
        total_sales=total_sales,
        cost_lines=cost_lines,
        total_cost=total_cost_rub,
        avg_cost_pct=avg_cost_pct,
    )


# ═══════════════════════════════════════════════════════
# Форматирование итогового сообщения
# ═══════════════════════════════════════════════════════


def format_day_report(
    employee_name: str,
    date_str: str,
    positives: str,
    negatives: str,
    iiko_data: DayReportData,
) -> str:
    """
    Собрать итоговый текст отчёта дня для отправки в Telegram.
    Формат максимально приближен к оригинальному боту.
    """
    lines: list[str] = []

    # ── Заголовок с данными сотрудника ──
    lines.append(f"👤 Сотрудник: {employee_name}")
    lines.append(f"🗓 Дата: {date_str}")
    lines.append(f"✅ Плюсы: {positives}")
    lines.append(f"❌ Минусы: {negatives}")

    # ── Продажи ──
    lines.append("")
    lines.append("📊 <b>Продажи за сегодня:</b>")
    lines.append("")

    if iiko_data.error:
        lines.append(f"⚠️ {iiko_data.error}")
    else:
        for sl in iiko_data.sales_lines:
            lines.append(f"— {sl.pay_type}: <b>{sl.amount:,.2f} ₽</b>")
        lines.append("")
        lines.append(f"ИТОГО: <b>{iiko_data.total_sales:,.2f} ₽</b>")

        # ── Себестоимость ──
        lines.append("")
        lines.append("📉 <b>Себестоимость:</b>")
        lines.append("")

        for cl in iiko_data.cost_lines:
            lines.append(
                f"— {cl.place}: <b>{cl.sales:,.2f} ₽</b> — <b>{cl.cost_pct:.1f}%</b>"
            )

        lines.append("")
        lines.append(f"ИТОГО: <b>{iiko_data.total_cost:,.2f} ₽</b>")
        lines.append(f"Средняя себестоимость: <b>{iiko_data.avg_cost_pct:.1f}%</b>")

    return "\n".join(lines)
