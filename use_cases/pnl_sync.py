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

from sqlalchemy import select, delete as sa_delete

from adapters import fintablo_api
from adapters import iiko_api
from db.engine import async_session
from db.ft_models import FTPnlCategory, PnlAccountMapping
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
    """Вернуть все активные маппинги из БД."""
    async with async_session() as session:
        stmt = (
            select(PnlAccountMapping)
            .where(PnlAccountMapping.is_active.is_(True))
            .order_by(
                PnlAccountMapping.ft_pnl_category_name,
                PnlAccountMapping.iiko_account_name,
            )
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "iiko_account_name": r.iiko_account_name,
                "ft_pnl_category_id": r.ft_pnl_category_id,
                "ft_pnl_category_name": r.ft_pnl_category_name,
            }
            for r in rows
        ]


async def save_mapping(iiko_account_name: str, ft_pnl_category_id: int) -> int:
    """Создать новый маппинг. Возвращает id записи."""
    # Получаем имя FT-категории для кэша
    async with async_session() as session:
        cat = await session.get(FTPnlCategory, ft_pnl_category_id)
        ft_name = cat.name if cat else f"ID:{ft_pnl_category_id}"

        obj = PnlAccountMapping(
            iiko_account_name=iiko_account_name,
            ft_pnl_category_id=ft_pnl_category_id,
            ft_pnl_category_name=ft_name,
        )
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj.id


async def delete_mapping(mapping_id: int) -> bool:
    """Удалить маппинг по id. Возвращает True если удалён."""
    async with async_session() as session:
        stmt = sa_delete(PnlAccountMapping).where(PnlAccountMapping.id == mapping_id)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0


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
    Загрузить OLAP-отчёт по пресету и вернуть список уникальных
    iiko Account.Name с суммами (TransactionType=INCOMING_SERVICE + INVOICE).

    date_from, date_to — формат YYYY-MM-DDTHH:MM:SS (ISO).
    Возвращает: [{"account_name": ..., "sum": ...}, ...]
    """
    rows = await iiko_api.fetch_olap_by_preset(OPIU_PRESET_ID, date_from, date_to)

    # Группируем по Account.Name, суммируем Sum.Incoming
    account_sums: dict[str, float] = defaultdict(float)
    for row in rows:
        account_name = row.get("Account.Name") or row.get("AccountName")
        if not account_name:
            continue
        try:
            value = float(row.get("Sum.Incoming", 0) or 0)
        except (ValueError, TypeError):
            value = 0.0
        if value > 0:
            account_sums[account_name] += value

    return [
        {"account_name": name, "sum": round(total, 2)}
        for name, total in sorted(account_sums.items())
    ]


async def get_distinct_iiko_accounts(
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[str]:
    """
    Получить уникальные имена iiko-счетов из OLAP-пресета за период.
    Если даты не указаны — берём текущий месяц.
    """
    if not date_from or not date_to:
        now = now_kgd()
        date_from = now.replace(day=1).strftime("%Y-%m-%dT00:00:00")
        # Конец месяца: начало следующего
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        date_to = next_month.strftime("%Y-%m-%dT00:00:00")

    data = await fetch_iiko_accounts_from_preset(date_from, date_to)
    return [d["account_name"] for d in data]


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

    # ── 2. Загрузить данные из iiko ──
    iiko_data = await fetch_iiko_accounts_from_preset(date_from, date_to)
    iiko_by_account = {d["account_name"]: d["sum"] for d in iiko_data}
    logger.info(
        "[pnl_sync] iiko: %d счетов с данными, total=%.2f",
        len(iiko_by_account),
        sum(iiko_by_account.values()),
    )

    # ── 3. Считать маппинг ──
    mappings = await get_all_mappings()
    if not mappings:
        logger.warning("[pnl_sync] Маппинг пуст — нечего обновлять")
        return {"updated": 0, "skipped": 0, "errors": 0, "details": ["Маппинг пуст"]}

    # ── 4. Агрегация: FT category_id → сумма iiko-счетов ──
    ft_totals: dict[int, float] = defaultdict(float)
    ft_names: dict[int, str] = {}
    unmapped_accounts: list[str] = []

    for m in mappings:
        cat_id = m["ft_pnl_category_id"]
        ft_names[cat_id] = m["ft_pnl_category_name"]
        iiko_sum = iiko_by_account.get(m["iiko_account_name"], 0.0)
        ft_totals[cat_id] += iiko_sum
        if m["iiko_account_name"] not in iiko_by_account:
            unmapped_accounts.append(m["iiko_account_name"])

    # Округлить
    for cat_id in ft_totals:
        ft_totals[cat_id] = round(ft_totals[cat_id], 2)

    logger.info(
        "[pnl_sync] Агрегация: %d FT-категорий, total=%.2f",
        len(ft_totals),
        sum(ft_totals.values()),
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
