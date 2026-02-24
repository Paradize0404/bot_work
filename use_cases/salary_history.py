"""
Use-case: синхронизация истории ставок из Google Sheets → БД.

Схема работы:
  1. Читаем лист «История ставок» (A=Сотрудник, B=Тип, C=Ставка, D=Дата с)
  2. Группируем записи по сотруднику, сортируем по valid_from
  3. Для каждой записи кроме последней:
       valid_to = (valid_from следующей записи) − 1 день
     Для последней:
       valid_to = NULL (ставка действует по сей день)
  4. Upsert в таблицу salary_history (по уникальному ключу employee_name + valid_from)
  5. Записываем обратно колонку «Дата по» в Google Sheets
  6. Предоставляет функцию get_rate_for_date() для расчёта ФОТ

Также экспортирует setup_history_sheet() — создаёт/обновляет лист с дропдаунами.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import async_session_factory
from db.models import SalaryHistory, Employee, EmployeeRole
from adapters.google_sheets import (
    read_salary_history_sheet,
    write_salary_history_valid_to,
    setup_salary_history_sheet,
    append_salary_history_rows,
    write_history_iiko_ids,
    read_salary_settings,
    delete_salary_history_rows,
)

logger = logging.getLogger(__name__)

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_date(s: str) -> Optional[date]:
    """Разобрать дату из строки DD.MM.YYYY или YYYY-MM-DD."""
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _fmt_date(d: Optional[date]) -> str:
    """Форматировать дату в DD.MM.YYYY для GSheet, или '' если None."""
    return d.strftime("%d.%m.%Y") if d else ""


# ═══════════════════════════════════════════════════════
# Синхронизация GSheet → БД
# ═══════════════════════════════════════════════════════


async def sync_salary_history(triggered_by: str | None = None) -> int:
    """
    Прочитать лист «История ставок», вычислить valid_to, уpsert в БД,
    записать Дата по обратно в GSheet.

    Возвращает количество записей, сохранённых в БД.
    """
    logger.info("[salary_history] Синхронизация истории ставок (by=%s)", triggered_by)

    # ── 1. Читаем лист ──
    raw_rows = await read_salary_history_sheet()
    if not raw_rows:
        logger.info("[salary_history] Лист пуст, нечего синхронизировать")
        return 0

    # ── 2. Парсим даты, группируем по имени ──
    # name → [(row_index, sal_type, rate, mot_pct, mot_base, valid_from_date), ...]
    grouped: dict[str, list[tuple[int, str, float, object, object, date]]] = (
        defaultdict(list)
    )
    skipped = 0
    for r in raw_rows:
        d = _parse_date(r["valid_from"])
        if d is None:
            logger.warning(
                "[salary_history] Строка %d: не удалось разобрать дату «%s», пропускаю",
                r["row"],
                r["valid_from"],
            )
            skipped += 1
            continue
        grouped[r["name"]].append(
            (r["row"], r["sal_type"], r["rate"], r.get("mot_pct"), r.get("mot_base"), d)
        )

    if skipped:
        logger.warning(
            "[salary_history] Пропущено строк из-за неверной даты: %d", skipped
        )

    # ── 3. Вычисляем valid_to, готовим данные для БД и GSheet ──
    db_records: list[dict] = []
    sheet_updates: list[dict] = []  # {"row": int, "valid_to": str}

    for name, entries in grouped.items():
        # Сортируем по дате начала
        entries.sort(key=lambda x: x[5])

        for i, (row_idx, sal_type, rate, mot_pct, mot_base, valid_from) in enumerate(
            entries
        ):
            if i + 1 < len(entries):
                # valid_to = день перед следующей записью
                next_from = entries[i + 1][5]
                valid_to: Optional[date] = next_from - timedelta(days=1)
            else:
                # Последняя (текущая) запись
                valid_to = None

            db_records.append(
                {
                    "employee_name": name,
                    "sal_type": sal_type,
                    "rate": rate,
                    "mot_pct": mot_pct,
                    "mot_base": mot_base,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                }
            )
            sheet_updates.append(
                {
                    "row": row_idx,
                    "valid_to": _fmt_date(valid_to),
                }
            )

    logger.info(
        "[salary_history] Подготовлено: %d записей по %d сотрудникам",
        len(db_records),
        len(grouped),
    )

    # ── 4. Upsert в БД ──
    async with async_session_factory() as session:
        for rec in db_records:
            stmt = pg_insert(SalaryHistory).values(**rec)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_salary_history_emp_from",
                set_={
                    "sal_type": rec["sal_type"],
                    "rate": rec["rate"],
                    "mot_pct": rec["mot_pct"],
                    "mot_base": rec["mot_base"],
                    "valid_to": rec["valid_to"],
                },
            )
            await session.execute(stmt)
        await session.commit()

    logger.info("[salary_history] Upsert в БД: %d записей", len(db_records))

    # ── 5. Записываем valid_to обратно в GSheet ──
    await write_salary_history_valid_to(sheet_updates)

    # ── 6. Закрываем открытые записи для уволенных/удалённых сотрудников ──
    await close_history_for_deleted_employees()

    return len(db_records)


# ═══════════════════════════════════════════════════════
# Автозакрытие истории для уволенных сотрудников
# ═══════════════════════════════════════════════════════


async def close_history_for_deleted_employees() -> int:
    """
    Найти всех сотрудников с deleted=True в иико, у которых ещё открытая запись в истории
    (в salary_history valid_to=NULL), и закрыть им ставку текущей датой.

    Логика:
      - Если сотрудник удалён в iiko, его последняя активная ставка закрывается текущей датой.
      - Прошлые периоды считаются по ставке на дату внутри периода, не ломаясь.
      - Сам сотрудник остаётся в «Истории ставок» — строки не удаляются, только закрывается.

    Возвращает количество закрытых записей.
    """
    today = date.today()

    # ── 1. Загружаем удалённых сотрудников + открытые записи истории ──
    async with async_session_factory() as session:
        # Сотрудники с deleted=True (ФИО для сравнения)
        deleted_emps = (
            (
                await session.execute(
                    select(Employee).where(Employee.deleted == True)  # noqa: E712
                )
            )
            .scalars()
            .all()
        )

        if not deleted_emps:
            return 0

        # Строим множество ФИО удалённых
        def _full_name(emp: Employee) -> str:
            parts = [
                p
                for p in (emp.last_name, emp.first_name, emp.middle_name)
                if p and p.strip()
            ]
            return " ".join(parts) if parts else (emp.name or "").strip()

        deleted_names: set[str] = {_full_name(e) for e in deleted_emps if _full_name(e)}

        # Открытые записи истории для этих сотрудников
        open_records = (
            (
                await session.execute(
                    select(SalaryHistory).where(
                        SalaryHistory.valid_to == None,  # noqa: E711
                        SalaryHistory.employee_name.in_(deleted_names),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not open_records:
            return 0

        # Закрываем в БД
        for rec in open_records:
            rec.valid_to = today
        await session.commit()

    closed_names = [r.employee_name for r in open_records]
    logger.info(
        "[salary_history] Закрыто записей в Истории для удалённых: %d — %s",
        len(closed_names),
        ", ".join(closed_names[:5]),
    )

    # ── 2. Обновляем GSheet: читаем лист и пишем Дата по ──
    try:
        # Строим индекс: (имя, valid_from как "DD.MM.YYYY") → дата закрытия
        closed_map: dict[tuple[str, str], date] = {
            (r.employee_name, _fmt_date(r.valid_from)): today for r in open_records
        }
        raw_rows = await read_salary_history_sheet()
        sheet_updates: list[dict] = []
        for r in raw_rows:
            key = (r["name"], r["valid_from"])
            if key in closed_map:
                sheet_updates.append(
                    {"row": r["row"], "valid_to": _fmt_date(closed_map[key])}
                )
        if sheet_updates:
            await write_salary_history_valid_to(sheet_updates)
    except Exception:
        logger.exception("[salary_history] Ошибка при обновлении GSheet для удалённых")

    return len(open_records)


async def delete_history_for_employee(employee_id: str) -> int:
    """
    Полностью удалить все записи истории савок для сотрудника (по iiko UUID).

    Используется исключительно при ручном исключении через бот: удаляет всё из БД и из GSheet.
    При удалении через iiko (флаг deleted=True) используйте close_history_for_deleted_employees().

    Возвращает количество удалённых записей в БД.
    """
    # Вычисляем ФИО сотрудника по iiko UUID
    async with async_session_factory() as session:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            logger.warning(
                "[salary_history] delete_history_for_employee: сотрудник %s не найден",
                employee_id,
            )
            return 0

        parts = [
            p
            for p in (emp.last_name, emp.first_name, emp.middle_name)
            if p and p.strip()
        ]
        emp_name = " ".join(parts) if parts else (emp.name or "").strip()
        if not emp_name:
            return 0

        # Удаляем все записи из БД
        result = await session.execute(
            delete(SalaryHistory).where(SalaryHistory.employee_name == emp_name)
        )
        deleted_count = result.rowcount
        await session.commit()

    if deleted_count == 0:
        logger.info(
            "[salary_history] delete_history_for_employee: записей БД для %s не найдено,"
            " всё равно проверяем GSheet",
            emp_name,
        )
    else:
        logger.info(
            "[salary_history] Удалено из БД %d записей истории для %s",
            deleted_count,
            emp_name,
        )

    # Удаляем строки из GSheet всегда — даже если в БД записей не было
    # (строки могут быть введены вручную в листе без синхронизации в БД)
    try:
        raw_rows = await read_salary_history_sheet()
        row_nums = [r["row"] for r in raw_rows if r["name"] == emp_name]
        if row_nums:
            await delete_salary_history_rows(row_nums)
            logger.info(
                "[salary_history] Удалено %d строк GSheet «История ставок» для %s",
                len(row_nums),
                emp_name,
            )
        else:
            logger.info(
                "[salary_history] GSheet «История ставок»: строк для %s не найдено",
                emp_name,
            )
    except Exception:
        logger.exception(
            "[salary_history] Ошибка GSheet при удалении строк для %s", emp_name
        )

    return deleted_count


# ═══════════════════════════════════════════════════════
# Получение активной ставки для ФОТ
# ═══════════════════════════════════════════════════════


async def load_salary_history_index() -> dict[str, list[dict]]:
    """
    Загрузить всю историю ставок из БД.

    Возвращает:
      { "Иванов И.И.": [{"sal_type":..., "rate":..., "valid_from": date, "valid_to": date|None}, ...] }
    Записи внутри каждого сотрудника отсортированы по valid_from.
    """
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(SalaryHistory).order_by(
                        SalaryHistory.employee_name,
                        SalaryHistory.valid_from,
                    )
                )
            )
            .scalars()
            .all()
        )

    index: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        index[r.employee_name].append(
            {
                "sal_type": r.sal_type,
                "rate": float(r.rate),
                "mot_pct": float(r.mot_pct) if r.mot_pct is not None else None,
                "mot_base": r.mot_base,
                "valid_from": r.valid_from,
                "valid_to": r.valid_to,
            }
        )
    return dict(index)


def get_rate_for_date(
    history: list[dict],
    target_date: date,
) -> Optional[dict]:
    """
    Найти активную запись истории на конкретную дату.

    Возвращает {"sal_type": ..., "rate": float} или None если нет данных.
    """
    for rec in reversed(history):  # ищем с конца (новые первыми)
        if rec["valid_from"] <= target_date:
            if rec["valid_to"] is None or rec["valid_to"] >= target_date:
                return rec
    return None


def get_prorated_monthly(
    history: list[dict],
    period_start: date,
    period_end: date,
    days_in_month: int,
) -> float:
    """
    Рассчитать начисление для ежемесячной ставки с учётом смены ставки внутри периода.

    Учитывает, что ставка могла измениться в середине месяца.
    Возвращает итоговую сумму начисления за период.
    """
    total = 0.0
    current = period_start
    while current <= period_end:
        rec = get_rate_for_date(history, current)
        if rec and rec["sal_type"] == "ежемесячная":
            # Найдём непрерывный отрезок с одинаковой ставкой
            seg_end = period_end
            if rec["valid_to"] and rec["valid_to"] < period_end:
                seg_end = rec["valid_to"]
            days = (seg_end - current).days + 1
            total += (rec["rate"] / days_in_month) * days
            current = seg_end + timedelta(days=1)
        else:
            current += timedelta(days=1)
    return round(total, 2)


# ═══════════════════════════════════════════════════════
# Инициализация листа (вызывается при обновлении списка сотрудников)
# ═══════════════════════════════════════════════════════


async def refresh_history_sheet_dropdowns(employee_names: list[str]) -> None:
    """Обновить выпадающий список сотрудников на листе «История ставок»."""
    await setup_salary_history_sheet(employee_names)
    logger.info(
        "[salary_history] Дропдауны обновлены: %d сотрудников", len(employee_names)
    )


_DEFAULT_HISTORY_DATE = "01.01.2020"


async def bootstrap_salary_history_sheet() -> int:
    """
    Первичное заполнение листа «История ставок» из листа «Зарплаты».

    Логика:
    - Читаем уже существующие строки в «История ставок» — их трогаем
    - Для каждого сотрудника из «Зарплаты», которого ещё нет в истории:
        * тип и ставка берутся из «Зарплаты»
        * если тип = «почасовая» — ставка из iiko (EmployeeRole.payment_per_hour)
        * дата начала = 01.01.2020 (базовая точка отсчёта)
    - Дописываем новые строки в конец листа

    Возвращает количество добавленных строк.
    """
    import asyncio as _asyncio

    # ── 1. Параллельно читаем текущую историю + настройки зарплат + данные БД ──
    existing_rows, salary_settings, (employees_db, roles_db) = await _asyncio.gather(
        read_salary_history_sheet(),
        read_salary_settings(),
        _load_db_for_bootstrap(),
    )

    # Имена уже присутствующих в листе (не дублируем)
    existing_names: set[str] = {r["name"] for r in existing_rows}

    # full_name → EmployeeRole (главная роль по employee.role_id)
    role_by_iiko_id: dict[str, EmployeeRole] = {str(r.id): r for r in roles_db}

    def _full_name_from_emp(emp: Employee) -> str:
        parts = [
            p
            for p in (emp.last_name, emp.first_name, emp.middle_name)
            if p and p.strip()
        ]
        return " ".join(parts) if parts else (emp.name or "").strip()

    # full_name → Employee
    emp_by_full_name: dict[str, Employee] = {}
    for emp in employees_db:
        fn = _full_name_from_emp(emp)
        if fn:
            emp_by_full_name[fn] = emp

    # ── 2а. Backfill iiko_id для существующих строк с пустым col F ──
    id_updates: list[dict] = []
    for r in existing_rows:
        if not r.get("iiko_id"):
            emp_found = emp_by_full_name.get(r["name"])
            if emp_found:
                id_updates.append({"row": r["row"], "iiko_id": str(emp_found.id)})
    if id_updates:
        await write_history_iiko_ids(id_updates)
        logger.info(
            "[salary_history] bootstrap: обновлено iiko_id для %d существующих строк",
            len(id_updates),
        )

    # ── 2. Строим список новых строк ──
    new_rows: list[dict] = []
    for name, settings in salary_settings.items():
        if name in existing_names:
            continue  # уже есть — не трогаем

        sal_type = settings.get("type", "")
        rate = settings.get("rate", 0.0)

        emp = emp_by_full_name.get(name)

        if sal_type == "почасовая":
            # Берём ставку из iiko (EmployeeRole.payment_per_hour)
            if emp and emp.role_id:
                role = role_by_iiko_id.get(str(emp.role_id))
                if role and role.payment_per_hour:
                    rate = float(role.payment_per_hour)

        new_rows.append(
            {
                "name": name,
                "sal_type": sal_type,
                "rate": round(rate) if sal_type != "почасовая" else rate,
                "mot_pct": settings.get("mot_pct") or "",
                "mot_base": settings.get("mot_base") or "",
                "valid_from": _DEFAULT_HISTORY_DATE,
                "iiko_id": str(emp.id) if emp else "",
            }
        )

    # Сортируем: сначала по типу, потом по имени
    new_rows.sort(key=lambda r: (r["sal_type"], r["name"].lower()))

    if not new_rows:
        logger.info("[salary_history] bootstrap: все сотрудники уже есть в истории")
        return 0

    count = await append_salary_history_rows(new_rows)
    logger.info(
        "[salary_history] bootstrap: добавлено %d новых строк в «История ставок»",
        count,
    )
    return count


async def _load_db_for_bootstrap() -> tuple[list, list]:
    """Загрузить сотрудников и роли из БД (вспомогательная функция для bootstrap)."""
    async with async_session_factory() as session:
        emps = (await session.execute(select(Employee))).scalars().all()
        roles = (await session.execute(select(EmployeeRole))).scalars().all()
    return list(emps), list(roles)
