"""
Use-case: создание и отправка акта списания (writeoff) в iiko.

Бизнес-логика:
  1. Получение складов по подразделению пользователя (бар / кухня)
  2. Получение счетов списания (Account)
  3. Поиск номенклатуры (GOODS / DISH / PREPARED)
  4. Получение единицы измерения по UUID
  5. Формирование и отправка документа через адаптер
  6. Подготовка writeoff, одобрение/отклонение (use-case для admin)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from use_cases._helpers import now_kgd
from uuid import UUID

from sqlalchemy import select, func, or_

from db.engine import async_session_factory
from db.models import (
    Store, Product, Entity,
    GSheetExportGroup, WriteoffRequestStoreGroup, ProductGroup,
)
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
# Вспомогательные функции: фильтрация по папкам
# ─────────────────────────────────────────────────────

async def _is_request_department(department_id: str) -> bool:
    """Проверить, является ли подразделение точкой-получателем заявок."""
    from use_cases.product_request import get_request_stores
    stores = await get_request_stores()
    return any(s["id"] == department_id for s in stores)


async def _bfs_allowed_groups(session, group_model_class) -> set[str]:
    """
    BFS по iiko_product_group: вернуть множество UUID всех групп-потомков
    корневых записей из таблицы group_model_class (gsheet_export_group
    или writeoff_request_store_group).

    Возвращает пустое множество, если таблица пуста.
    """
    root_rows = (await session.execute(
        select(group_model_class.group_id)
    )).all()
    root_ids = [str(r.group_id) for r in root_rows]

    if not root_ids:
        return set()

    group_rows = (await session.execute(
        select(ProductGroup.id, ProductGroup.parent_id)
        .where(ProductGroup.deleted.is_(False))
    )).all()

    children_map: dict[str, list[str]] = {}
    for g in group_rows:
        pid = str(g.parent_id) if g.parent_id else None
        if pid:
            children_map.setdefault(pid, []).append(str(g.id))

    allowed: set[str] = set()
    queue = list(root_ids)
    while queue:
        gid = queue.pop()
        if gid in allowed:
            continue
        allowed.add(gid)
        queue.extend(children_map.get(gid, []))

    return allowed


# ─────────────────────────────────────────────────────
# Кеш и поиск номенклатуры
# ─────────────────────────────────────────────────────

async def preload_products(department_id: str | None = None) -> None:
    """
    Загрузить товары для списания в in-memory кеш.

    Логика фильтрации (если department_id задан):
      - Точка-получатель заявок → GOODS+DISH из writeoff_request_store_group (BFS) + все PREPARED
      - Все остальные точки     → GOODS+DISH из gsheet_export_group (BFS) + все PREPARED
    Если таблица папок пуста → показываем только PREPARED.

    Если department_id не задан → загружает все GOODS+DISH+PREPARED (без фильтрации, fallback).
    ~1942 товара × ~200 байт ≈ 400 КБ RAM. TTL = 10 мин.
    После загрузки search_products ищет за 0 мс вместо ~2 сек (DB round-trip).
    """
    cache_key = department_id or "all"
    if _cache.get_products(cache_key) is not None:
        return  # уже прогрет

    t0 = time.monotonic()

    async with async_session_factory() as session:
        # Определяем набор разрешённых групп (если dept задан)
        allowed_groups: set[str] | None = None
        if department_id is not None:
            is_req = await _is_request_department(department_id)
            group_model = WriteoffRequestStoreGroup if is_req else GSheetExportGroup
            allowed_groups = await _bfs_allowed_groups(session, group_model)
            logger.info(
                "[writeoff] Прогрев кеша для dept=%s (%s): %d разрешённых групп",
                department_id, group_model.__tablename__, len(allowed_groups),
            )

        stmt = (
            select(Product.id, Product.name, Product.parent_id,
                   Product.main_unit, Product.product_type)
            .where(Product.product_type.in_(["GOODS", "DISH", "PREPARED"]))
            .where(Product.deleted.is_(False))
            .order_by(Product.name)
        )
        result = await session.execute(stmt)
        rows = result.all()

        # Фильтр папок: GOODS+DISH — только из разрешённых групп, PREPARED — всегда
        if allowed_groups is not None:
            rows = [
                r for r in rows
                if r.product_type == "PREPARED"
                or (r.product_type in ("GOODS", "DISH") and r.parent_id
                    and str(r.parent_id) in allowed_groups)
            ]

        # Батч-резолв единиц: собираем уникальные UUID → один запрос
        unit_ids = {r.main_unit for r in rows if r.main_unit}
        unit_map: dict[str, str] = {}
        missing_ids = []
        for uid in unit_ids:
            uid_str = str(uid)
            cached = _cache.get_unit(uid_str)
            if cached is not None:
                unit_map[uid_str] = cached
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

    # Формируем список
    items: list[dict] = []
    for r in rows:
        uid_str = str(r.main_unit) if r.main_unit else None
        uname = unit_map.get(uid_str, "шт") if uid_str else "шт"
        unorm = normalize_unit(uname)
        items.append({
            "id": str(r.id),
            "name": r.name,
            "name_lower": r.name.lower() if r.name else "",
            "main_unit": uid_str,
            "product_type": r.product_type,
            "unit_name": uname,
            "unit_norm": unorm,
        })

    _cache.set_products(items, cache_key)
    logger.info(
        "[writeoff] Прогрев кеша номенклатуры (dept=%s): %d товаров за %.2f сек",
        cache_key, len(items), time.monotonic() - t0,
    )


async def search_products(
    query: str,
    limit: int = 15,
    department_id: str | None = None,
) -> list[dict]:
    """
    Поиск товаров по подстроке названия.

    Фильтрация зависит от подразделения (department_id):
      - Точка-получатель заявок → GOODS+DISH из writeoff_request_store_group + все PREPARED
      - Все остальные точки     → GOODS+DISH из gsheet_export_group + все PREPARED
      - department_id=None      → все GOODS+DISH+PREPARED (без фильтрации, fallback)

    Стратегия поиска:
      1. Если в кеше есть products для данного dept → поиск in-memory (0 мс)
      2. Иначе → прогреть кеш + повторить
      3. Если кеш пуст (таблицы групп пусты) → fallback-запрос в БД
    """
    t0 = time.monotonic()
    pattern = query.strip().lower()
    if not pattern:
        return []

    cache_key = department_id or "all"
    logger.info("[writeoff] Поиск товаров по «%s» (dept=%s)...", pattern, cache_key)

    # --- Попытка поиска в кеше ---
    cached_products = _cache.get_products(cache_key)
    if cached_products is not None:
        results = [
            {k: v for k, v in p.items() if k != "name_lower"}
            for p in cached_products
            if pattern in p["name_lower"]
        ][:limit]
        logger.info(
            "[writeoff] Поиск «%s» → %d результатов за %.4f сек (кеш, dept=%s)",
            pattern, len(results), time.monotonic() - t0, cache_key,
        )
        return results

    # --- Прогреваем кеш и повторяем ---
    logger.debug("[writeoff] Кеш пуст (dept=%s), прогреваю...", cache_key)
    await preload_products(department_id)
    cached_products = _cache.get_products(cache_key)
    if cached_products is not None:
        results = [
            {k: v for k, v in p.items() if k != "name_lower"}
            for p in cached_products
            if pattern in p["name_lower"]
        ][:limit]
        logger.info(
            "[writeoff] Поиск «%s» → %d результатов за %.4f сек (после прогрева, dept=%s)",
            pattern, len(results), time.monotonic() - t0, cache_key,
        )
        return results

    # --- Fallback: запрос в БД без фильтрации по папкам ---
    logger.debug("[writeoff] Fallback: ищу в БД без фильтра папок...")

    async with async_session_factory() as session:
        stmt = (
            select(Product)
            .where(func.lower(Product.name).contains(pattern))
            .where(Product.product_type.in_(["GOODS", "DISH", "PREPARED"]))
            .where(Product.deleted.is_(False))
            .order_by(Product.name)
            .limit(limit)
        )
        result = await session.execute(stmt)
        products = result.scalars().all()

        # Батч-резолв единиц измерения в той же сессии (один RT вместо N)
        unit_ids = {p.main_unit for p in products if p.main_unit}
        unit_map: dict[str, str] = {}
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
        "[writeoff] Поиск «%s» → %d результатов за %.2f сек (БД fallback)",
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
        "dateIncoming": now_kgd().strftime("%Y-%m-%dT%H:%M:%S"),
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
        # Прогрев admin_ids + складов + номенклатуры параллельно
        from use_cases import admin as _admin_uc
        stores, _, _ = await asyncio.gather(
            get_stores_for_department(department_id),
            _admin_uc.get_admin_ids(),
            preload_products(department_id),
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


# ═══════════════════════════════════════════════════════
# Высокоуровневые use-case функции
# ═══════════════════════════════════════════════════════

@dataclass(slots=True)
class WriteoffStart:
    """Результат prepare_writeoff."""
    stores: list[dict]
    is_admin: bool
    role_type: str               # 'bar', 'kitchen', 'admin', 'unknown'
    auto_store: dict | None      # авто-выбранный склад или None
    accounts: list[dict] | None  # если авто-склад — счета для него


@dataclass(slots=True)
class ApprovalResult:
    """Результат approve_writeoff."""
    success: bool
    result_text: str


async def prepare_writeoff(
    department_id: str,
    role_name: str | None,
    is_bot_admin: bool,
) -> WriteoffStart:
    """
    Подготовка данных для создания акта списания.
    Вся бизнес-логика авто-выбора склада по роли — здесь.
    """
    stores = await get_stores_for_department(department_id)

    if is_bot_admin:
        role_type = "admin"
        store_keyword = None
    else:
        role_type = classify_role(role_name)
        store_keyword = get_store_keyword_for_role(role_type)

    auto_store = None
    accounts = None

    if store_keyword and stores:
        matched = [s for s in stores if store_keyword in s["name"].lower()]
        if matched:
            auto_store = matched[0]
            accounts = await get_writeoff_accounts(auto_store["name"])
            if not accounts:
                accounts = None

    return WriteoffStart(
        stores=stores,
        is_admin=is_bot_admin,
        role_type=role_type,
        auto_store=auto_store,
        accounts=accounts,
    )


async def approve_writeoff(doc) -> ApprovalResult:
    """
    Одобрить документ: build → send в iiko.
    doc — PendingWriteoff из pending_writeoffs.
    """
    document = build_writeoff_document(
        store_id=doc.store_id,
        account_id=doc.account_id,
        reason=doc.reason,
        items=doc.items,
        author_name=doc.author_name,
    )
    result_text = await send_writeoff_document(document)
    success = result_text.startswith("✅")
    return ApprovalResult(success=success, result_text=result_text)


async def finalize_without_admins(
    store_id: str,
    account_id: str,
    reason: str,
    items: list[dict],
    author_name: str,
) -> str:
    """
    Отправить акт напрямую (fallback если нет админов).
    Возвращает текст результата.
    """
    document = build_writeoff_document(
        store_id=store_id,
        account_id=account_id,
        reason=reason,
        items=items,
        author_name=author_name,
    )
    return await send_writeoff_document(document)
