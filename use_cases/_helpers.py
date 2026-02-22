"""
Shared helpers for use_cases — DRY вместо дублей в каждом sync-модуле.

Конвертеры: safe_uuid, safe_bool, safe_decimal, safe_int, safe_float.
Время: now_kgd() — текущее время по Калининграду (Europe/Kaliningrad, UTC+2).
Хеш: compute_hash() — SHA-256 для дедупликации.
Дерево: bfs_allowed_groups() — BFS по iiko_product_group.
Склады: load_stores_for_department() — склады подразделения (бар/кухня).

Используются в sync.py, sync_fintablo.py, sync_stock_balances.py и т.д.
"""

import hashlib
import re
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

# Калининградское время (UTC+2) — используется ВЕЗДЕ в проекте
KGD_TZ = ZoneInfo("Europe/Kaliningrad")


def now_kgd() -> datetime:
    """
    Текущее время по Калининграду (naive, без tzinfo).
    Совместимо с TIMESTAMP WITHOUT TIME ZONE в asyncpg.
    Используется ВЕЗДЕ вместо utcnow().
    """
    return datetime.now(KGD_TZ).replace(tzinfo=None)


# ─────────────────────────────────────────────────────
# Безопасные конвертеры
# ─────────────────────────────────────────────────────


def safe_uuid(v: Any) -> uuid.UUID | None:
    """Безопасное преобразование в UUID (None если невалидно)."""
    if v is None:
        return None
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError):
        return None


def safe_bool(v: Any) -> bool:
    """Безопасное преобразование в bool."""
    if isinstance(v, bool):
        return v
    return str(v).lower() == "true" if isinstance(v, str) else False


def safe_decimal(v: Any) -> float | None:
    """Безопасное преобразование в float (для Numeric-полей)."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def safe_int(v: Any) -> int | None:
    """Безопасное преобразование в int."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def safe_float(v: Any) -> float | None:
    """Безопасное преобразование в float (алиас safe_decimal)."""
    return safe_decimal(v)


# ─────────────────────────────────────────────────────
# Секреты и хеши
# ─────────────────────────────────────────────────────

_SECRET_RE = re.compile(
    r'(key|token|password|secret|bearer)=([^\s&"\']+)', re.IGNORECASE
)


def mask_secrets(text: str) -> str:
    """Маскирует секреты в строке для безопасного логирования."""
    if not text:
        return text
    return _SECRET_RE.sub(r"\1=***", str(text))


def compute_hash(text: str) -> str:
    """SHA-256 хеш текста. Используется для дедупликации pinned-сообщений."""
    return hashlib.sha256(text.encode()).hexdigest()


# ─────────────────────────────────────────────────────
# BFS по дереву номенклатурных групп (общий для writeoff, invoice, sync_min_stock)
# ─────────────────────────────────────────────────────


async def bfs_allowed_groups(session, group_model_class) -> set[str]:
    """
    BFS по iiko_product_group: вернуть множество UUID всех групп-потомков
    корневых записей из таблицы group_model_class (GSheetExportGroup
    или WriteoffRequestStoreGroup).

    Args:
        session: AsyncSession (SQLAlchemy)
        group_model_class: ORM-модель с полем group_id (e.g. GSheetExportGroup)

    Returns:
        set[str] — UUID всех разрешённых групп (корневые + потомки).
        Пустое множество если таблица пуста.
    """
    from sqlalchemy import select
    from db.models import ProductGroup

    root_rows = (await session.execute(select(group_model_class.group_id))).all()
    root_ids = [str(r.group_id) for r in root_rows]

    if not root_ids:
        return set()

    group_rows = (
        await session.execute(
            select(ProductGroup.id, ProductGroup.parent_id).where(
                ProductGroup.deleted.is_(False)
            )
        )
    ).all()

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
# Склады подразделения (бар / кухня) — общий для writeoff и outgoing_invoice
# ─────────────────────────────────────────────────────


async def load_stores_for_department(department_id: str) -> list[dict]:
    """
    Получить склады, привязанные к подразделению пользователя,
    у которых в названии есть 'бар' или 'кухня'.
    Возвращает [{id, name}, ...].

    Без кеширования — вызывающий код должен кешировать самостоятельно
    через writeoff_cache / invoice_cache.
    """
    import logging
    import time

    from sqlalchemy import select, func, or_
    from db.engine import async_session_factory
    from db.models import Store

    logger = logging.getLogger(__name__)
    t0 = time.monotonic()

    async with async_session_factory() as session:
        stmt = (
            select(Store)
            .where(Store.parent_id == UUID(department_id))
            .where(Store.deleted.is_(False))
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
    logger.info(
        "[helpers] Склады для %s: %d шт за %.2f сек",
        department_id,
        len(items),
        time.monotonic() - t0,
    )
    return items
