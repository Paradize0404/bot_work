"""
Use-case: редактирование минимальных остатков через Telegram-бот.

Флоу:
  1. Пользователь ищет товар по подстроке названия.
  2. Выбирает товар из inline-кнопок.
  3. Вводит новый минимальный остаток.
  4. Бот записывает в Google Таблицу + min_stock_level (БД).

Зависимости:
  - iiko_product           — поиск товаров
  - adapters/google_sheets — запись в Google Таблицу
  - min_stock_level        — кэш в PostgreSQL
"""

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import async_session_factory
from db.models import Product, Department, MinStockLevel

from adapters import google_sheets as gsheet
from bot._utils import escape_md as _escape_md

logger = logging.getLogger(__name__)

LABEL = "EditMinStock"


# ═══════════════════════════════════════════════════════
# Dataclasses для результатов
# ═══════════════════════════════════════════════════════


@dataclass(slots=True)
class EditMinResult:
    """Результат apply_min_level."""

    success: bool
    text: str  # Markdown-форматированный текст результата


# ═══════════════════════════════════════════════════════
# 1. Поиск товаров для редактирования
# ═══════════════════════════════════════════════════════


async def search_products_for_edit(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск товаров по подстроке названия.
    Только GOODS, не удалённые.
    Возвращает [{id, name, product_type}, ...].
    """
    pattern = query.strip().lower()
    if not pattern:
        return []

    t0 = time.monotonic()
    logger.info("[%s] Поиск товаров по «%s»...", LABEL, pattern)

    async with async_session_factory() as session:
        stmt = (
            select(Product.id, Product.name, Product.product_type)
            .where(func.lower(Product.name).contains(pattern))
            .where(Product.product_type == "GOODS")
            .where(Product.deleted.is_(False))
            .order_by(Product.name)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()

    items = [
        {"id": str(r.id), "name": r.name, "product_type": r.product_type} for r in rows
    ]
    logger.info(
        "[%s] Поиск «%s» → %d результатов за %.2f сек",
        LABEL,
        pattern,
        len(items),
        time.monotonic() - t0,
    )
    return items


# ═══════════════════════════════════════════════════════
# 2. Обновление мин. остатка → Google Таблица + БД
# ═══════════════════════════════════════════════════════


async def update_min_level(
    product_id: str,
    department_id: str,
    new_min: float,
    new_max: float = 0.0,
) -> str:
    """
    Записать новый minLevel в Google Таблицу и min_stock_level (БД).

    Шаги:
      1. Получить имя товара и ресторана из БД.
      2. Получить текущий min из min_stock_level (для отображения «было»).
      3. Обновить ячейку в Google Таблице.
      4. Upsert в min_stock_level (БД).

    Возвращает текстовый статус ("✅ ..." или "❌ ...").
    """
    t0 = time.monotonic()
    logger.info(
        "[%s] Обновляю min=%s, max=%s для product=%s, dept=%s",
        LABEL,
        new_min,
        new_max,
        product_id,
        department_id,
    )

    # 1. Получить имена
    async with async_session_factory() as session:
        prod = (
            await session.execute(
                select(Product.name).where(Product.id == UUID(product_id))
            )
        ).scalar_one_or_none()

        dept = (
            await session.execute(
                select(Department.name).where(Department.id == UUID(department_id))
            )
        ).scalar_one_or_none()

        if not prod:
            return "❌ Товар не найден в БД"
        if not dept:
            return "❌ Ресторан не найден в БД"

        product_name = prod
        department_name = dept

        # 2. Текущий min из БД
        old_row = (
            await session.execute(
                select(MinStockLevel.min_level)
                .where(MinStockLevel.product_id == UUID(product_id))
                .where(MinStockLevel.department_id == UUID(department_id))
            )
        ).scalar_one_or_none()
        old_min = float(old_row) if old_row is not None else None

    # 3. Записать в Google Таблицу
    try:
        ok = await gsheet.update_min_max(
            product_id=product_id,
            department_id=department_id,
            min_level=new_min,
            max_level=new_max,
        )
        if not ok:
            return (
                "❌ Товар или ресторан не найден в Google Таблице.\n"
                "Сначала нажмите «📤 Номенклатура → GSheet»."
            )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception(
            "[%s] ❌ Ошибка Google Sheets за %.2f сек: %s",
            LABEL,
            elapsed,
            exc,
        )
        return f"❌ Ошибка записи в Google Таблицу: {exc}"

    # 4. Upsert в min_stock_level (БД) — мгновенный кэш
    try:
        async with async_session_factory() as session:
            stmt = pg_insert(MinStockLevel).values(
                product_id=UUID(product_id),
                product_name=product_name,
                department_id=UUID(department_id),
                department_name=department_name,
                min_level=new_min,
                max_level=new_max,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_min_stock_product_dept",
                set_={
                    "product_name": stmt.excluded.product_name,
                    "department_name": stmt.excluded.department_name,
                    "min_level": stmt.excluded.min_level,
                    "max_level": stmt.excluded.max_level,
                },
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.warning("[%s] Upsert в БД не удался (GSheet OK)", LABEL, exc_info=True)

    elapsed = time.monotonic() - t0
    old_str = f"{old_min:.4g}" if old_min is not None else "—"

    logger.info(
        "[%s] ✅ %s (%s): min %s → %s за %.2f сек",
        LABEL,
        product_name,
        department_name,
        old_str,
        new_min,
        elapsed,
    )
    return (
        f"✅ *Минимальный остаток обновлён!*\n\n"
        f"📦 {_escape_md(product_name)}\n"
        f"🏪 {_escape_md(department_name)}\n"
        f"Было: {old_str}\n"
        f"Стало: *{new_min:.4g}*"
    )


# ═══════════════════════════════════════════════════════
# 3. Высокоуровневый use-case: валидация + обновление
# ═══════════════════════════════════════════════════════


def apply_min_level(value_str: str) -> EditMinResult | float:
    """
    Валидация введённого значения мин. остатка.
    Возвращает EditMinResult(error) или float(validated_value).
    """
    text = value_str.strip().replace(",", ".")
    try:
        new_min = float(text)
    except ValueError:
        return EditMinResult(
            success=False,
            text="⚠️ Введите число (например: 5, 10.5, 0).\nПопробуйте ещё раз:",
        )

    if new_min < 0:
        return EditMinResult(
            success=False,
            text="⚠️ Значение не может быть отрицательным. Попробуйте ещё раз:",
        )

    if new_min > 999999:
        return EditMinResult(
            success=False,
            text="⚠️ Слишком большое значение. Максимум 999 999. Попробуйте ещё раз:",
        )

    return new_min
