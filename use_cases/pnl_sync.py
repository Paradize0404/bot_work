"""
Use-case: синхронизация ОПИУ (Отчёт о прибылях и убытках).

Логика:
  1. Загрузить отчёт по проводкам из iiko (OLAP TRANSACTIONS по пресету)
  2. Считать маппинг iiko Account.Name → FT PnL category из Google Sheets
  3. Агрегировать суммы по FT PnL-категориям:
     - Приоритет: Product.SecondParent (группа) > Account.Name (счёт)
     - WRITEOFF → «Списания продуктов» + вычет из исходной категории
  4. Для каждой FT-категории — параллельная синхронизация через asyncio.gather
  5. Ежедневный запуск в 07:00 по расписанию

Маппинг хранится в Google Sheets (лист «Маппинг FinTablo», колонки F–G).
Бот-записи в FinTablo помечаются comment="iiko-bot-auto".

WRITEOFF-логика:
  - Списания пишутся в отдельную категорию «Списания продуктов».
  - Одновременно сумма WRITEOFF вычитается из той FT-категории,
    куда попала бы строка по стандартному маппингу (группа > счёт).
    Это предотвращает задвоение: «Сырьевая себестоимость» учитывает
    закуп-минус-списания, а «Списания» — отдельная статья расхода.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from adapters import fintablo_api
from adapters import iiko_api
from adapters.google_sheets import read_fintab_all_mappings
from db.engine import async_session_factory as async_session
from db.ft_models import FTDirection, FTPnlCategory
from db.models import Entity, ProductGroup
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

# Пресет OLAP-отчёта «Статьи и закуп по складам БОТ»
OPIU_PRESET_ID = "4120ac6e-b8b3-4e97-bd75-dda8c864b4c3"

# Метка для PnL-записей, созданных ботом (чтобы отличать от ручных)
BOT_COMMENT = "iiko-bot-auto"

# Название FT PnL-категории, куда попадают списания (WRITEOFF)
WRITEOFF_FT_CATEGORY_NAME = "Списания продуктов"


# ═══════════════════════════════════════════════════════
# CRUD маппинга (DB)
# ═══════════════════════════════════════════════════════


async def get_all_mappings() -> list[dict]:
    """Вернуть все активные маппинги из листа «Маппинг FinTablo» (колонки F-G)."""
    both = await read_fintab_all_mappings()
    return both["opiu"]


async def get_available_ft_categories() -> list[dict]:
    """Вернуть все FT PnL-категории из локальной БД (синхронизированные)."""
    async with async_session() as session:
        stmt = select(FTPnlCategory).order_by(FTPnlCategory.name)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "type": r.type,
                "pnl_type": r.pnl_type,
            }
            for r in rows
        ]


# ═══════════════════════════════════════════════════════
# Получение iiko-счетов из OLAP-отчёта
# ═══════════════════════════════════════════════════════


def _normalize_dept(name: str) -> str:
    """Нормализовать имя подразделения для сравнения.

    OLAP отдаёт имена вида 'PizzaYolo / Пицца Йоло (Московский)' или
    'Клиническая PizzaYolo', а маппинг содержит 'Клиническая' / 'Московский'.
    Удаляем шум: 'PizzaYolo', 'Пицца Йоло', спец-символы, лишние пробелы.
    """
    s = name.lower()
    # удаляем бренд-суффиксы
    for noise in ("pizzayolo", "пицца йоло", "pizza yolo"):
        s = s.replace(noise, "")
    # удаляем спец-символы и скобки
    s = s.replace("/", " ").replace("(", " ").replace(")", " ")
    return " ".join(s.split()).strip()


def _resolve_department_direction(
    dept_name: str,
    dept_dir_map: list[dict],
) -> str | None:
    """Определить название FT-направления по Department из OLAP.

    Порядок:
      1. Точное совпадение (lower)
      2. Нормализованное совпадение (убрано PizzaYolo и т.п.)
      3. Подстрочное (маппинг-ключ ⊂ department или наоборот)
    """
    lower = dept_name.lower()
    norm = _normalize_dept(dept_name)

    # 1  точное
    for m in dept_dir_map:
        if m["dept_name"].lower() == lower:
            return m["ft_direction_name"]

    # 2  нормализованное
    for m in dept_dir_map:
        if _normalize_dept(m["dept_name"]) == norm:
            return m["ft_direction_name"]
        # маппинг-ключ содержится в нормализованном OLAP dept
        m_norm = _normalize_dept(m["dept_name"])
        if m_norm and (m_norm in norm or norm in m_norm):
            return m["ft_direction_name"]

    # 3  подстрочное (как было)
    for m in dept_dir_map:
        key = m["dept_name"].lower()
        if key in lower or lower in key:
            return m["ft_direction_name"]

    return None


async def _get_writeoff_category_id() -> int | None:
    """Найти FT PnL-категорию «Списания продуктов» по имени в БД."""
    async with async_session() as session:
        stmt = (
            select(FTPnlCategory.id)
            .where(FTPnlCategory.name == WRITEOFF_FT_CATEGORY_NAME)
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
    return row


async def _get_direction_map() -> dict[str, int]:
    """Вернуть {direction_name: direction_id} из ft_direction."""
    async with async_session() as session:
        stmt = select(FTDirection.name, FTDirection.id).where(
            FTDirection.name.isnot(None)
        )
        rows = (await session.execute(stmt)).all()
    return {name: did for name, did in rows if name}


async def fetch_iiko_accounts_from_preset(
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Загрузить OLAP-отчёт по пресету и вернуть сырые строки.

    date_from, date_to — формат YYYY-MM-DDTHH:MM:SS (ISO).
    Возвращает::

        [{
            "account_name": str,
            "product_group": str | None,
            "department": str | None,
            "transaction_type": str | None,
            "sum": float,
        }, ...]
    """
    rows = await iiko_api.fetch_olap_by_preset(OPIU_PRESET_ID, date_from, date_to)

    result: list[dict] = []
    for row in rows:
        account_name = (row.get("Account.Name") or row.get("AccountName") or "").strip()
        if not account_name:
            continue

        # Определяем тип транзакции ДО фильтрации по сумме
        tt = (row.get("TransactionType") or "").strip() or None
        is_writeoff = (tt or "").upper() == "WRITEOFF"

        # Для WRITEOFF: Sum.Incoming может быть 0 (расход, не приход).
        # Пробуем Sum.Outgoing, Sum.Amount, Sum.Cost как фолбэки.
        try:
            value = float(row.get("Sum.Incoming", 0) or 0)
        except (ValueError, TypeError):
            value = 0.0

        if is_writeoff and value <= 0:
            for alt_field in ("Sum.Outgoing", "Sum.Amount", "Sum.Cost"):
                try:
                    alt = float(row.get(alt_field, 0) or 0)
                except (ValueError, TypeError):
                    alt = 0.0
                if alt > 0:
                    value = alt
                    logger.info(
                        "[pnl_sync] WRITEOFF: использую %s=%.2f вместо Sum.Incoming=0"
                        " (account=%s)",
                        alt_field,
                        alt,
                        account_name,
                    )
                    break
            # Если ВСЕ поля 0 — используем abs(Sum.Incoming) на случай отрицательного
            if value <= 0:
                try:
                    value = abs(float(row.get("Sum.Incoming", 0) or 0))
                except (ValueError, TypeError):
                    value = 0.0
            if value <= 0:
                logger.warning(
                    "[pnl_sync] WRITEOFF: все суммы = 0, пропускаю строку"
                    " (account=%s, row_keys=%s)",
                    account_name,
                    list(row.keys()),
                )
                continue
        elif value <= 0:
            continue

        pg = (row.get("Product.SecondParent") or "").strip() or None
        dept = (row.get("Department") or "").strip() or None
        result.append(
            {
                "account_name": account_name,
                "product_group": pg,
                "department": dept,
                "transaction_type": tt,
                "sum": value,
            }
        )

    return result


async def get_distinct_iiko_accounts() -> list[str]:
    """
    Получить список вариантов для колонки F (Счет iiko / Группа).

    Возвращает объединённый отсортированный список:
    - Счета из iiko_entity (root_type='Account') → «Название (uuid)»
    - Товарные группы из iiko_product_group → «[Группа] Название»

    Записи вида «[Группа] Алкоголь» используются в маппинге ОПИУ для
    сопоставления с полем Product.SecondParent из OLAP-отчёта.
    """
    async with async_session() as session:
        # Счета
        stmt_acc = (
            select(Entity.name, Entity.id)
            .where(Entity.root_type == "Account")
            .where(Entity.deleted.is_(False))
            .where(Entity.name.isnot(None))
            .order_by(Entity.name)
        )
        rows_acc = (await session.execute(stmt_acc)).all()

        # Товарные группы (Product.SecondParent в OLAP)
        stmt_grp = (
            select(ProductGroup.name)
            .where(ProductGroup.deleted.is_(False))
            .where(ProductGroup.name.isnot(None))
            .order_by(ProductGroup.name)
        )
        rows_grp = (await session.execute(stmt_grp)).scalars().all()

    seen: set[str] = set()
    result: list[str] = []

    for name, uid in rows_acc:
        label = f"{name} ({uid})"
        if label not in seen:
            seen.add(label)
            result.append(label)

    for grp_name in rows_grp:
        label = f"[Группа] {grp_name}"
        if label not in seen:
            seen.add(label)
            result.append(label)

    return sorted(result)


async def get_distinct_product_groups_2nd_level() -> list[str]:
    """
    Получить список уникальных номенклатурных групп 2-го уровня от корня.

    Используется для дропдауна в колонке L маппинга FinTablo:
    «Ном. группа (закуп ТМЦ/Хозы)» → «Статья FinTablo (Закуп)».

    Иерархия: корень → уровень 1 (SecondParent) → уровень 2 → ... → товар.
    Возвращает названия групп на уровне 1 (первый ребёнок корня).
    """
    async with async_session() as session:
        stmt = select(ProductGroup.id, ProductGroup.name, ProductGroup.parent_id).where(
            ProductGroup.deleted.is_(False),
            ProductGroup.name.isnot(None),
        )
        rows = (await session.execute(stmt)).all()

    pg_map = {str(r[0]): {"name": r[1], "parent_id": str(r[2]) if r[2] else ""} for r in rows}

    # Найти корни (без parent или parent не в карте)
    roots = set()
    for gid, info in pg_map.items():
        if not info["parent_id"] or info["parent_id"] not in pg_map:
            roots.add(gid)

    # Уровень 1 = дети корней
    level1_names: set[str] = set()
    for gid, info in pg_map.items():
        if info["parent_id"] in roots and gid not in roots:
            level1_names.add(info["name"])

    return sorted(level1_names)


# ═══════════════════════════════════════════════════════
# Основная логика: обновление ОПИУ в FinTablo
# ═══════════════════════════════════════════════════════


async def update_opiu(
    *,
    triggered_by: str | None = None,
    target_date: datetime | None = None,
) -> dict:
    """
    Обновить ОПИУ в FinTablo по данным iiko.

    Алгоритм:
      1. Определить месяц (target_date или текущий)
      2. Загрузить OLAP-отчёт из iiko за весь месяц
      3. Считать маппинг из Google Sheets (F-G для категорий, D-E для направлений)
      4. Агрегировать по FT PnL-категориям:
         - ВСЕ транзакции разбиваются по department → FT direction (из D-E)
         - WRITEOFF → хардкод «Списания продуктов»
         - Остальные → приоритет группа > счёт (из F-G)
      5. Зачистить устаревшие бот-записи без direction
      6. Создать / обновить бот-записи в FinTablo

    Возвращает::

        {
            "updated": int,
            "skipped": int,
            "errors": int,
            "details": list[str],
            "unmapped_keys": list[str],  # строки без маппинга
            "elapsed": float,
        }
    """
    t0 = time.monotonic()
    now = target_date or now_kgd()
    date_mm_yyyy = now.strftime("%m.%Y")
    trigger_label = triggered_by or "manual"

    logger.info(
        "[pnl_sync] Старт ОПИУ за %s (triggered_by=%s)", date_mm_yyyy, trigger_label
    )

    # ── 1. Период: весь месяц ──
    first_day = now.replace(day=1)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    date_from = first_day.strftime("%Y-%m-%dT00:00:00")
    date_to = next_month.strftime("%Y-%m-%dT00:00:00")

    # ── 2. Загрузить данные из iiko (сырые строки) ──
    iiko_rows = await fetch_iiko_accounts_from_preset(date_from, date_to)
    logger.info(
        "[pnl_sync] iiko: %d строк, total=%.2f",
        len(iiko_rows),
        sum(r["sum"] for r in iiko_rows),
    )

    # ── 3. Считать маппинг + справочные данные (один вызов GSheets) ──
    all_maps = await read_fintab_all_mappings()
    mappings = all_maps["opiu"]
    dept_dir_map = all_maps["dept_direction"]

    writeoff_cat_id = await _get_writeoff_category_id()
    direction_map = await _get_direction_map()  # name → id

    if not mappings and writeoff_cat_id is None:
        logger.warning("[pnl_sync] Маппинг пуст и нет категории списаний")
        return {
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "details": ["Маппинг пуст"],
            "unmapped_keys": [],
            "elapsed": 0,
        }

    # Индекс маппинга: ключ iiko → (cat_id, cat_name)
    mapping_index: dict[str, tuple[int, str]] = {
        m["iiko_account_name"]: (m["ft_pnl_category_id"], m["ft_pnl_category_name"])
        for m in mappings
    }

    # ── 4. Агрегация ──
    #   ВСЕ транзакции разбиваются по department → direction (из D-E)
    #   WRITEOFF → «Списания продуктов» + вычет из исходной категории
    #   Остальные → приоритет: [Группа] > Account.Name (маппинг F-G)
    #   Ключ: (cat_id, direction_id | None)
    ft_totals: dict[tuple[int, int | None], float] = defaultdict(float)
    ft_names: dict[int, str] = {}
    unmapped_keys: set[str] = set()
    unmapped_sums: dict[str, float] = defaultdict(float)  # ключ → сумма

    # WRITEOFF-вычеты: сколько списаний вычесть из каждой (cat_id, dir_id)
    writeoff_deductions: dict[tuple[int, int | None], float] = defaultdict(float)

    for row in iiko_rows:
        value = row["sum"]
        account_name = row["account_name"]
        product_group = row["product_group"]
        department = row["department"]
        tx_type = (row["transaction_type"] or "").upper()

        # ── Определяем direction для ВСЕХ транзакций ──
        dir_name = (
            _resolve_department_direction(department, dept_dir_map)
            if department
            else None
        )
        dir_id = direction_map.get(dir_name) if dir_name else None
        if department and dir_id is None:
            ukey = f"dept={department} (нет направления)"
            unmapped_keys.add(ukey)

        # ── Определяем FT-категорию по стандартному маппингу ──
        mapped_cat_id: int | None = None
        mapped_cat_name: str = ""
        if product_group:
            group_key = f"[Группа] {product_group}"
            if group_key in mapping_index:
                mapped_cat_id, mapped_cat_name = mapping_index[group_key]
        if mapped_cat_id is None and account_name in mapping_index:
            mapped_cat_id, mapped_cat_name = mapping_index[account_name]

        # ── WRITEOFF → «Списания продуктов» + вычет из исходной категории ──
        if tx_type == "WRITEOFF":
            if writeoff_cat_id is None:
                ukey = f"WRITEOFF (нет категории «{WRITEOFF_FT_CATEGORY_NAME}»)"
                unmapped_keys.add(ukey)
                unmapped_sums[ukey] += value
                continue
            # Записываем в «Списания продуктов»
            ft_totals[(writeoff_cat_id, dir_id)] += value
            ft_names[writeoff_cat_id] = WRITEOFF_FT_CATEGORY_NAME
            # Трекаем вычет из исходной категории
            if mapped_cat_id is not None:
                writeoff_deductions[(mapped_cat_id, dir_id)] += value
                ft_names[mapped_cat_id] = mapped_cat_name
            else:
                logger.warning(
                    "[pnl_sync] WRITEOFF без маппинга: account=%s group=%s sum=%.2f"
                    " — не вычтено из категории",
                    account_name,
                    product_group,
                    value,
                )
            continue

        # ── INVOICE / INCOMING_SERVICE → маппинг ──
        if mapped_cat_id is None:
            unmapped_keys.add(account_name)
            unmapped_sums[account_name] += value
            continue

        ft_totals[(mapped_cat_id, dir_id)] += value
        ft_names[mapped_cat_id] = mapped_cat_name

    # ── Применяем WRITEOFF-вычеты ──
    for key, deduction in writeoff_deductions.items():
        if key in ft_totals:
            old = ft_totals[key]
            ft_totals[key] = old - deduction
            cat_id_d, dir_id_d = key
            logger.info(
                "[pnl_sync] WRITEOFF-вычет: cat=%s dir=%s  %.2f → %.2f (-%0.2f)",
                ft_names.get(cat_id_d, str(cat_id_d)),
                dir_id_d,
                old,
                ft_totals[key],
                deduction,
            )
        else:
            # Есть списания но нет прихода — вычитать неоткуда, логируем
            logger.warning(
                "[pnl_sync] WRITEOFF-вычет: cat=%d dir=%s — нет прихода, вычет %.2f проигнорирован",
                key[0],
                key[1],
                deduction,
            )

    # Округлить
    for key in ft_totals:
        ft_totals[key] = round(ft_totals[key], 2)

    # Убираем нулевые/отрицательные  (после вычетов категория может обнулиться)
    ft_totals = {k: v for k, v in ft_totals.items() if v > 0.005}

    total_allocated = sum(ft_totals.values())
    total_unmapped = sum(unmapped_sums.values())
    total_incoming = sum(r["sum"] for r in iiko_rows)
    total_writeoff_ded = sum(writeoff_deductions.values())

    logger.info(
        "[pnl_sync] Агрегация: %d записей FT, allocated=%.2f, unmapped=%.2f (%d keys), writeoff_deducted=%.2f",
        len(ft_totals),
        total_allocated,
        total_unmapped,
        len(unmapped_keys),
        total_writeoff_ded,
    )

    # ── Контроль целостности ──
    # total_incoming = allocated + unmapped + writeoff_deductions (writeoff вычтено)
    expected = total_allocated + total_unmapped + total_writeoff_ded
    discrepancy = abs(total_incoming - expected)
    if discrepancy > 1.0:
        logger.warning(
            "[pnl_sync] ⚠️ Расхождение целостности: iiko=%.2f, FT+unmapped+ded=%.2f, delta=%.2f",
            total_incoming,
            expected,
            discrepancy,
        )

    # ── 5. Зачистка старых бот-записей без direction ──
    # Если категория теперь разбивается по направлениям, удалить
    # устаревшие записи с direction_id=None.
    cats_with_dir = {cat_id for cat_id, d_id in ft_totals if d_id is not None}
    for cat_id in cats_with_dir:
        if (cat_id, None) not in ft_totals:
            # старые записи без direction — зачищаем
            try:
                all_items = await fintablo_api.fetch_pnl_items(
                    category_id=cat_id,
                    date_mm_yyyy=date_mm_yyyy,
                )
                for it in all_items:
                    if not (it.get("comment") or "").startswith(BOT_COMMENT):
                        continue
                    # Удаляем только записи БЕЗ direction
                    if it.get("directionId") or it.get("direction_id"):
                        continue
                    await fintablo_api.delete_pnl_item(int(it["id"]))
                    logger.info(
                        "[pnl_sync] Зачищена старая запись без direction: cat=%d id=%s",
                        cat_id,
                        it["id"],
                    )
            except Exception:
                logger.exception(
                    "[pnl_sync] Ошибка зачистки directionless для cat=%d", cat_id
                )

    # ── 6. Обновить каждую FT-категорию параллельно ──
    updated = 0
    skipped = 0
    errors = 0
    details: list[str] = []

    # Подготовка задач
    tasks: list[tuple[int, int | None, float, str]] = []
    for (cat_id, direction_id), iiko_total in ft_totals.items():
        cat_name = ft_names.get(cat_id, f"ID:{cat_id}")
        dir_label = ""
        if direction_id:
            dir_label = next(
                (n for n, d in direction_map.items() if d == direction_id), ""
            )
        display_name = f"{cat_name} / {dir_label}" if dir_label else cat_name
        tasks.append((cat_id, direction_id, iiko_total, display_name))

    # Параллельный запуск (asyncio.gather)
    async def _do_sync(
        cat_id: int, direction_id: int | None, iiko_total: float, display_name: str
    ) -> tuple[str, str]:
        """Возвращает (status, display_name)."""
        try:
            result = await _sync_one_category(
                cat_id,
                display_name,
                iiko_total,
                date_mm_yyyy,
                direction_id=direction_id,
            )
            return (result, display_name if result != "skipped" else "")
        except Exception:
            logger.exception(
                "[pnl_sync] Ошибка для %s (id=%d, dir=%s)",
                display_name,
                cat_id,
                direction_id,
            )
            return ("error", display_name)

    results = await asyncio.gather(
        *[_do_sync(cid, did, total, dn) for cid, did, total, dn in tasks]
    )

    for (cid, did, total, dn), (status, _) in zip(tasks, results):
        if status == "updated":
            updated += 1
            details.append(f"✅ {dn}: {total:.2f}")
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors += 1
            details.append(f"❌ {dn}: ошибка")
        else:
            details.append(f"ℹ️ {dn}: {status}")

    elapsed = time.monotonic() - t0

    logger.info(
        "[pnl_sync] ОПИУ за %s завершён: upd=%d, skip=%d, err=%d, %.1f сек",
        date_mm_yyyy,
        updated,
        skipped,
        errors,
        elapsed,
    )

    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "details": details,
        "unmapped_keys": sorted(unmapped_keys),
        "unmapped_sums": dict(unmapped_sums),
        "total_incoming": round(total_incoming, 2),
        "total_allocated": round(total_allocated, 2),
        "total_unmapped": round(total_unmapped, 2),
        "writeoff_deducted": round(total_writeoff_ded, 2),
        "elapsed": round(elapsed, 1),
        "month": date_mm_yyyy,
    }


async def _sync_one_category(
    cat_id: int,
    cat_name: str,
    iiko_total: float,
    date_mm_yyyy: str,
    *,
    direction_id: int | None = None,
) -> str:
    """
    Синхронизировать одну FT PnL-категорию (опционально — с direction).

    Логика:
      - Получить текущие PnL-записи для категории за месяц
      - Если direction_id задан — фильтровать по нему
      - Подсчитать сумму бот-записей (comment содержит BOT_COMMENT)
      - Рассчитать дельту и обновить

    Возвращает: "updated", "skipped" или текст описания.
    """
    # GET текущие записи за месяц
    existing_items = await fintablo_api.fetch_pnl_items(
        category_id=cat_id,
        date_mm_yyyy=date_mm_yyyy,
        direction_id=direction_id,
    )

    # Разделяем бот-записи и прочие
    bot_items = [
        it for it in existing_items if (it.get("comment") or "").startswith(BOT_COMMENT)
    ]
    other_items = [
        it
        for it in existing_items
        if not (it.get("comment") or "").startswith(BOT_COMMENT)
    ]

    ft_bot_total = sum(float(it.get("value", 0)) for it in bot_items)
    ft_other_total = sum(float(it.get("value", 0)) for it in other_items)

    # Целевая сумма от бота = iiko_total - прочие (ручные) записи
    desired_bot_value = round(iiko_total - ft_other_total, 2)

    if desired_bot_value <= 0 and not bot_items:
        logger.debug(
            "[pnl_sync] %s: FT other=%.2f >= iiko=%.2f, пропуск",
            cat_name,
            ft_other_total,
            iiko_total,
        )
        return "skipped"

    # Текущая бот-сумма совпадает?
    if abs(round(ft_bot_total, 2) - desired_bot_value) < 0.01:
        logger.debug("[pnl_sync] %s: delta=0, пропуск", cat_name)
        return "skipped"

    logger.info(
        "[pnl_sync] %s: iiko=%.2f, ft_other=%.2f, ft_bot=%.2f → desired_bot=%.2f",
        cat_name,
        iiko_total,
        ft_other_total,
        ft_bot_total,
        desired_bot_value,
    )

    # Удаляем старые бот-записи
    for item in bot_items:
        item_id = item.get("id")
        if item_id:
            await fintablo_api.delete_pnl_item(int(item_id))
            logger.debug("[pnl_sync] Удалена бот-запись id=%s", item_id)

    # Создаём новую бот-запись (если сумма > 0)
    if desired_bot_value > 0:
        await fintablo_api.create_pnl_item(
            category_id=cat_id,
            value=desired_bot_value,
            date_mm_yyyy=date_mm_yyyy,
            comment=BOT_COMMENT,
            direction_id=direction_id,
        )
        logger.info(
            "[pnl_sync] %s: создана запись %.2f за %s (dir=%s)",
            cat_name,
            desired_bot_value,
            date_mm_yyyy,
            direction_id,
        )

    return "updated"
