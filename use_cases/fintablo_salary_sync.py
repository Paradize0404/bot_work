"""
Use-case: синхронизация ФОТ → FinTablo (ведомость месяца).   **v2**

Схема распределения ФОТ по направлениям FinTablo
─────────────────────────────────────────────────
Отделы определяются из маппинга Google Sheets (колонки D-E вкладки
«Маппинг FinTablo»): «Подразделение ФОТ» → «Направление FinTablo».
Направления резолвятся в direction_id через таблицу ft_direction в DB.

Маппинг строго по iiko UUID: col A маппинга содержит «Имя (uuid)»,
синхронизация ищет этот UUID в ФОТ (col L).  Имена не используются.

Начисления:

  totalPay.fix = «Начислено» (суммарная колонка C в ФОТ)
  totalPay.percent = 0
  totalPay.bonus   = 0
  totalPay.forfeit = 0

Позиции (positions):

  1. Секция «Администрация» → 50 / 50 между Московский и Клиническая.
  2. Одна рабочая секция    → 100 % в соответствующем направлении.
  3. Несколько секций       → пропорционально «Начислено» каждой секции.

Delta-sync:
  GET текущие начисления / позиции → если отличается → PUT новые.

Запуск: после ``update_fot_sheet()`` в scheduler (шаг 8) и по кнопке
«Синхр. зарплату».
"""

import asyncio
import logging
import time
from datetime import date
from typing import Any

from sqlalchemy import select

from adapters import fintablo_api
from adapters.google_sheets import (
    read_fintab_dept_direction_mapping,
    read_fintab_employee_mapping,
    read_fot_all_employees,
)
from db.engine import async_session_factory as async_session
from db.ft_models import FTDirection
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

_MONTH_NAMES_RU = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}

# ───── Направления FinTablo ─────
# Маппинг загружается динамически из Google Sheets (колонки D-E) + ft_direction (DB).
# Названия направлений для админ-сплита (50/50):
_ADMIN_DIR_NAMES: list[str] = ["Московский", "Клиническая"]


async def _load_direction_maps() -> (
    tuple[dict[str, int], dict[int, str], dict[str, int]]
):
    """
    Загрузить динамический маппинг секция ФОТ → directionId.

    1. Читает D-E из «Маппинг FinTablo»: dept_name → ft_direction_name
    2. Читает ft_direction из БД: direction_name → direction_id
    3. Соединяет: dept_name → direction_id

    Возвращает (section_to_dir, dir_to_name, all_dir_by_name)::

        section_to_dir:  {«Секция ФОТ»: directionId}  — эквивалент старого _SECTION_TO_DIRECTION
        dir_to_name:     {directionId: «Имя»}     — обратный
        all_dir_by_name: {«Имя»: directionId}   — все направления из DB
    """
    # Читаем Sheets D-E
    dept_dir_rows = await read_fintab_dept_direction_mapping()

    # Читаем ft_direction из БД
    async with async_session() as session:
        stmt = select(FTDirection.name, FTDirection.id).where(
            FTDirection.name.isnot(None)
        )
        rows = (await session.execute(stmt)).all()
    all_dir_by_name: dict[str, int] = {name: did for name, did in rows if name}

    # Строим section_to_dir: dept_name (stripped) → directionId
    section_to_dir: dict[str, int] = {}
    for row in dept_dir_rows:
        dept_raw = row["dept_name"]
        # Отсекаем суффикс «  —  Выручка: ...» как в ФОТ
        dept = dept_raw.split("  \u2014  ")[0].split("  \u2014  ")[0].strip()
        ft_dir_name = row["ft_direction_name"]
        dir_id = all_dir_by_name.get(ft_dir_name)
        if dir_id is None:
            logger.warning(
                "[fintablo_sync] D-E: направление «%s» не найдено в ft_direction",
                ft_dir_name,
            )
            continue
        section_to_dir[dept] = dir_id

    dir_to_name: dict[int, str] = {v: k for k, v in all_dir_by_name.items()}

    logger.info(
        "[fintablo_sync] Маппинг направлений: %d секций, %d ft_direction",
        len(section_to_dir),
        len(all_dir_by_name),
    )
    return section_to_dir, dir_to_name, all_dir_by_name


def _resolve_section_direction(
    sect_name: str,
    section_to_dir: dict[str, int],
) -> int | None:
    """Определить directionId по названию секции ФОТ.

    Сначала точное совпадение, затем подстрочное.
    """
    if sect_name in section_to_dir:
        return section_to_dir[sect_name]
    lower = sect_name.lower()
    for key, dir_id in section_to_dir.items():
        if key.lower() in lower or lower in key.lower():
            return dir_id
    return None


# ═══════════════════════════════════════════════════════
# Главная функция
# ═══════════════════════════════════════════════════════


async def sync_fot_to_fintablo(
    triggered_by: str | None = None,
    target_date: date | None = None,
) -> dict:
    """
    Синхронизировать начисления из листа ФОТ в FinTablo.

    ФОТ — единственный источник истины.
    Все сотрудники из маппинга получают начисления из ФОТ.
    Все остальные записи за этот месяц в FinTablo обнуляются.

    ``target_date`` — любая дата внутри нужного месяца.
    По умолчанию (None) — текущий месяц по ``now_kgd()``.

    Возвращает статистику::

        {
            "total": int,         # записей обработано
            "updated": int,       # PUT salary отправлено
            "skipped": int,       # salary без изменений
            "errors": int,        # ошибки API
            "no_fot": int,        # iiko_id не найден в ФОТ
            "pos_updated": int,   # позиции обновлены
            "zeroed": int,        # лишних записей обнулено
        }
    """
    t0 = time.monotonic()
    today = target_date or now_kgd().date()

    month_name = _MONTH_NAMES_RU[today.month]
    tab_name = f"ФОТ {month_name} {today.year}"
    date_mm_yyyy = f"{today.month:02d}.{today.year}"

    logger.info(
        "[fintablo_sync] Старт v2: вкладка «%s», период %s, triggered_by=%s",
        tab_name,
        date_mm_yyyy,
        triggered_by,
    )

    # 1. Читаем маппинг + ФОТ + текущих сотрудников FT + направления параллельно
    mapping_rows, fot_result, ft_employees, dir_maps = await asyncio.gather(
        read_fintab_employee_mapping(),
        read_fot_all_employees(tab_name),
        fintablo_api.fetch_employees(),
        _load_direction_maps(),
    )
    fot_by_uuid, _fot_by_name = fot_result
    section_to_dir, dir_to_name, all_dir_by_name = dir_maps

    # Направления для сплита админ-персонала
    admin_directions = [
        all_dir_by_name[n] for n in _ADMIN_DIR_NAMES if n in all_dir_by_name
    ]
    missing_admin = [n for n in _ADMIN_DIR_NAMES if n not in all_dir_by_name]
    if missing_admin:
        logger.error(
            "[fintablo_sync] В ft_direction отсутствуют админ-направления: %s. "
            "Сплит администрации будет неполным!",
            missing_admin,
        )

    if not mapping_rows:
        logger.info("[fintablo_sync] Маппинг пуст — пропускаем")
        return {
            "total": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "no_fot": 0,
            "pos_updated": 0,
            "zeroed": 0,
        }

    # 1b. Все salary-записи за месяц (для обнуления лишних)
    all_salary_entries = await fintablo_api.get_salary(date_mm_yyyy=date_mm_yyyy)

    # 2. Строим mapping → fintab + fot_data (1:1 маппинг, строго по iiko UUID)
    stats = {
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "no_fot": 0,
        "pos_updated": 0,
        "zeroed": 0,
    }

    employees_to_sync: list[dict] = []  # [{fintab_id, fintab_name, emp_data}]
    seen_ft_ids: set[int] = set()
    for row in mapping_rows:
        fintab_id = row["fintab_id"]
        if fintab_id in seen_ft_ids:
            continue  # дедупликация
        seen_ft_ids.add(fintab_id)

        iiko_id = row.get("iiko_id", "")
        if not iiko_id:
            logger.warning(
                "[fintablo_sync] «%s» → FT:%d — нет iiko UUID в маппинге, пропускаем",
                row.get("fintab_name", ""),
                fintab_id,
            )
            stats["no_fot"] += 1
            continue

        emp_data = fot_by_uuid.get(iiko_id)
        if emp_data is None:
            logger.warning(
                "[fintablo_sync] «%s» → FT:%d — iiko UUID %s не найден в ФОТ",
                row.get("fintab_name", ""),
                fintab_id,
                iiko_id,
            )
            stats["no_fot"] += 1
            continue

        employees_to_sync.append(
            {
                "fintab_id": fintab_id,
                "fintab_name": row.get("fintab_name", ""),
                "emp_data": emp_data,
            }
        )

    # Индексируем текущих FT-сотрудников
    ft_emp_by_id: dict[int, dict] = {
        emp["id"]: emp for emp in ft_employees if emp.get("id")
    }

    # 3. Обрабатываем каждого сотрудника
    for emp_rec in employees_to_sync:
        fintab_id = emp_rec["fintab_id"]
        ft_name = emp_rec["fintab_name"] or str(fintab_id)
        emp_data = emp_rec["emp_data"]
        stats["total"] += 1

        by_dept = emp_data.get("by_dept", {})
        total_accrued = emp_data["total"].get("accrued", 0.0)

        desired_pay: dict[str, int] = {
            "fix": round(total_accrued),
            "percent": 0,
            "bonus": 0,
            "forfeit": 0,
        }

        desired_positions = _compute_positions(
            by_dept, section_to_dir, dir_to_name, admin_directions
        )

        try:
            # ── Salary delta-sync ──
            #   PUT /v1/salary/{id} — полная перезапись totalPay,
            #   не аддитивная, все поля (fix/percent/bonus/forfeit)
            #   передаются явно.  Delta-check лишь оптимизация
            #   (пропуск PUT когда данные не изменились).
            salary_items = await fintablo_api.get_salary(
                employee_id=fintab_id,
                date_mm_yyyy=date_mm_yyyy,
            )
            current_pay = _extract_current_totalpay(salary_items, date_mm_yyyy)

            pay_changed = any(
                abs(desired_pay[k] - current_pay.get(k, 0.0)) >= 0.01
                for k in desired_pay
            )

            if pay_changed:
                logger.info(
                    "[fintablo_sync] «%s» (FT:%d)" " fix: %.0f → %.0f",
                    ft_name,
                    fintab_id,
                    current_pay.get("fix", 0),
                    desired_pay["fix"],
                )
                await fintablo_api.update_salary(
                    employee_id=fintab_id,
                    date_mm_yyyy=date_mm_yyyy,
                    total_pay=desired_pay,
                )
                stats["updated"] += 1
            else:
                stats["skipped"] += 1

            # ── Positions delta-sync ──
            current_emp = ft_emp_by_id.get(fintab_id)
            if current_emp and desired_positions:
                pos_changed = await _sync_positions(
                    fintab_id,
                    ft_name,
                    current_emp,
                    desired_positions,
                    date_mm_yyyy,
                    section_to_dir=section_to_dir,
                    dir_to_name=dir_to_name,
                )
                if pos_changed:
                    stats["pos_updated"] += 1

        except Exception:
            logger.exception(
                "[fintablo_sync] Ошибка: «%s» (FT:%d)",
                ft_name,
                fintab_id,
            )
            stats["errors"] += 1

    # 4. Обнулить лишние salary-записи (не из маппинга)
    #    ФОТ — единственный источник истины.
    synced_ft_ids = {e["fintab_id"] for e in employees_to_sync}
    _ZERO_PAY: dict[str, float] = {
        "fix": 0.0,
        "percent": 0.0,
        "bonus": 0.0,
        "forfeit": 0.0,
    }
    for sal_entry in all_salary_entries:
        ft_id = sal_entry.get("id")
        if ft_id is None or ft_id in synced_ft_ids:
            continue
        # Проверяем, что totalPay не нулевой (нет смысла обнулять нули)
        tp = sal_entry.get("totalPay") or {}
        current_amount = sum(
            abs(float(tp.get(k) or 0)) for k in ("fix", "percent", "bonus", "forfeit")
        )
        if current_amount < 0.01:
            continue
        ft_name = sal_entry.get("name", str(ft_id))
        try:
            logger.info(
                "[fintablo_sync] Обнуление лишней записи: «%s» (FT:%d) " "fix=%.0f → 0",
                ft_name,
                ft_id,
                float(tp.get("fix") or 0),
            )
            await fintablo_api.update_salary(
                employee_id=ft_id,
                date_mm_yyyy=date_mm_yyyy,
                total_pay=_ZERO_PAY,
            )
            stats["zeroed"] += 1
        except Exception:
            logger.exception(
                "[fintablo_sync] Ошибка обнуления: «%s» (FT:%d)",
                ft_name,
                ft_id,
            )
            stats["errors"] += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "[fintablo_sync] Завершено за %.1f сек: "
        "total=%d updated=%d skipped=%d errors=%d no_fot=%d "
        "pos_updated=%d zeroed=%d",
        elapsed,
        stats["total"],
        stats["updated"],
        stats["skipped"],
        stats["errors"],
        stats["no_fot"],
        stats["pos_updated"],
        stats["zeroed"],
    )
    return stats


# ═══════════════════════════════════════════════════════
# Определение позиций по секциям ФОТ
# ═══════════════════════════════════════════════════════


def _compute_positions(
    by_dept: dict[str, dict],
    section_to_dir: dict[str, int],
    dir_to_name: dict[int, str],
    admin_directions: list[int],
) -> list[dict[str, Any]]:
    """
    По секциям ФОТ рассчитать желаемые позиции для FinTablo.

    Возвращает::

        [
            {
                "directionId": int,
                "percentage": int,       # 0-100, сумма = 100
                "department": str,
                "type": "direct-production" | "administrative",
            },
            ...
        ]

    Начисления одного направления (рабочая секция + Администрация)
    суммируются в одну позицию (без дублей по directionId).
    """
    dir_accrued: dict[int, float] = {}
    dir_is_work: dict[int, bool] = {}

    for sect_name, vals in by_dept.items():
        accrued = vals.get("accrued", 0.0)
        if sect_name == "Администрация":
            if accrued < 0.01 or not admin_directions:
                continue
            share = accrued / len(admin_directions)
            for d in admin_directions:
                dir_accrued[d] = dir_accrued.get(d, 0.0) + share
            continue

        dir_id = _resolve_section_direction(sect_name, section_to_dir)
        if dir_id is not None:
            dir_accrued[dir_id] = dir_accrued.get(dir_id, 0.0) + accrued
            dir_is_work[dir_id] = True
        else:
            logger.warning(
                "[fintablo_sync] Неизвестная секция ФОТ «%s» — пропускаем",
                sect_name,
            )

    if not dir_accrued:
        return []

    total = sum(dir_accrued.values())

    positions: list[dict[str, Any]] = []
    for dir_id in sorted(dir_accrued):
        amount = dir_accrued[dir_id]
        pct = round(amount / total * 100) if total > 0 else 0
        pos_type = "direct-production" if dir_is_work.get(dir_id) else "administrative"
        positions.append(
            {
                "directionId": dir_id,
                "percentage": pct,
                "department": dir_to_name.get(dir_id, ""),
                "type": pos_type,
            }
        )

    _normalize_percentages(positions)
    return positions


def _normalize_percentages(positions: list[dict]) -> None:
    """Подогнать проценты чтобы сумма была ровно 100 %."""
    if not positions:
        return
    total_pct = sum(p["percentage"] for p in positions)
    if total_pct == 100 or total_pct == 0:
        return
    # Корректируем позицию с наибольшим процентом
    positions.sort(key=lambda p: p["percentage"], reverse=True)
    positions[0]["percentage"] += 100 - total_pct


# ═══════════════════════════════════════════════════════
# Синхронизация позиций (positions) сотрудника
# ═══════════════════════════════════════════════════════


async def _sync_positions(
    fintab_id: int,
    ft_name: str,
    current_emp: dict,
    desired: list[dict[str, Any]],
    date_mm_yyyy: str,
    *,
    section_to_dir: dict[str, int] | None = None,
    dir_to_name: dict[int, str] | None = None,
) -> bool:
    """
    Сравнить текущие позиции сотрудника с желаемыми.
    Если отличаются — обновить через PUT /v1/employees/{id}.

    Возвращает True если позиции были обновлены.
    """
    current_positions = current_emp.get("positions") or []

    # Сравниваем по (directionId, percentage) — игнорируем dummy 0 %
    def _pos_key(p: dict) -> tuple:
        return (
            p.get("directionId"),
            p.get("percentage"),
            p.get("department", ""),
            p.get("type", ""),
        )

    cur_set = {_pos_key(p) for p in current_positions if p.get("percentage", 0) > 0}
    des_set = {_pos_key(p) for p in desired if p.get("percentage", 0) > 0}

    if cur_set == des_set:
        return False

    logger.info(
        "[fintablo_sync] «%s» (FT:%d) позиции: %s → %s",
        ft_name,
        fintab_id,
        _pos_summary(current_positions, dir_to_name),
        _pos_summary(desired, dir_to_name),
    )

    # Формируем тело запроса
    positions_body = _build_positions_body(
        desired,
        current_positions,
        section_to_dir=section_to_dir,
        dir_to_name=dir_to_name,
    )

    # ── Защита от API-бага FinTablo ──
    # 1 позиция без positionId приводит к удалению сотрудника на стороне FT.
    has_position_id = any(p.get("positionId") for p in positions_body)
    if len(positions_body) == 1 and not has_position_id:
        logger.error(
            "[fintablo_sync] ОТМЕНА PUT employees/%d «%s»: "
            "1 позиция без positionId — сработал бы API-баг FinTablo. "
            "Проверьте маппинг направлений (D-E) и ft_direction.",
            fintab_id,
            ft_name,
        )
        return False

    await fintablo_api.update_employee(
        employee_id=fintab_id,
        body={
            "date": date_mm_yyyy,
            "positions": positions_body,
        },
    )
    return True


def _build_positions_body(
    desired: list[dict[str, Any]],
    current: list[dict],
    *,
    section_to_dir: dict[str, int] | None = None,
    dir_to_name: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Построить массив positions для PUT /v1/employees/{id}.

    Стратегия:
    - Все *желаемые* позиции включаются с правильным %.
    - Все *текущие* позиции, отсутствующие в желаемых, включаются
      с percentage=0 (обнуляем, а не удаляем — API не убирает
      неупомянутые позиции).
    - positionId переиспользуется если directionId совпадает.
    - Если итого только 1 запись — добавляем dummy (0 %) чтобы обойти
      API-баг (1 позиция без positionId удаляет все).
    """
    cur_by_dir: dict[int, dict] = {}
    for p in current:
        d = p.get("directionId")
        if d and d not in cur_by_dir:
            cur_by_dir[d] = p

    body: list[dict[str, Any]] = []
    used_dirs: set[int] = set()

    # Желаемые позиции
    for pos in desired:
        entry: dict[str, Any] = {
            "type": pos.get("type", "direct-production"),
            "directionId": pos["directionId"],
            "percentage": pos["percentage"],
            "department": pos.get("department", ""),
        }
        existing = cur_by_dir.get(pos["directionId"])
        if existing and existing.get("positionId"):
            entry["positionId"] = existing["positionId"]
        body.append(entry)
        used_dirs.add(pos["directionId"])

    # Текущие позиции, которых нет среди желаемых → обнуляем
    for dir_id, cur_pos in cur_by_dir.items():
        if dir_id in used_dirs:
            continue
        entry: dict[str, Any] = {
            "type": cur_pos.get("type", "direct-production"),
            "directionId": dir_id,
            "percentage": 0,
            "department": cur_pos.get("department", ""),
        }
        if cur_pos.get("positionId"):
            entry["positionId"] = cur_pos["positionId"]
        body.append(entry)
        used_dirs.add(dir_id)

    # Обходной приём: если только 1 запись — нужна хотя бы ещё одна
    if len(body) == 1:
        dummy_dir = _pick_dummy_direction(
            used_dirs,
            section_to_dir or {},
            dir_to_name or {},
        )
        if dummy_dir:
            existing_dummy = cur_by_dir.get(dummy_dir)
            name_map = dir_to_name or {}
            dummy: dict[str, Any] = {
                "type": "direct-production",
                "directionId": dummy_dir,
                "percentage": 0,
                "department": name_map.get(dummy_dir, ""),
            }
            if existing_dummy and existing_dummy.get("positionId"):
                dummy["positionId"] = existing_dummy["positionId"]
            body.append(dummy)
        else:
            logger.warning(
                "[fintablo_sync] Не удалось подобрать dummy-направление "
                "для обхода API-бага (used_dirs=%s). PUT может быть опасен.",
                used_dirs,
            )

    return body


def _pick_dummy_direction(
    used: set[int],
    section_to_dir: dict[str, int],
    dir_to_name: dict[int, str] | None = None,
) -> int | None:
    """Выбрать направление, не задействованное в реальных позициях.

    Сначала ищем среди section_to_dir (маппинг секций),
    затем среди всех направлений из ft_direction (fallback).
    """
    for dir_id in section_to_dir.values():
        if dir_id not in used:
            return dir_id
    # Fallback: любое направление из БД, не задействованное
    if dir_to_name:
        for dir_id in dir_to_name:
            if dir_id not in used:
                return dir_id
    return None


def _pos_summary(
    positions: list[dict],
    dir_to_name: dict[int, str] | None = None,
) -> str:
    """Краткое описание позиций для лога."""
    name_map = dir_to_name or {}
    parts = []
    for p in positions:
        name = name_map.get(p.get("directionId", 0), "?")
        pct = p.get("percentage", 0)
        parts.append(f"{name}:{pct}%")
    return ", ".join(parts) or "∅"


# ═══════════════════════════════════════════════════════
# Вспомогательные функции
# ═══════════════════════════════════════════════════════


def _extract_current_totalpay(
    salary_items: list[dict],
    date_mm_yyyy: str,
) -> dict[str, float]:
    """
    Из списка записей GET /v1/salary извлечь totalPay за нужный месяц.

    Возвращает {'fix': float, 'percent': float, 'bonus': float, 'forfeit': float}.
    """
    for item in salary_items:
        if item.get("date") == date_mm_yyyy:
            tp = item.get("totalPay") or {}
            return {
                "fix": float(tp.get("fix") or 0),
                "percent": float(tp.get("percent") or 0),
                "bonus": float(tp.get("bonus") or 0),
                "forfeit": float(tp.get("forfeit") or 0),
            }
    return {"fix": 0.0, "percent": 0.0, "bonus": 0.0, "forfeit": 0.0}
