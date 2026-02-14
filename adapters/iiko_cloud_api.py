"""
Адаптер iikoCloud API (https://api-ru.iiko.services).

Отдельный от iiko_api.py (on-premise REST API).
Используется для:
  - Получения организаций (organization_id)
  - Регистрации/настройки вебхуков
  - Верификации входящих вебхуков

Авторизация:
  Токен берётся из таблицы iiko_access_tokens (внешний cron обновляет каждые 5 мин).
  НЕ создаём/обновляем токен сами — «работает, не трогай».
"""

import logging
import time
from typing import Any

import httpx
from sqlalchemy import text

from config import IIKO_CLOUD_BASE_URL, IIKO_CLOUD_WEBHOOK_SECRET
from db.engine import async_session_factory

logger = logging.getLogger(__name__)

LABEL = "iikoCloud"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# Persistent HTTP client
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Lazy-init persistent httpx client для iikoCloud."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            http2=False,
        )
    return _client


async def close_client() -> None:
    """Закрыть HTTP-клиент при остановке."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
        logger.info("[%s] httpx client closed", LABEL)


# ═══════════════════════════════════════════════════════
# Токен из БД (read-only, внешний cron обновляет)
# ═══════════════════════════════════════════════════════

async def get_cloud_token() -> str:
    """
    Получить последний iikoCloud access token из таблицы iiko_access_tokens.
    Поднимается RuntimeError если токен отсутствует.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            text("SELECT token FROM iiko_access_tokens ORDER BY created_at DESC LIMIT 1")
        )
        token = result.scalar_one_or_none()
    if not token:
        raise RuntimeError("No iikoCloud token in iiko_access_tokens — проверь внешний cron")
    return token


async def _headers() -> dict[str, str]:
    """Authorization header для запросов к iikoCloud."""
    token = await get_cloud_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════
# Организации
# ═══════════════════════════════════════════════════════

async def get_organizations() -> list[dict[str, Any]]:
    """
    POST /api/1/organizations — получить список организаций.
    Нужно для определения Organization ID (один раз).
    """
    t0 = time.monotonic()
    client = await _get_client()
    headers = await _headers()

    resp = await client.post(
        f"{IIKO_CLOUD_BASE_URL}/api/1/organizations",
        headers=headers,
        json={},
    )
    resp.raise_for_status()
    data = resp.json()

    orgs = data.get("organizations", [])
    logger.info(
        "[%s] Получено %d организаций за %.1f сек",
        LABEL, len(orgs), time.monotonic() - t0,
    )
    return orgs


# ═══════════════════════════════════════════════════════
# Вебхуки: настройка
# ═══════════════════════════════════════════════════════

async def get_webhook_settings(organization_id: str) -> dict[str, Any]:
    """
    POST /api/1/webhooks/settings — текущие настройки вебхука.
    """
    client = await _get_client()
    headers = await _headers()

    resp = await client.post(
        f"{IIKO_CLOUD_BASE_URL}/api/1/webhooks/settings",
        headers=headers,
        json={"organizationId": organization_id},
    )
    resp.raise_for_status()
    return resp.json()


async def register_webhook(
    organization_id: str,
    webhook_url: str,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """
    POST /api/1/webhooks/update_settings — зарегистрировать вебхук.

    Фильтры:
      - DeliveryOrder: статус Closed (заказ закрыт → товар списан)
      - TableOrder: статус Closed
    """
    t0 = time.monotonic()
    client = await _get_client()
    headers = await _headers()

    payload = {
        "organizationId": organization_id,
        "webHooksUri": webhook_url,
        "authToken": auth_token or IIKO_CLOUD_WEBHOOK_SECRET,
        "webHooksFilter": {
            "deliveryOrderFilter": {
                "orderStatuses": ["Closed"],
                "errors": False,
            },
            "tableOrderFilter": {
                "orderStatuses": ["Closed"],
                "errors": False,
            },
        },
    }

    resp = await client.post(
        f"{IIKO_CLOUD_BASE_URL}/api/1/webhooks/update_settings",
        headers=headers,
        json=payload,
    )
    resp.raise_for_status()
    result = resp.json()

    logger.info(
        "[%s] Вебхук зарегистрирован: %s (org=%s) за %.1f сек",
        LABEL, webhook_url, organization_id, time.monotonic() - t0,
    )
    return result


# ═══════════════════════════════════════════════════════
# Верификация входящего вебхука
# ═══════════════════════════════════════════════════════

def verify_webhook_auth(auth_header: str | None) -> bool:
    """
    Проверить Authorization header входящего вебхука от iikoCloud.
    iikoCloud передаёт authToken, который мы указали при регистрации.
    """
    if not auth_header:
        return False
    # iikoCloud может отправить как "Bearer <token>" так и просто "<token>"
    token = auth_header
    if token.startswith("Bearer "):
        token = token[7:]
    return token == IIKO_CLOUD_WEBHOOK_SECRET
