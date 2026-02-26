"""
Use-case: расчёт мотивации «от выручки» через OLAP-отчёт и явки iiko.

Логика:
  1. Для каждого сотрудника с активной записью в «Истории ставок»
     где mot_base = "от выручки" и mot_pct > 0:
       a. Берём его явки за расчётный период (из attendance_records).
       b. Определяем уникальные пары (дата смены, подразделение).
       c. Для каждой пары берём выручку (DishDiscountSumInt) из OLAP-отчёта
          «Отчет по мотивации БОТ» (пресет df94cd59-…).
       d. Мотивация = Σ(выручка) × mot_pct / 100.
  2. Возвращаем dict: employee_full_name → motivation_amount (руб.)

OLAP-отчёт:
  Название:  «Отчет по мотивации БОТ»
  Id:        df94cd59-2cd8-4608-86bd-6267e1ea8cc6
  Метрика:   DishDiscountSumInt (сумма со скидкой, MONEY)
  Строки:    CloseTime (время закрытия, DATETIME)
  Столбцы:   Department (торговое предприятие, STRING)
"""

import logging
from collections import defaultdict
from datetime import date

from adapters.iiko_api import fetch_motivation_revenue_olap
from use_cases.salary_history import load_salary_history_index, get_rate_for_date

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Построение индекса выручки из OLAP
# ═══════════════════════════════════════════════════════


def _normalize_date(raw: str) -> str:
    """
    Привести строку даты к формату YYYY-MM-DD.

    Поддерживает:
      - "Sun Feb 01 09:07:46 EET 2026" — Java toString (iiko OLAP v1)
      - "DD.MM.YYYY [HH:MM:SS]"       — числовой формат OLAP v1
      - "YYYY-MM-DD[THH:MM:SS]"       — ISO / attendance API
    """
    s = raw.strip()
    if not s:
        return s

    # Java toString: "Mon Feb 01 09:07:46 EET 2026"
    # Шаблон: Day Mon DD HH:MM:SS TZ YYYY
    _JAVA_MONTHS = {
        "Jan": "01",
        "Feb": "02",
        "Mar": "03",
        "Apr": "04",
        "May": "05",
        "Jun": "06",
        "Jul": "07",
        "Aug": "08",
        "Sep": "09",
        "Oct": "10",
        "Nov": "11",
        "Dec": "12",
    }
    parts = s.split()
    if len(parts) >= 5 and parts[1] in _JAVA_MONTHS:
        # parts = ["Sun", "Feb", "01", "09:07:46", "EET", "2026"]
        month = _JAVA_MONTHS[parts[1]]
        day = parts[2].zfill(2)
        year = parts[-1]  # год всегда последний
        return f"{year}-{month}-{day}"

    # DD.MM.YYYY [HH:MM:SS]
    if len(s) >= 10 and s[2] == "." and s[5] == ".":
        return f"{s[6:10]}-{s[3:5]}-{s[:2]}"

    # YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS+02:00
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]

    return s


def _build_revenue_index(olap_rows: list[dict]) -> dict[tuple[str, str], float]:
    """
    Преобразовать строки OLAP-отчёта в индекс:
      (date_str "YYYY-MM-DD", dept_name) → DishDiscountSumInt (руб.)

    OLAP v1 возвращает CloseTime в формате DD.MM.YYYY — нормализуется в YYYY-MM-DD.
    OLAP может вернуть несколько строк на одну пару (дата, подразделение) — суммируем.
    Строки без Department (итоговые) игнорируются.
    """
    index: dict[tuple[str, str], float] = defaultdict(float)
    skipped = 0
    for row in olap_rows:
        close_time_raw = str(row.get("CloseTime") or "").strip()
        dept = str(row.get("Department") or "").strip()
        revenue_raw = row.get("DishDiscountSumInt", 0)

        # Пропускаем итоговые строки (нет департамента или нет даты)
        if not close_time_raw or not dept:
            skipped += 1
            continue

        date_str = _normalize_date(close_time_raw)
        try:
            revenue = float(revenue_raw) if revenue_raw is not None else 0.0
        except (ValueError, TypeError):
            revenue = 0.0

        if revenue != 0:
            index[(date_str, dept)] += revenue

    logger.info(
        "[revenue_motivation] OLAP-индекс: %d уникальных (дата, подразд.), пропущено итоговых: %d",
        len(index),
        skipped,
    )
    return dict(index)


# ═══════════════════════════════════════════════════════
# Основной расчёт
# ═══════════════════════════════════════════════════════


async def calculate_revenue_motivation(
    attendance_records: list[dict],
    emp_full_names: dict[str, str],  # iiko_id → full_name
    date_from: date,
    date_to: date,
    history_index: dict[str, list[dict]] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Рассчитать мотивацию «от выручки» для всех сотрудников за период.

    Параметры:
      attendance_records — список явок из fetch_attendance() за период
      emp_full_names     — iiko_id (str) → «Фамилия Имя Отчество» (для поиска в истории)
      date_from / date_to — расчётный период (date-объекты)
      history_index      — кэш истории ставок (если None — загружается из БД)

    Возвращает:
      { "Фамилия И.О.": {"Подразделение": <motivation_rubles>, ...}, ... }
      Только сотрудники с mot_base="от выручки" и mot_pct>0.
    """
    t_from_str = date_from.strftime("%Y-%m-%d")
    t_to_str = date_to.strftime("%Y-%m-%d")

    # ── 1. Загружаем OLAP-отчёт и историю ставок параллельно ──
    import asyncio

    if history_index is None:
        olap_rows, history_index = await asyncio.gather(
            fetch_motivation_revenue_olap(t_from_str, t_to_str),
            load_salary_history_index(),
        )
    else:
        olap_rows = await fetch_motivation_revenue_olap(t_from_str, t_to_str)

    logger.info(
        "[revenue_motivation] OLAP строк: %d, сотрудников в истории: %d",
        len(olap_rows),
        len(history_index),
    )

    # ── 2. Строим индекс выручки (дата, подразделение) → руб. ──
    revenue_index = _build_revenue_index(olap_rows)

    if not revenue_index:
        logger.warning(
            "[revenue_motivation] OLAP вернул пустой результат за %s — %s",
            t_from_str,
            t_to_str,
        )
        return {}

    # ── 3. Индексируем явки по iiko_id ──
    # emp_iiko_id → [(shift_date_str, dept_name), ...]
    emp_shifts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for rec in attendance_records:
        emp_id = (rec.get("employeeId") or "").strip()
        dept_name = (rec.get("departmentName") or "").strip()
        date_from_raw = (rec.get("dateFrom") or "").strip()
        if not emp_id or not dept_name or not date_from_raw:
            continue
        shift_date_str = date_from_raw[:10]  # "YYYY-MM-DD"
        emp_shifts[emp_id].add((shift_date_str, dept_name))

    # ── 4. Для каждого сотрудника рассчитываем мотивацию ──
    result: dict[str, dict[str, float]] = {}

    for emp_iiko_id, full_name in emp_full_names.items():
        if not full_name:
            continue

        history = history_index.get(full_name, [])
        if not history:
            continue

        # Получаем активную запись на конец периода (последняя ставка за период)
        # Если ставка менялась в течение периода — используем текущую (упрощение,
        # т.к. процент мотивации обычно не меняется внутри месяца).
        active = get_rate_for_date(history, date_to)
        if active is None:
            # Попробуем на дату начала периода
            active = get_rate_for_date(history, date_from)
        if active is None:
            continue

        mot_base = (active.get("mot_base") or "").strip()
        mot_pct = active.get("mot_pct")

        if mot_base != "от выручки" or not mot_pct or float(mot_pct) <= 0:
            continue

        mot_pct_val = float(mot_pct)

        # Явки сотрудника за период
        shifts = emp_shifts.get(emp_iiko_id, set())
        if not shifts:
            logger.debug(
                "[revenue_motivation] %s (id=%s): нет явок за период, мотивация=0",
                full_name,
                emp_iiko_id,
            )
            continue

        # Суммируем выручку по уникальным (дата, подразделение) смены сотрудника
        total_revenue = 0.0
        matched_pairs: list[tuple[str, str, float]] = []
        for shift_date, dept_name in sorted(shifts):
            # Прямое совпадение по имени подразделения
            rev = revenue_index.get((shift_date, dept_name), 0.0)
            if rev <= 0:
                # Попытка нечёткого поиска: ищем dept_name как подстроку ключей
                for (d, dep), v in revenue_index.items():
                    if d == shift_date and (
                        dept_name.lower() in dep.lower()
                        or dep.lower() in dept_name.lower()
                    ):
                        rev = v
                        break
            if rev > 0:
                total_revenue += rev
                matched_pairs.append((shift_date, dept_name, rev))

        if total_revenue <= 0:
            logger.debug(
                "[revenue_motivation] %s: выручка=0 за период "
                "(явок %d, подразд.: %s)",
                full_name,
                len(shifts),
                {d for _, d in shifts},
            )
            continue

        # Разбиваем мотивацию по подразделениям
        dept_revenues: dict[str, float] = defaultdict(float)
        for _, dept_name_mp, rev in matched_pairs:
            dept_revenues[dept_name_mp] += rev

        result[full_name] = {
            dept: round(rev * mot_pct_val / 100.0, 2)
            for dept, rev in dept_revenues.items()
        }
        motivation_total = sum(result[full_name].values())

        logger.info(
            "[revenue_motivation] %s: выручка=%.2f × %.2f%% = %.2f руб. "
            "(%d смен, %d дней совпало, %d подразд.)",
            full_name,
            total_revenue,
            mot_pct_val,
            motivation_total,
            len(shifts),
            len(matched_pairs),
            len(dept_revenues),
        )

    logger.info(
        "[revenue_motivation] Итог: мотивация «от выручки» для %d сотрудников",
        len(result),
    )
    return result


# ═══════════════════════════════════════════════════════
# Вспомогательный публичный метод для payroll
# ═══════════════════════════════════════════════════════


async def get_revenue_motivation_map(
    attendance_records: list[dict],
    emp_iiko_to_fullname: dict[str, str],
    date_from: date,
    date_to: date,
    history_index: dict[str, list[dict]] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Тонкая обёртка над calculate_revenue_motivation.

    Безопасно: при любой ошибке API возвращает {} (не ломает ФОТ).

    Возвращает: full_name → {dept_name → motivation_rubles}
    """
    try:
        return await calculate_revenue_motivation(
            attendance_records=attendance_records,
            emp_full_names=emp_iiko_to_fullname,
            date_from=date_from,
            date_to=date_to,
            history_index=history_index,
        )
    except Exception:
        logger.exception(
            "[revenue_motivation] Ошибка при расчёте мотивации от выручки, "
            "возвращаю пустой результат"
        )
        return {}


async def get_department_revenue_totals(
    date_from: date,
    date_to: date,
) -> dict[str, float]:
    """
    Получить суммарную выручку по подразделениям из OLAP-отчёта.

    Возвращает: {dept_name → total_revenue_rubles}
    Безопасно: при ошибке возвращает {}.
    """
    try:
        olap_rows = await fetch_motivation_revenue_olap(
            date_from.strftime("%Y-%m-%d"),
            date_to.strftime("%Y-%m-%d"),
        )
        revenue_index = _build_revenue_index(olap_rows)
        totals: dict[str, float] = defaultdict(float)
        for (_, dept_name), rev in revenue_index.items():
            totals[dept_name] += rev
        logger.info(
            "[revenue_motivation] Выручка по подразделениям: %d шт.",
            len(totals),
        )
        return dict(totals)
    except Exception:
        logger.exception(
            "[revenue_motivation] Ошибка при получении выручки по подразделениям"
        )
        return {}


# ═══════════════════════════════════════════════════════
# Мотивация «от накладных кондитерки»
# ═══════════════════════════════════════════════════════


async def _load_pastry_group_ids_expanded() -> set[str]:
    """
    Загрузить все group_id из PastryNomenclatureGroup, а затем
    рекурсивно расширить набор всеми дочерними группами из ProductGroup.

    Возвращает множество UUID-строк всех групп (включая подгруппы).
    """
    from uuid import UUID as _UUID

    from sqlalchemy import select

    from db.engine import async_session_factory
    from db.models import PastryNomenclatureGroup, ProductGroup

    async with async_session_factory() as session:
        stmt = select(PastryNomenclatureGroup.group_id)
        root_ids = {str(gid) for gid in (await session.execute(stmt)).scalars().all()}
        if not root_ids:
            return set()

        # Загружаем всю иерархию групп
        stmt_groups = select(ProductGroup.id, ProductGroup.parent_id)
        rows = (await session.execute(stmt_groups)).all()

    # parent_id → [child_id, ...]
    children_map: dict[str, list[str]] = defaultdict(list)
    for gid, pid in rows:
        if pid:
            children_map[str(pid)].append(str(gid))

    # BFS по иерархии
    expanded = set(root_ids)
    queue = list(root_ids)
    while queue:
        current = queue.pop()
        for child in children_map.get(current, []):
            if child not in expanded:
                expanded.add(child)
                queue.append(child)

    logger.info(
        "[pastry_motivation] Кондитерские группы: %d корневых → %d с подгруппами",
        len(root_ids),
        len(expanded),
    )
    return expanded


async def _load_product_group_index() -> dict[str, str]:
    """Загрузить product_id → parent_id (group UUID)."""
    from sqlalchemy import select

    from db.engine import async_session_factory
    from db.models import Product

    async with async_session_factory() as session:
        stmt = select(Product.id, Product.parent_id).where(Product.deleted.is_(False))
        rows = (await session.execute(stmt)).all()
    return {str(pid): str(gid) for pid, gid in rows if gid}


async def _load_store_name_index() -> dict[str, str]:
    """Загрузить store_id → store_name."""
    from sqlalchemy import select

    from db.engine import async_session_factory
    from db.models import Store

    async with async_session_factory() as session:
        stmt = select(Store.id, Store.name).where(Store.deleted.is_(False))
        rows = (await session.execute(stmt)).all()
    return {str(sid): (name or "") for sid, name in rows}


def _match_store_to_dept(store_name: str, dept_names: set[str]) -> str | None:
    """Нечёткое сопоставление имени склада с именем подразделения."""
    sn = store_name.lower()
    for dn in dept_names:
        dl = dn.lower()
        if sn in dl or dl in sn:
            return dn
    return None


async def calculate_pastry_invoice_motivation(
    attendance_records: list[dict],
    emp_full_names: dict[str, str],
    date_from: date,
    date_to: date,
    history_index: dict[str, list[dict]] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Рассчитать мотивацию «от накладных кондитерки» за период.

    Логика:
      1. Получаем расходные накладные за период.
      2. Для каждой позиции проверяем, входит ли товар в кондитерские группы.
      3. Суммируем стоимость кондитерских позиций по складам.
      4. Сопоставляем склады с подразделениями.
      5. Для сотрудников с mot_base="от накладных кондитерки":
         мотивация = сумма кондитерских позиций по подразделениям × mot_pct / 100.

    Возвращает: {full_name: {dept_name: motivation_rubles}}
    """
    import asyncio

    from adapters.iiko_api import fetch_outgoing_invoices

    t_from_str = date_from.strftime("%Y-%m-%d")
    t_to_str = date_to.strftime("%Y-%m-%d")

    # ── 1. Параллельная загрузка данных ──
    if history_index is None:
        (
            outgoing_docs,
            history_index,
            pastry_groups,
            product_index,
            store_names,
        ) = await asyncio.gather(
            fetch_outgoing_invoices(t_from_str, t_to_str),
            load_salary_history_index(),
            _load_pastry_group_ids_expanded(),
            _load_product_group_index(),
            _load_store_name_index(),
        )
    else:
        outgoing_docs, pastry_groups, product_index, store_names = await asyncio.gather(
            fetch_outgoing_invoices(t_from_str, t_to_str),
            _load_pastry_group_ids_expanded(),
            _load_product_group_index(),
            _load_store_name_index(),
        )

    logger.info(
        "[pastry_motivation] Расходных накладных: %d, "
        "кондитерских групп (с подгруппами): %d, "
        "товаров: %d",
        len(outgoing_docs),
        len(pastry_groups),
        len(product_index),
    )

    if not pastry_groups:
        logger.warning("[pastry_motivation] Нет кондитерских групп в БД")
        return {}

    # ── 2. Суммируем кондитерские позиции по складам ──
    # store_id → total sum
    store_pastry_sum: dict[str, float] = defaultdict(float)
    matched_items = 0
    total_items = 0

    for doc in outgoing_docs:
        store_id = (doc.get("defaultStore") or "").strip()
        if not store_id:
            continue
        for item in doc.get("items", []):
            total_items += 1
            product_id = (item.get("productId") or "").strip()
            if not product_id:
                continue
            group_id = product_index.get(product_id)
            if group_id and group_id in pastry_groups:
                try:
                    item_sum = float(item.get("sum") or 0)
                except (ValueError, TypeError):
                    item_sum = 0.0
                if item_sum > 0:
                    store_pastry_sum[store_id] += item_sum
                    matched_items += 1

    logger.info(
        "[pastry_motivation] Позиций в расходных: %d, "
        "из них кондитерских: %d, складов: %d",
        total_items,
        matched_items,
        len(store_pastry_sum),
    )

    if not store_pastry_sum:
        return {}

    # ── 3. Сопоставляем склады → подразделения (dept_name) ──
    # Собираем уникальные dept_name из attendance
    dept_names: set[str] = set()
    for rec in attendance_records:
        dn = (rec.get("departmentName") or "").strip()
        if dn:
            dept_names.add(dn)

    # store_id → dept_name (matched)
    store_to_dept: dict[str, str] = {}
    for sid, s_total in store_pastry_sum.items():
        sname = store_names.get(sid, "")
        if not sname:
            continue
        matched_dept = _match_store_to_dept(sname, dept_names)
        if matched_dept:
            store_to_dept[sid] = matched_dept

    # dept_name → total pastry sum
    dept_pastry_sum: dict[str, float] = defaultdict(float)
    unmatched_sum = 0.0
    for sid, total in store_pastry_sum.items():
        dept = store_to_dept.get(sid)
        if dept:
            dept_pastry_sum[dept] += total
        else:
            unmatched_sum += total

    # Если есть незамэтченные склады — добавляем ко всем подразделениям поровну
    # (либо ко всем, либо в «Без подразделения»)
    if unmatched_sum > 0:
        logger.info(
            "[pastry_motivation] Незамэтченные склады: %.0f ₽ "
            "(будет распределено по всем подразделениям)",
            unmatched_sum,
        )
        if dept_pastry_sum:
            share = unmatched_sum / len(dept_pastry_sum)
            for dept in dept_pastry_sum:
                dept_pastry_sum[dept] += share
        else:
            # Нет ни одного замэтченного подразделения:
            # кладём под общий ключ
            dept_pastry_sum["_all_"] = unmatched_sum

    logger.info(
        "[pastry_motivation] Кондитерские суммы по подразделениям: %s",
        {k: f"{v:,.0f}" for k, v in dept_pastry_sum.items()},
    )

    # ── 4. Индексируем явки по iiko_id → dept_names ──
    emp_depts: dict[str, set[str]] = defaultdict(set)
    for rec in attendance_records:
        emp_id = (rec.get("employeeId") or "").strip()
        dept_name = (rec.get("departmentName") or "").strip()
        if emp_id and dept_name:
            emp_depts[emp_id].add(dept_name)

    # ── 5. Рассчитываем мотивацию ──
    result: dict[str, dict[str, float]] = {}

    for emp_iiko_id, full_name in emp_full_names.items():
        if not full_name:
            continue

        history = history_index.get(full_name, [])
        if not history:
            continue

        active = get_rate_for_date(history, date_to)
        if active is None:
            active = get_rate_for_date(history, date_from)
        if active is None:
            continue

        mot_base = (active.get("mot_base") or "").strip()
        mot_pct = active.get("mot_pct")

        if mot_base != "от накладных кондитерки" or not mot_pct:
            continue
        try:
            mot_pct_val = float(mot_pct)
        except (ValueError, TypeError):
            continue
        if mot_pct_val <= 0:
            continue

        # Подразделения сотрудника из явок
        my_depts = emp_depts.get(emp_iiko_id, set())
        if not my_depts:
            # Нет явок — если есть общий ключ _all_, используем его
            if "_all_" in dept_pastry_sum:
                total = dept_pastry_sum["_all_"]
                result[full_name] = {
                    "Без подразделения": round(total * mot_pct_val / 100, 2)
                }
            continue

        emp_motivation: dict[str, float] = {}
        for dept in my_depts:
            pastry_sum = dept_pastry_sum.get(dept, 0.0)
            if pastry_sum > 0:
                emp_motivation[dept] = round(pastry_sum * mot_pct_val / 100, 2)

        if emp_motivation:
            result[full_name] = emp_motivation
            logger.info(
                "[pastry_motivation] %s: %.1f%% × кондитерские → %s",
                full_name,
                mot_pct_val,
                {k: f"{v:,.0f}" for k, v in emp_motivation.items()},
            )

    logger.info(
        "[pastry_motivation] Итог: мотивация «от накладных кондитерки» "
        "для %d сотрудников",
        len(result),
    )
    return result


async def get_pastry_invoice_motivation_map(
    attendance_records: list[dict],
    emp_iiko_to_fullname: dict[str, str],
    date_from: date,
    date_to: date,
    history_index: dict[str, list[dict]] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Безопасная обёртка над calculate_pastry_invoice_motivation.

    Возвращает: full_name → {dept_name → motivation_rubles}
    """
    try:
        return await calculate_pastry_invoice_motivation(
            attendance_records=attendance_records,
            emp_full_names=emp_iiko_to_fullname,
            date_from=date_from,
            date_to=date_to,
            history_index=history_index,
        )
    except Exception:
        logger.exception(
            "[pastry_motivation] Ошибка при расчёте мотивации "
            "от накладных кондитерки, возвращаю пустой результат"
        )
        return {}
