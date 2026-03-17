"""
Use-case: отчёт дня (смены).

Поток:
  1. Сотрудник заполняет плюсы и минусы смены (FSM в handler'е)
  2. Бизнес-логика запрашивает два OLAP-отчёта из iiko:
     - Продажи по типам оплаты (preset SALES_PRESET)
     - Себестоимость по типам мест приготовления (preset COST_PRESET)
  3. Собирает итоговое сообщение: плюсы + минусы + продажи + себестоимость
  4. Отправляет всем сотрудникам с правом «📋 Отчёт дня» (PERM_DAY_REPORT)

Отчёт привязан к подразделению (department) сотрудника.
Данные из iiko фильтруются по дате (сегодня 00:00→завтра 00:00).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from adapters.iiko_api import fetch_olap_sales_v1
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

# Общие / не различающие слова, которые встречаются во всех подразделениях.
# Не используются при keyword-матчинге, чтобы избежать ложных совпадений.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {"пицца", "йоло", "pizzayolo", "/", "пиццайоло"}
)

# Минимальная длина слова для keyword-матчинга (после strip скобок)
_MIN_KEYWORD_LEN = 3


def _extract_keywords(name: str) -> set[str]:
    """Извлечь значимые слова из имени подразделения (lower, без скобок, без generic)."""
    words: set[str] = set()
    for w in name.lower().split():
        clean = w.strip("()[].,/")
        if len(clean) >= _MIN_KEYWORD_LEN and clean not in _GENERIC_WORDS:
            words.add(clean)
    return words


def _dept_matches(dept_name: str, olap_dept: str) -> bool:
    """
    Проверить, соответствует ли department_name из БД значению Department из OLAP.

    Трёхступенчатый матчинг (case-insensitive):
      1. Подстрока: dept_name ⊂ olap_dept  ИЛИ  olap_dept ⊂ dept_name
      2. Ключевые слова: пересечение значимых слов (без generic типа «Пицца», «Йоло»).
         Пример: «Пицца Йоло (Гайдара)» ↔ «PizzaYolo / Гайдара PizzaYolo»
         → общее слово «гайдара» → совпадение.
    """
    a = dept_name.strip().lower()
    b = olap_dept.strip().lower()

    # 1. Подстрока (быстрая проверка)
    if a in b or b in a:
        return True

    # 2. Ключевые слова
    kw_a = _extract_keywords(a)
    kw_b = _extract_keywords(b)
    return bool(kw_a & kw_b)


async def get_distinct_cooking_place_types() -> list[str]:
    """
    Получить уникальные значения CookingPlaceType из пресета выручки.

    Запрашивает текущий месяц (1-е число → следующий месяц).
    Используется для формирования выпадающего списка в колонке H маппинга.
    """
    now = now_kgd()
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = first_day.replace(year=now.year + 1, month=1)
    else:
        next_month = first_day.replace(month=now.month + 1)

    date_from = first_day.strftime("%Y-%m-%dT00:00:00")
    date_to = next_month.strftime("%Y-%m-%dT00:00:00")

    try:
        rows = await fetch_olap_sales_v1(date_from, date_to)
    except Exception:
        logger.exception("[day_report] Ошибка получения CookingPlaceType из iiko")
        return []

    types: set[str] = set()
    for row in rows:
        cpt = (row.get("CookingPlaceType") or "").strip()
        if cpt:
            types.add(cpt)

    result = sorted(types)
    logger.info("[day_report] CookingPlaceType: %d значений: %s", len(result), result)
    return result


async def get_distinct_pay_types() -> list[str]:
    """
    Получить уникальные значения PayTypes из пресета выручки.

    Запрашивает текущий месяц (1-е число → следующий месяц).
    Используется для формирования выпадающего списка в колонке J маппинга.
    """
    now = now_kgd()
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = first_day.replace(year=now.year + 1, month=1)
    else:
        next_month = first_day.replace(month=now.month + 1)

    date_from = first_day.strftime("%Y-%m-%dT00:00:00")
    date_to = next_month.strftime("%Y-%m-%dT00:00:00")

    try:
        rows = await fetch_olap_sales_v1(date_from, date_to)
    except Exception:
        logger.exception("[day_report] Ошибка получения PayTypes из iiko")
        return []

    types: set[str] = set()
    for row in rows:
        pt = (row.get("PayTypes") or "").strip()
        if pt:
            types.add(pt)

    result = sorted(types)
    logger.info("[day_report] PayTypes: %d значений: %s", len(result), result)
    return result


async def fetch_day_report_data(
    department_id: str | None = None,
    department_name: str | None = None,
) -> DayReportData:
    """
    Получить данные продаж и себестоимости за сегодня из iiko OLAP.

    Используем V1 OLAP API (GET /resto/api/reports/olap?report=SALES),
    который явно запрашивает нужные метрики (DishDiscountSumInt,
    ProductCostBase.ProductCost) и измерения (Department, PayTypes,
    CookingPlaceType). В отличие от V2 preset API, V1 гарантирует
    возврат ВСЕХ типов оплаты и подразделений, включая новые —
    V2 preset может не включать недавно добавленные PayTypes/Department.

    Фильтрация данных по подразделению:
      Клиентский уровень: фильтрация строк по полю Department.
      Имена в БД и в OLAP могут отличаться (латиница/кириллица, иерархия),
      поэтому используется трёхступенчатый матчинг:
        a) подстрока: «Клиническая PizzaYolo» ⊂ OLAP «Клиническая PizzaYolo»
        b) содержание: «Московский» ⊂ «PizzaYolo / Пицца Йоло (Московский)»
        c) ключевые слова: «Пицца Йоло (Гайдара)» ↔ «PizzaYolo / Гайдара PizzaYolo»
           (общее слово «Гайдара»)

    Args:
        department_id:   UUID подразделения (не используется для V1, сохранён для совместимости).
        department_name: Имя подразделения для клиентской фильтрации по полю Department.
    """
    t0 = time.monotonic()
    logger.info(
        "[day_report] Запрашиваю данные из iiko (V1 OLAP), dept=%s...",
        department_name or department_id or "все",
    )

    now = now_kgd()
    date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    date_to = date_from + timedelta(days=1)

    date_from_str = date_from.strftime("%Y-%m-%dT%H:%M:%S")
    date_to_str = date_to.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        rows = await fetch_olap_sales_v1(date_from_str, date_to_str)
    except Exception as exc:
        logger.exception("[day_report] Ошибка получения данных из iiko (V1 OLAP)")
        return DayReportData(
            sales_lines=[],
            total_sales=0,
            cost_lines=[],
            total_cost=0,
            avg_cost_pct=0,
            error=f"Ошибка iiko: {exc}",
        )

    # ── Клиентская фильтрация по полю Department ──
    # V1 OLAP возвращает данные ВСЕХ подразделений,
    # поэтому клиентский фильтр обязателен.
    # Используем _dept_matches(): подстрока + ключевые слова.
    if department_name:
        original_count = len(rows)
        rows = [
            r for r in rows if _dept_matches(department_name, r.get("Department", ""))
        ]
        logger.info(
            "[day_report] Фильтр по Department '%s': %d → %d строк",
            department_name,
            original_count,
            len(rows),
        )

    # ── Разбираем строки: продажи (по PayTypes) и себестоимость (по CookingPlaceType) ──
    sales_by_pay: dict[str, float] = {}
    cost_by_place: dict[str, dict[str, float]] = {}

    for row in rows:
        pay_type = row.get("PayTypes")
        place = row.get("CookingPlaceType")
        amount = row.get("DishDiscountSumInt", 0) or 0
        # ProductCostBase.ProductCost — себестоимость в рублях (прямое значение из iiko)
        cost_rub_raw = row.get("ProductCostBase.ProductCost", 0) or 0

        # Строки с PayTypes → продажи
        if pay_type:
            sales_by_pay[pay_type] = sales_by_pay.get(pay_type, 0) + amount

        # Строки с CookingPlaceType → себестоимость
        if place:
            if place not in cost_by_place:
                cost_by_place[place] = {"sales": 0, "cost_rub": 0}
            cost_by_place[place]["sales"] += amount
            cost_by_place[place]["cost_rub"] += cost_rub_raw

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
        # Себестоимость в рублях напрямую из iiko (ProductCostBase.Cost)
        cost_rub = data["cost_rub"]
        cost_pct = (cost_rub / place_sales * 100) if place_sales else 0

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
