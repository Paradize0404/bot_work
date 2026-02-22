"""
Use-case: заявки на товары (product requests).

Флоу:
  1. Создатель начинает заявку → вводит наименования
  2. Склад-источник берётся из прайс-листа (PriceProduct.store_id)
  3. Склад-получатель авто-определяется по типу склада + подразделению пользователя
  4. Контрагент авто-определяется из iiko_supplier по имени целевого склада
  5. Заявка сохраняется в БД (status=pending), уведомление → получателям
  6. Получатель видит заявку, нажимает «Отправить» →
     создаётся расходная накладная в iiko (через outgoing_invoice)

Получатели определяются через Google Таблицу (столбец «📬 Получатель»
на листе «Права доступа»).
"""

import logging
import re
import time
from use_cases._helpers import now_kgd
from uuid import UUID

from sqlalchemy import select, func

from db.engine import async_session_factory
from db.models import ProductRequest, Store, Supplier, ProductGroup

async def search_product_groups(query: str):
    async with async_session_factory() as session:
        stmt = (
            select(ProductGroup)
            .where(ProductGroup.deleted.is_(False))
            .where(func.lower(ProductGroup.name).contains(query))
            .limit(15)
        )
        return (await session.execute(stmt)).scalars().all()

async def get_product_group_by_id(group_id: str):
    async with async_session_factory() as session:
        stmt = select(ProductGroup).where(ProductGroup.id == UUID(group_id))
        return (await session.execute(stmt)).scalar_one_or_none()

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Заведения для заявок — настраиваемый список из GSheet
# ═══════════════════════════════════════════════════════


async def get_request_stores() -> list[dict[str, str]]:
    """
    Получить выбранное заведение для заявок из GSheet «Настройки».

    Возвращает [{id, name}] (0 или 1 элемент — одно выбранное заведение).
    """
    from adapters import google_sheets as gsheet

    stores = await gsheet.read_request_stores()
    logger.info("[request] Заведение для заявок из GSheet: %d шт", len(stores))
    return stores


async def sync_request_stores_sheet() -> int:
    """
    Синхронизировать подразделения (department_type=DEPARTMENT) из БД
    → GSheet «Настройки» → «## Заведение для заявок».

    Вызывается при синхронизации подразделений (sync_departments).
    Пользователь затем ставит ✅ напротив нужных заведений в таблице.

    Returns: количество заведений.
    """
    from adapters import google_sheets as gsheet
    from db.engine import async_session_factory
    from db.models import Department
    from sqlalchemy import select, func

    async with async_session_factory() as session:
        result = await session.execute(
            select(Department.id, Department.name)
            .where(Department.deleted.is_(False))
            .where(func.upper(Department.department_type) == "DEPARTMENT")
            .order_by(Department.name)
        )
        all_depts = [{"id": str(d.id), "name": d.name} for d in result.all()]

    count = await gsheet.sync_request_stores_to_sheet(all_depts)
    logger.info("[request] Синхронизировано %d заведений → GSheet", count)
    return count


# ═══════════════════════════════════════════════════════
# Авто-определение складов по типу + подразделению
# ═══════════════════════════════════════════════════════

# Алиасы для нормализации имён складов → канонические типы
# (должны совпадать с _STORE_TYPES в adapters/google_sheets.py)
_STORE_TYPE_ALIASES: dict[str, str] = {
    "хоз. товары": "хозы",
    "хоз товары": "хозы",
    "хоз.товары": "хозы",
    "хозтовары": "хозы",
    "хоз-товары": "хозы",
    "хозяйственные товары": "хозы",
}


def _normalize_store_type(raw: str) -> str:
    """
    Нормализовать «сырое» название типа склада к каноническому значению
    из списка ['бар', 'кухня', 'тмц', 'хозы'].

    Примеры:
        'хоз. товары'  → 'хозы'
        'тмц сельма'   → 'тмц'
        'бар'          → 'бар'
        'кухня'        → 'кухня'
    """
    raw = raw.strip().lower()
    if raw in _STORE_TYPE_ALIASES:
        return _STORE_TYPE_ALIASES[raw]
    # Prefix-match: «тмц сельма», «тмц (xxx)» → 'тмц'
    if raw.startswith("тмц"):
        return "тмц"
    # Prefix-match: «хоз. ...» / «хоз ...»
    if raw.startswith("хоз"):
        return "хозы"
    return raw


def extract_store_type(store_name: str) -> str:
    """
    Извлечь канонический тип склада из полного имени.

    Примеры:
        'PizzaYolo: Кухня (Московский)'  → 'кухня'
        'Кухня (Клиническая)'            → 'кухня'
        'Бар'                            → 'бар'
        'PizzaYolo: ТМЦ (Гайдара)'       → 'тмц'
        'Хоз. товары (Московский)'       → 'хозы'
        'ТМЦ Сельма'                     → 'тмц'
    """
    name = store_name.strip()
    # Убираем бренд-префикс до ':'
    if ":" in name:
        name = name.split(":", 1)[1].strip()
    # Убираем суффикс (подразделение) в скобках
    name = re.sub(r"\s*\([^)]+\)\s*$", "", name).strip()
    return _normalize_store_type(name)


async def get_all_stores_for_department(department_id: str) -> list[dict[str, str]]:
    """
    Все склады подразделения (parent_id = department_id, deleted=False).
    Возвращает [{id, name}, ...].
    """
    async with async_session_factory() as session:
        stmt = (
            select(Store.id, Store.name)
            .where(Store.deleted.is_(False))
            .where(Store.parent_id == UUID(department_id))
            .order_by(Store.name)
        )
        rows = (await session.execute(stmt)).all()
    return [{"id": str(r.id), "name": r.name} for r in rows]


async def build_store_type_map(department_id: str) -> dict[str, dict[str, str]]:
    """
    Построить маппинг {store_type_lower: {id, name}} для складов подразделения.

    Пример для подразделения «Московский»:
        {'кухня': {'id': '...', 'name': 'PizzaYolo: Кухня (Московский)'},
         'бар':   {'id': '...', 'name': 'PizzaYolo: Бар (Московский)'}}
    """
    stores = await get_all_stores_for_department(department_id)
    result: dict[str, dict[str, str]] = {}
    for s in stores:
        stype = extract_store_type(s["name"])
        if stype and stype not in result:  # первый совпавший
            result[stype] = {"id": s["id"], "name": s["name"]}
    logger.debug(
        "[request] store_type_map для dept=%s: %s",
        department_id,
        list(result.keys()),
    )
    return result


def resolve_target_store(
    source_store_name: str,
    user_store_type_map: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """
    По названию склада-источника найти целевой склад пользователя.

    source_store_name: 'Кухня (Клиническая)' → type 'кухня'
    user_store_type_map: {'кухня': {id, name}} (склады подразделения пользователя)
    Возвращает {id, name} или None.
    """
    stype = extract_store_type(source_store_name)
    if not stype:
        return None
    return user_store_type_map.get(stype)


async def find_counteragent_for_store(store_name: str) -> dict[str, str] | None:
    """
    Найти контрагента (iiko_supplier) по имени склада.

    В iiko склады/подразделения часто зарегистрированы как контрагенты
    для внутренних перемещений. Ищем по точному совпадению,
    потом по частичному (contains).

    Возвращает {id, name} или None.
    """
    name_lower = store_name.strip().lower()
    async with async_session_factory() as session:
        # 1) Точное совпадение
        stmt = (
            select(Supplier.id, Supplier.name)
            .where(Supplier.deleted.is_(False))
            .where(func.lower(Supplier.name) == name_lower)
        )
        row = (await session.execute(stmt)).first()
        if row:
            logger.debug(
                "[request] counteragent exact match: '%s' → '%s'", store_name, row.name
            )
            return {"id": str(row.id), "name": row.name}

        # 2) Частичное (contains)
        stmt = (
            select(Supplier.id, Supplier.name)
            .where(Supplier.deleted.is_(False))
            .where(func.lower(Supplier.name).contains(name_lower))
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        if row:
            logger.debug(
                "[request] counteragent partial match: '%s' → '%s'",
                store_name,
                row.name,
            )
            return {"id": str(row.id), "name": row.name}

        logger.warning("[request] counteragent not found for store '%s'", store_name)
        return None


# ═══════════════════════════════════════════════════════
# Получатели заявок — делегируем в permissions (GSheet)
# ═══════════════════════════════════════════════════════


async def get_receiver_ids(role_type: str = None) -> list[int]:
    """Список telegram_id получателей заявок (из GSheet кеша)."""
    from use_cases import permissions as perm_uc

    return await perm_uc.get_receiver_ids(role_type)


async def is_receiver(telegram_id: int) -> bool:
    """Проверить, является ли пользователь получателем заявок (из GSheet кеша)."""
    from use_cases import permissions as perm_uc

    return await perm_uc.is_receiver(telegram_id)


# ═══════════════════════════════════════════════════════
# Создание / получение заявок
# ═══════════════════════════════════════════════════════


async def create_request(
    *,
    requester_tg: int,
    requester_name: str,
    department_id: str,
    department_name: str,
    store_id: str,
    store_name: str,
    counteragent_id: str,
    counteragent_name: str,
    account_id: str,
    account_name: str,
    items: list[dict],
    total_sum: float,
    comment: str = "",
) -> int:
    """Создать заявку (status=pending). Возвращает pk."""
    t0 = time.monotonic()
    async with async_session_factory() as session:
        req = ProductRequest(
            status="pending",
            requester_tg=requester_tg,
            requester_name=requester_name,
            department_id=UUID(department_id),
            department_name=department_name,
            store_id=UUID(store_id),
            store_name=store_name,
            counteragent_id=UUID(counteragent_id),
            counteragent_name=counteragent_name,
            account_id=UUID(account_id),
            account_name=account_name,
            items=items,
            total_sum=total_sum,
            comment=comment,
        )
        session.add(req)
        await session.commit()
        pk = req.pk

    logger.info(
        "[request] ✅ Заявка pk=%d создана: tg:%d, dept=%s, store=%s, items=%d, sum=%.2f (%.2f сек)",
        pk,
        requester_tg,
        department_name,
        store_name,
        len(items),
        total_sum,
        time.monotonic() - t0,
    )
    return pk


async def get_request_by_pk(pk: int) -> dict | None:
    """Получить заявку по pk."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()

    if not r:
        return None

    return {
        "pk": r.pk,
        "status": r.status,
        "requester_tg": r.requester_tg,
        "requester_name": r.requester_name,
        "department_id": str(r.department_id),
        "department_name": r.department_name,
        "store_id": str(r.store_id),
        "store_name": r.store_name,
        "counteragent_id": str(r.counteragent_id),
        "counteragent_name": r.counteragent_name,
        "account_id": str(r.account_id),
        "account_name": r.account_name,
        "items": r.items or [],
        "total_sum": float(r.total_sum) if r.total_sum else 0.0,
        "comment": r.comment,
        "approved_by": r.approved_by,
        "created_at": r.created_at,
        "approved_at": r.approved_at,
    }


async def get_pending_requests() -> list[dict]:
    """Все заявки со статусом pending."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.status == "pending")
            .order_by(ProductRequest.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "requester_name": r.requester_name,
            "department_name": r.department_name,
            "store_name": r.store_name,
            "counteragent_name": r.counteragent_name,
            "items_count": len(r.items) if r.items else 0,
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def get_pending_requests_full() -> list[dict]:
    """Все заявки pending с полными данными (без N+1)."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.status == "pending")
            .order_by(ProductRequest.created_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "status": r.status,
            "requester_tg": r.requester_tg,
            "requester_name": r.requester_name,
            "department_id": str(r.department_id),
            "department_name": r.department_name,
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "counteragent_id": str(r.counteragent_id),
            "counteragent_name": r.counteragent_name,
            "account_id": str(r.account_id),
            "account_name": r.account_name,
            "items": r.items or [],
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "comment": r.comment,
            "approved_by": r.approved_by,
            "created_at": r.created_at,
            "approved_at": r.approved_at,
        }
        for r in rows
    ]


async def get_user_requests(telegram_id: int, limit: int = 10) -> list[dict]:
    """Последние заявки пользователя (approved/pending/cancelled), для истории."""
    async with async_session_factory() as session:
        stmt = (
            select(ProductRequest)
            .where(ProductRequest.requester_tg == telegram_id)
            .order_by(ProductRequest.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "pk": r.pk,
            "status": r.status,
            "requester_name": r.requester_name,
            "department_id": str(r.department_id),
            "department_name": r.department_name,
            "store_id": str(r.store_id),
            "store_name": r.store_name,
            "counteragent_id": str(r.counteragent_id),
            "counteragent_name": r.counteragent_name,
            "account_id": str(r.account_id),
            "account_name": r.account_name,
            "items": r.items or [],
            "total_sum": float(r.total_sum) if r.total_sum else 0.0,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def approve_request(pk: int, approved_by: int) -> bool:
    """Пометить заявку как approved."""
    now = now_kgd()
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.status = "approved"
        r.approved_by = approved_by
        r.approved_at = now
        await session.commit()

    logger.info("[request] ✅ Заявка pk=%d approved by tg:%d", pk, approved_by)
    return True


async def cancel_request(pk: int, cancelled_by: int) -> bool:
    """Отменить заявку."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.status = "cancelled"
        r.approved_by = cancelled_by
        await session.commit()

    logger.info("[request] ❌ Заявка pk=%d cancelled by tg:%d", pk, cancelled_by)
    return True


async def update_request_items(pk: int, items: list[dict], total_sum: float) -> bool:
    """Обновить позиции заявки (при редактировании получателем)."""
    async with async_session_factory() as session:
        stmt = select(ProductRequest).where(ProductRequest.pk == pk)
        result = await session.execute(stmt)
        r = result.scalar_one_or_none()
        if not r or r.status != "pending":
            return False
        r.items = items
        r.total_sum = total_sum
        await session.commit()

    logger.info(
        "[request] ✏️ Заявка pk=%d items обновлены (%d поз., sum=%.2f)",
        pk,
        len(items),
        total_sum,
    )
    return True


def format_request_text(
    req: dict, settings_dept_name: str = "", items_filter: list[dict] = None
) -> str:
    """HTML-текст заявки для отображения (плоский список, без деления по складам)."""
    items = items_filter if items_filter is not None else req.get("items", [])
    created = req.get("created_at")
    date_str = created.strftime("%d.%m.%Y %H:%M") if created else "?"

    dept_name = req.get("department_name", "?")
    header = f"📤 {dept_name}"
    if settings_dept_name:
        header += f" → 📥 {settings_dept_name}"

    text = (
        f"📝 <b>Заявка #{req['pk']}</b>\n"
        f"📅 {date_str}\n"
        f"👤 {req.get('requester_name', '?')}\n"
        f"{header}\n\n"
        f"<b>Позиции ({len(items)}):</b>\n"
    )

    total = 0.0
    for i, item in enumerate(items, 1):
        name = item.get("name", "?")
        amount = item.get("amount", 0)
        price = item.get("price", 0)
        unit = item.get("unit_name", "шт")
        line_sum = round(amount * price, 2)
        total += line_sum
        text += f"  {i}. {name} × {amount:.4g} {unit}"
        if price:
            text += f" × {price:.2f}₽ = {line_sum:.2f}₽"
        text += "\n"

    if items_filter is None:
        total = req.get("total_sum", 0)

    text += f"\n<b>Итого: {total:.2f}₽</b>"

    if req.get("comment"):
        text += f"\n💬 {req['comment']}"

    status_map = {
        "pending": "⏳ Ожидает",
        "approved": "✅ Отправлена",
        "cancelled": "❌ Отменена",
    }
    text += f"\n\n<b>Статус:</b> {status_map.get(req.get('status', ''), req.get('status', ''))}"
    return text


# ═══════════════════════════════════════════════════════
# Группы кондитеров (Pastry Groups)
# ═══════════════════════════════════════════════════════


async def get_pastry_groups() -> list[dict]:
    """Получить список номенклатурных групп кондитеров."""
    from db.models import PastryNomenclatureGroup

    async with async_session_factory() as session:
        stmt = select(PastryNomenclatureGroup).order_by(
            PastryNomenclatureGroup.group_name
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {"id": str(r.id), "group_id": str(r.group_id), "group_name": r.group_name}
        for r in rows
    ]


async def add_pastry_group(group_id: str, group_name: str) -> bool:
    """Добавить группу кондитеров."""
    from db.models import PastryNomenclatureGroup

    async with async_session_factory() as session:
        stmt = select(PastryNomenclatureGroup).where(
            PastryNomenclatureGroup.group_id == UUID(group_id)
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            return False
        new_group = PastryNomenclatureGroup(
            group_id=UUID(group_id), group_name=group_name
        )
        session.add(new_group)
        await session.commit()
    return True


async def remove_pastry_group(pk: str) -> bool:
    """Удалить группу кондитеров по ID записи."""
    from db.models import PastryNomenclatureGroup

    async with async_session_factory() as session:
        stmt = select(PastryNomenclatureGroup).where(
            PastryNomenclatureGroup.id == UUID(pk)
        )
        group = (await session.execute(stmt)).scalar_one_or_none()
        if not group:
            return False
        await session.delete(group)
        await session.commit()
    return True


async def is_pastry_product(product_id: str) -> bool:
    """Проверить, относится ли товар к кондитерской группе (включая подгруппы)."""
    from db.models import Product, ProductGroup, PastryNomenclatureGroup

    async with async_session_factory() as session:
        # Получаем parent_id товара
        stmt = select(Product.parent_id).where(Product.id == UUID(product_id))
        current_group_id = (await session.execute(stmt)).scalar_one_or_none()

        if not current_group_id:
            return False

        # Получаем все группы кондитеров
        stmt_pastry = select(PastryNomenclatureGroup.group_id)
        pastry_group_ids = set((await session.execute(stmt_pastry)).scalars().all())

        if not pastry_group_ids:
            return False

        # Поднимаемся по иерархии групп
        while current_group_id:
            if current_group_id in pastry_group_ids:
                return True

            stmt_parent = select(ProductGroup.parent_id).where(
                ProductGroup.id == current_group_id
            )
            current_group_id = (await session.execute(stmt_parent)).scalar_one_or_none()

        return False
