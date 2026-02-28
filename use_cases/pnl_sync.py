"""
Use-case: синхронизация ОПИУ (Отчёт о прибылях и убытках).

Логика:
  1. Загрузить отчёт по проводкам из iiko (OLAP TRANSACTIONS по пресету)
  2. Считать маппинг iiko Account.Name → FT PnL category из БД
  3. Агрегировать суммы (несколько iiko-счетов → одна FT-категория)
  4. Для каждой FT-категории:
     - GET текущие PnL-записи из FinTablo за месяц
     - Рассчитать дельту:
       * если FT сумма < загружаемой → добавить запись на разницу
       * если FT сумма > загружаемой → удалить все записи с меткой бота
         и добавить новую запись с нашей суммой
  5. Ежедневный запуск в 07:10 по расписанию (после основной синхронизации)

Маппинг хранится в таблице pnl_account_mapping (DB).
Бот-записи в FinTablo помечаются comment="iiko-bot-auto".
"""

import logging
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from adapters import fintablo_api
from adapters import iiko_api
from adapters.google_sheets import read_fintab_opiu_mapping
from db.engine import async_session_factory as async_session
from db.ft_models import FTPnlCategory
from db.models import Entity, ProductGroup
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

# Пресет OLAP-отчёта «Статьи и закуп по складам БОТ»
OPIU_PRESET_ID = "4120ac6e-b8b3-4e97-bd75-dda8c864b4c3"

# Метка для PnL-записей, созданных ботом (чтобы отличать от ручных)
BOT_COMMENT = "iiko-bot-auto"


# ═══════════════════════════════════════════════════════
# CRUD маппинга (DB)
# ═══════════════════════════════════════════════════════


async def get_all_mappings() -> list[dict]:
    """Вернуть все активные маппинги из листа «Маппинг FinTablo» (колонки F-G)."""
    return await read_fintab_opiu_mapping()


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


async def fetch_iiko_accounts_from_preset(
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Загрузить OLAP-отчёт по пресету и вернуть сырые строки.

    date_from, date_to — формат YYYY-MM-DDTHH:MM:SS (ISO).
    Возвращает: [{"account_name": str, "product_group": str|None, "sum": float}, ...]

    Каждая строка = одна комбинация счёт × товарная группа.
    В update_opiu применяется приоритетный маппинг:
      - сначала проверяется наличие маппинга для product_group,
      - при отсутствии — fallback на account_name.
    """
    rows = await iiko_api.fetch_olap_by_preset(OPIU_PRESET_ID, date_from, date_to)

    result: list[dict] = []
    for row in rows:
        account_name = row.get("Account.Name") or row.get("AccountName")
        if not account_name:
            continue
        try:
            value = float(row.get("Sum.Incoming", 0) or 0)
        except (ValueError, TypeError):
            value = 0.0
        if value <= 0:
            continue
        product_group = row.get("Product.SecondParent") or None
        result.append(
            {
                "account_name": account_name,
                "product_group": product_group,
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
      3. Считать маппинг из БД
      4. Агрегировать: для каждой FT PnL-категории просуммировать
         все привязанные iiko-счета
      5. Для каждой FT-категории:
         a. GET текущие PnL-записи за месяц
         b. Подсчитать сумму записей бота (comment = BOT_COMMENT)
         c. Рассчитать дельту и обновить:
            - дельта > 0 → добавить запись
            - дельта < 0 → удалить бот-записи, добавить новую
            - дельта == 0 → ничего не делать

    Возвращает: {"updated": N, "skipped": N, "errors": N, "details": [...]}
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

    # ── 3. Считать маппинг ──
    mappings = await get_all_mappings()
    if not mappings:
        logger.warning("[pnl_sync] Маппинг пуст — нечего обновлять")
        return {"updated": 0, "skipped": 0, "errors": 0, "details": ["Маппинг пуст"]}

    # Индекс маппинга: ключ iiko → (cat_id, cat_name)
    mapping_index: dict[str, tuple[int, str]] = {
        m["iiko_account_name"]: (m["ft_pnl_category_id"], m["ft_pnl_category_name"])
        for m in mappings
    }

    # ── 4. Агрегация с приоритетом группы над счётом ──
    # Для каждой сырой строки:
    #   1. Если Product.SecondParent есть и «[Группа] <group>» замаплен → эта FT-категория
    #   2. Иначе если Account.Name замаплен → эта FT-категория
    #   3. Иначе → пропускаем (немаппированная строка)
    ft_totals: dict[int, float] = defaultdict(float)
    ft_names: dict[int, str] = {}
    unmapped_rows: int = 0

    for row in iiko_rows:
        value = row["sum"]
        account_name = row["account_name"]
        product_group = row["product_group"]

        cat_id: int | None = None
        cat_name: str = ""

        # Приоритет 1: маппинг по группе товаров
        if product_group:
            group_key = f"[Группа] {product_group}"
            if group_key in mapping_index:
                cat_id, cat_name = mapping_index[group_key]

        # Приоритет 2: fallback на маппинг по счёту
        if cat_id is None and account_name in mapping_index:
            cat_id, cat_name = mapping_index[account_name]

        if cat_id is None:
            unmapped_rows += 1
            logger.debug(
                "[pnl_sync] Нет маппинга: account=%s group=%s sum=%.2f",
                account_name,
                product_group,
                value,
            )
            continue

        ft_totals[cat_id] += value
        ft_names[cat_id] = cat_name

    # Округлить
    for cat_id in ft_totals:
        ft_totals[cat_id] = round(ft_totals[cat_id], 2)

    logger.info(
        "[pnl_sync] Агрегация: %d FT-категорий, total=%.2f, unmapped_rows=%d",
        len(ft_totals),
        sum(ft_totals.values()),
        unmapped_rows,
    )

    # ── 5. Обновить каждую FT-категорию ──
    updated = 0
    skipped = 0
    errors = 0
    details: list[str] = []

    for cat_id, iiko_total in ft_totals.items():
        cat_name = ft_names.get(cat_id, f"ID:{cat_id}")
        try:
            result = await _sync_one_category(
                cat_id, cat_name, iiko_total, date_mm_yyyy
            )
            if result == "updated":
                updated += 1
                details.append(f"✅ {cat_name}: {iiko_total:.2f}")
            elif result == "skipped":
                skipped += 1
            else:
                details.append(f"ℹ️ {cat_name}: {result}")
        except Exception:
            errors += 1
            logger.exception("[pnl_sync] Ошибка для %s (id=%d)", cat_name, cat_id)
            details.append(f"❌ {cat_name}: ошибка")

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
        "elapsed": round(elapsed, 1),
    }


async def _sync_one_category(
    cat_id: int,
    cat_name: str,
    iiko_total: float,
    date_mm_yyyy: str,
) -> str:
    """
    Синхронизировать одну FT PnL-категорию.

    Логика:
      - Получить текущие PnL-записи для категории за месяц
      - Подсчитать сумму бот-записей (comment содержит BOT_COMMENT)
      - Подсчитать сумму всех записей
      - Если FT total < iiko_total → добавить запись на разницу
      - Если FT total > iiko_total → удалить бот-записи, добавить нашу
      - Если FT total == iiko_total → пропустить

    Возвращает: "updated", "skipped" или текст описания.
    """
    # GET текущие записи за месяц
    existing_items = await fintablo_api.fetch_pnl_items(
        category_id=cat_id, date_mm_yyyy=date_mm_yyyy
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
        # Ручные записи уже покрывают / превышают → пропускаем
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

    # Нужно обновить
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
        )
        logger.info(
            "[pnl_sync] %s: создана запись %.2f за %s",
            cat_name,
            desired_bot_value,
            date_mm_yyyy,
        )

    return "updated"
