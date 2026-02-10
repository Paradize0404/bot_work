"""
Use-case: создание и отправка акта списания (writeoff) в iiko.

Бизнес-логика:
  1. Получение складов по подразделению пользователя (бар / кухня)
  2. Получение счетов списания (Account)
  3. Поиск номенклатуры (GOODS / PREPARED)
  4. Получение единицы измерения по UUID
  5. Формирование и отправка документа через адаптер
"""

import asyncio
import logging
import time
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, func, or_

from db.engine import async_session_factory
from db.models import Store, Product, Entity
from adapters import iiko_api
from use_cases import writeoff_cache as _cache

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# Классификация должности → тип склада
# ─────────────────────────────────────────────────────

# Ключевые слова в названии должности, определяющие тип
_BAR_KEYWORDS = (
    "бармен", "старший бармен", "кассир", "кассир-бариста",
    "кассир-администратор", "ранер",
)
_KITCHEN_KEYWORDS = (
    "повар", "шеф-повар", "кондитер", "старший кондитер",
    "пекарь", "заготовщик", "посудомойка",
)


def classify_role(role_name: str | None) -> str:
    """
    Определить тип склада по названию должности.

    Возвращает:
      'bar'     — должность связана с баром (склад бара)
      'kitchen' — должность связана с кухней (склад кухни)
      'unknown' — не распознана (выбор склада вручную)

    Примечание: проверка на бот-админа (выбор склада вручную) делается
    отдельно в writeoff_handlers.py через admin_uc.is_admin().
    """
    if not role_name:
        return "unknown"

    low = role_name.strip().lower()

    for kw in _BAR_KEYWORDS:
        if kw in low:
            return "bar"

    for kw in _KITCHEN_KEYWORDS:
        if kw in low:
            return "kitchen"

    # Всё остальное (бухгалтер, собственник, техник, фриланс и т.д.)
    return "unknown"


def get_store_keyword_for_role(role_type: str) -> str | None:
    """
    Получить ключевое слово для фильтрации склада по типу роли.
    Возвращает 'бар', 'кухня' или None (= показать выбор).
    """
    if role_type == "bar":
        return "бар"
    if role_type == "kitchen":
        return "кухня"
    return None


# ─────────────────────────────────────────────────────
# Склады подразделения (бар / кухня)
# ─────────────────────────────────────────────────────

async def get_stores_for_department(department_id: str) -> list[dict]:
    """
    Получить склады, привязанные к подразделению пользователя,
    у которых в названии есть 'бар' или 'кухня'.
    Возвращает [{id, name}, ...].
    """
    # --- кеш ---
    cached = _cache.get_stores(department_id)
    if cached is not None:
        logger.debug("[writeoff] Склады из кеша (%d шт)", len(cached))
        return cached

    t0 = time.monotonic()
    logger.info("[writeoff] Загрузка складов для подразделения %s...", department_id)

    async with async_session_factory() as session:
        stmt = (
            select(Store)
            .where(Store.parent_id == UUID(department_id))
            .where(Store.deleted == False)
            .where(
                or_(
                    func.lower(Store.name).contains("бар"),
                    func.lower(Store.name).contains("кухня"),
                )
            )
            .order_by(Store.name)
        )
        result = await session.execute(stmt)
        stores = result.scalars().all()

    items = [{"id": str(s.id), "name": s.name} for s in stores]
    _cache.set_stores(department_id, items)
    logger.info(
        "[writeoff] Склады для %s: %d шт за %.2f сек",
        department_id, len(items), time.monotonic() - t0,
    )
    return items


# ─────────────────────────────────────────────────────
# Счета списания (Account)
# ─────────────────────────────────────────────────────

async def get_writeoff_accounts(store_name: str = "") -> list[dict]:
    """
    Получить счета списания, отфильтрованные по складу.

    Логика:
      1. root_type = 'Account', deleted = False
      2. name содержит 'списание'
      3. Если store_name содержит 'бар'  → name содержит 'бар'
         Если store_name содержит 'кухня' → name содержит 'кухня'
         Иначе → все счета со словом 'списание'
    Возвращает [{id, name}, ...].
    """
    # --- кеш ---
    cached = _cache.get_accounts(store_name)
    if cached is not None:
        logger.debug("[writeoff] Счета из кеша (%d шт, store=%s)", len(cached), store_name)
        return cached

    t0 = time.monotonic()

    # определяем сегмент склада
    sn = store_name.lower()
    if "бар" in sn:
        segment = "бар"
    elif "кухня" in sn or "кухн" in sn:
        segment = "кухня"
    else:
        segment = None

    logger.info(
        "[writeoff] Загрузка счетов списания (Account), store=%s segment=%s...",
        store_name, segment,
    )

    async with async_session_factory() as session:
        stmt = (
            select(Entity)
            .where(Entity.root_type == "Account")
            .where(Entity.deleted == False)
            .where(func.lower(Entity.name).contains("списание"))
        )
        if segment:
            stmt = stmt.where(func.lower(Entity.name).contains(segment))
        stmt = stmt.order_by(Entity.name)

        result = await session.execute(stmt)
        accounts = result.scalars().all()

    items = [{"id": str(a.id), "name": a.name} for a in accounts]
    _cache.set_accounts(store_name, items)
    logger.info(
        "[writeoff] Счетов: %d за %.2f сек",
        len(items), time.monotonic() - t0,
    )
    return items


# ─────────────────────────────────────────────────────
# Поиск номенклатуры
# ─────────────────────────────────────────────────────

async def search_products(query: str, limit: int = 15) -> list[dict]:
    """
    Поиск товаров по подстроке названия.
    Только GOODS и PREPARED, не удалённые.
    Возвращает [{id, name, main_unit, product_type, unit_name, unit_norm}, ...].
    Единицы измерения резолвятся в том же запросе (без доп. round-trip).
    """
    t0 = time.monotonic()
    pattern = query.strip().lower()
    if not pattern:
        return []

    logger.info("[writeoff] Поиск товаров по «%s»...", pattern)

    async with async_session_factory() as session:
        stmt = (
            select(Product)
            .where(func.lower(Product.name).contains(pattern))
            .where(Product.product_type.in_(["GOODS", "PREPARED"]))
            .where(Product.deleted == False)
            .order_by(Product.name)
            .limit(limit)
        )
        result = await session.execute(stmt)
        products = result.scalars().all()

        # Батч-резолв единиц измерения в той же сессии (один RT вместо N)
        unit_ids = {p.main_unit for p in products if p.main_unit}
        # Сначала смотрим кеш, потом догружаем оставшиеся из БД
        unit_map: dict[str, str] = {}  # uuid_str → unit_name
        missing_ids = []
        for uid in unit_ids:
            uid_str = str(uid)
            cached_name = _cache.get_unit(uid_str)
            if cached_name is not None:
                unit_map[uid_str] = cached_name
            else:
                missing_ids.append(uid)

        if missing_ids:
            unit_stmt = (
                select(Entity.id, Entity.name)
                .where(Entity.id.in_(missing_ids))
                .where(Entity.root_type == "MeasureUnit")
            )
            unit_result = await session.execute(unit_stmt)
            for row in unit_result.all():
                uid_str = str(row[0])
                uname = row[1] or "шт"
                unit_map[uid_str] = uname
                _cache.set_unit(uid_str, uname)

    items = []
    for p in products:
        uid_str = str(p.main_unit) if p.main_unit else None
        uname = unit_map.get(uid_str, "шт") if uid_str else "шт"
        unorm = normalize_unit(uname)
        items.append({
            "id": str(p.id),
            "name": p.name,
            "main_unit": uid_str,
            "product_type": p.product_type,
            "unit_name": uname,
            "unit_norm": unorm,
        })

    logger.info(
        "[writeoff] Поиск «%s» → %d результатов за %.2f сек",
        pattern, len(items), time.monotonic() - t0,
    )
    return items


# ─────────────────────────────────────────────────────
# Единица измерения по UUID
# ─────────────────────────────────────────────────────

async def get_unit_name(unit_id: str) -> str:
    """
    Получить название единицы измерения по UUID.
    Ищет в iiko_entity (root_type = 'MeasureUnit').
    Возвращает название или 'шт' по умолчанию.
    """
    if not unit_id:
        return "шт"

    # --- кеш ---
    cached = _cache.get_unit(unit_id)
    if cached is not None:
        return cached

    async with async_session_factory() as session:
        stmt = (
            select(Entity.name)
            .where(Entity.id == UUID(unit_id))
            .where(Entity.root_type == "MeasureUnit")
        )
        result = await session.execute(stmt)
        name = result.scalar_one_or_none()

    result_name = name or "шт"
    _cache.set_unit(unit_id, result_name)
    return result_name


def normalize_unit(unit_name: str) -> str:
    """
    Нормализовать название единицы для логики конвертации.
    Возвращает: 'kg', 'l', 'pcs' или оригинальное название.
    """
    low = (unit_name or "").strip().lower()
    if low in ("кг", "kg", "килограмм"):
        return "kg"
    if low in ("л", "l", "литр", "литры"):
        return "l"
    if low in ("шт", "шт.", "pcs", "порц", "порц."):
        return "pcs"
    return low


# ─────────────────────────────────────────────────────
# Формирование и отправка документа
# ─────────────────────────────────────────────────────

def build_writeoff_document(
    store_id: str,
    account_id: str,
    reason: str,
    items: list[dict],
    author_name: str = "",
) -> dict:
    """
    Построить JSON-документ для отправки в iiko API.
    items — [{id, quantity, main_unit}, ...]
    Отфильтровывает позиции с quantity == 0.
    comment = "причина (Автор: ФИО)" — для трекинга в iiko.
    """
    non_zero = [i for i in items if i.get("quantity", 0) > 0]
    comment_parts = [reason] if reason else []
    if author_name:
        comment_parts.append(f"(Автор: {author_name})")
    comment = " ".join(comment_parts)
    return {
        "dateIncoming": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "PROCESSED",
        "comment": comment,
        "storeId": store_id,
        "accountId": account_id,
        "items": [
            {
                "productId": item["id"],
                "amount": item["quantity"],
                "measureUnitId": item["main_unit"],
            }
            for item in non_zero
        ],
    }


async def send_writeoff_document(document: dict) -> str:
    """
    Отправить документ списания в iiko через adapter.
    Возвращает текстовый результат (ok / ошибка).
    """
    t0 = time.monotonic()
    n_items = len(document.get("items", []))

    if n_items == 0:
        return "⚠️ Документ пустой (все позиции с количеством 0)"

    logger.info(
        "[writeoff] Отправка документа: store=%s, account=%s, items=%d",
        document.get("storeId"), document.get("accountId"), n_items,
    )
    try:
        await iiko_api.send_writeoff(document)
        elapsed = time.monotonic() - t0
        logger.info("[writeoff] ✅ Документ отправлен за %.2f сек", elapsed)
        return "✅ Акт списания успешно отправлен!"
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception("[writeoff] ❌ Ошибка отправки за %.2f сек", elapsed)
        return f"❌ Ошибка отправки: {exc}"
    finally:
        # сбрасываем кеш складов/счетов после отправки
        _cache.invalidate()


async def preload_for_user(department_id: str) -> None:
    """
    Прогрев кеша при клике на "Документы".
    Запускается фоново — пользователь не ждёт.
    Загружает склады + счета + admin_ids.
    """
    t0 = time.monotonic()
    try:
        # Прогрев admin_ids параллельно со складами
        from use_cases import admin as _admin_uc
        stores, _ = await asyncio.gather(
            get_stores_for_department(department_id),
            _admin_uc.get_admin_ids(),
        )
        # параллельно грузим счета для всех складов
        await asyncio.gather(
            *(get_writeoff_accounts(s["name"]) for s in stores),
            return_exceptions=True,
        )
        logger.info(
            "[writeoff] Прогрев кеша: %d складов + счета + admin_ids за %.2f сек",
            len(stores), time.monotonic() - t0,
        )
    except Exception:
        logger.warning("[writeoff] Ошибка прогрева кеша", exc_info=True)
