"""
Use-case: маппинг department_id (iiko Server) → cloud_org_id (iikoCloud).

Маппинг хранится в Google Таблице (лист «Настройки»), кешируется in-memory с TTL.
Позволяет каждому пользователю получать стоп-лист / остатки
для своего заведения (по department_id из авторизации).

Кеш:
  _cache: dict[str, str] — {department_uuid: cloud_org_uuid}
  TTL: 5 мин (аналогично permissions)
  Graceful degradation: если GSheet недоступен — используем предыдущий кеш
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

LABEL = "CloudOrgMap"

# ─────────────────────────────────────────────────────
# In-memory cache with TTL
# ─────────────────────────────────────────────────────

_CACHE_TTL: float = 5 * 60  # 5 минут

_cache: dict[str, str] = {}  # {department_uuid: cloud_org_uuid}
_cache_ts: float = 0.0


async def _ensure_cache() -> dict[str, str]:
    """Обновить кеш из GSheet если просрочен."""
    global _cache, _cache_ts

    if _cache and (time.monotonic() - _cache_ts) < _CACHE_TTL:
        return _cache

    try:
        from adapters.google_sheets import read_cloud_org_mapping
        fresh = await read_cloud_org_mapping()
        if fresh:
            _cache = fresh
            _cache_ts = time.monotonic()
            logger.info("[%s] Кеш обновлён: %d привязок", LABEL, len(_cache))
        elif not _cache:
            logger.warning("[%s] GSheet вернул пустой маппинг, кеш пуст", LABEL)
        else:
            logger.warning("[%s] GSheet вернул пустой маппинг, используем предыдущий кеш (%d)", LABEL, len(_cache))
    except Exception:
        if _cache:
            logger.warning("[%s] Ошибка чтения GSheet, используем предыдущий кеш (%d)", LABEL, len(_cache), exc_info=True)
        else:
            logger.exception("[%s] Ошибка чтения GSheet, кеш пуст", LABEL)

    return _cache


async def resolve_cloud_org_id(department_id: str | None) -> str | None:
    """
    Получить cloud_org_id для подразделения.

    Args:
        department_id: UUID подразделения из авторизации пользователя

    Returns:
        cloud_org_uuid или None (маппинг не настроен)
    """
    if not department_id:
        return None

    mapping = await _ensure_cache()
    org_id = mapping.get(department_id)

    if not org_id:
        logger.debug("[%s] Нет привязки для dept=%s", LABEL, department_id)
    return org_id


async def resolve_cloud_org_id_for_user(telegram_id: int) -> str | None:
    """
    Получить cloud_org_id для пользователя (по его department_id из авторизации).
    """
    from use_cases.user_context import get_user_context
    ctx = await get_user_context(telegram_id)
    if not ctx or not ctx.department_id:
        return None
    return await resolve_cloud_org_id(ctx.department_id)


async def get_all_cloud_org_ids() -> list[str]:
    """
    Получить все уникальные cloud_org_id из маппинга.
    Для вебхук-обработки: обновляем стоп-лист по ВСЕМ привязанным организациям.
    """
    mapping = await _ensure_cache()
    return list(set(mapping.values()))


def invalidate_cache() -> None:
    """Сбросить кеш (вызывается после обновления маппинга в GSheet)."""
    global _cache, _cache_ts
    _cache.clear()
    _cache_ts = 0.0
    logger.info("[%s] Кеш инвалидирован", LABEL)
