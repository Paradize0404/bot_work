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
import json
from typing import Any

from use_cases.redis_cache import get_cached_or_fetch, invalidate_key

logger = logging.getLogger(__name__)

LABEL = "CloudOrgMap"

# ─────────────────────────────────────────────────────
# Redis cache with TTL
# ─────────────────────────────────────────────────────

_CACHE_TTL: int = 5 * 60  # 5 минут
_CACHE_KEY = "cloud_org_mapping"


async def _ensure_cache() -> dict[str, str]:
    """Обновить кеш из GSheet если просрочен."""

    async def _fetch() -> dict[str, str] | None:
        try:
            from adapters.google_sheets import read_cloud_org_mapping

            fresh = await read_cloud_org_mapping()
            if fresh:
                logger.info("[%s] Кеш обновлён: %d привязок", LABEL, len(fresh))
                return fresh
            else:
                logger.warning("[%s] GSheet вернул пустой маппинг", LABEL)
                return None
        except Exception:
            logger.exception("[%s] Ошибка чтения GSheet", LABEL)
            return None

    data = await get_cached_or_fetch(
        _CACHE_KEY,
        _fetch,
        ttl_seconds=_CACHE_TTL,
        serializer=json.dumps,
        deserializer=json.loads,
    )
    return data or {}


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


async def invalidate_cache() -> None:
    """Сбросить кеш (вызывается после обновления маппинга в GSheet)."""
    await invalidate_key(_CACHE_KEY)
    logger.info("[%s] Кеш инвалидирован", LABEL)
