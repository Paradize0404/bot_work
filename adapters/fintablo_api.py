"""
Адаптер FinTablo REST API.
Единственный модуль, который знает про HTTP-эндпоинты FinTablo.
Возвращает «сырые» данные (list[dict]) — без бизнес-логики.

Оптимизации:
  - Один persistent httpx.AsyncClient с keep-alive connection pool
  - Authorization: Bearer для всех запросов
  - Rate limit: 300 req/min (FinTablo ограничение)
  - Семафор (макс 4 параллельных запроса) чтобы не пробить rate limit
  - Retry с exponential backoff при 429 Too Many Requests
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from config import FINTABLO_BASE_URL, FINTABLO_TOKEN

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
_LIMITS = httpx.Limits(
    max_connections=20, max_keepalive_connections=10, keepalive_expiry=120
)

# Семафор: макс 4 одновременных запроса к FinTablo (300 req/min → safe margin)
_semaphore = asyncio.Semaphore(4)

# Retry-настройки для 429
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 2.0  # секунд, далее *2 каждый retry

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Lazy-init persistent httpx client с Bearer token."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=FINTABLO_BASE_URL,
            headers={
                "Authorization": f"Bearer {FINTABLO_TOKEN}",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT,
            limits=_LIMITS,
        )
    return _client


async def close_client() -> None:
    """Закрыть HTTP-клиент при остановке (вызывается из main.py)."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
        logger.info("FinTablo httpx client closed")


# ═══════════════════════════════════════════════════════
# Generic fetcher — все FinTablo GET-списки одинаковые:
#   GET /v1/{endpoint} → {"status": 200, "items": [...]}
#
# FinTablo НЕ пагинирует list-эндпоинты — возвращает все
# записи за один запрос. Пагинация убрана.
# ═══════════════════════════════════════════════════════


async def _fetch_list(endpoint: str, label: str, **params) -> list[dict[str, Any]]:
    """
    Универсальный fetch для всех FinTablo list-эндпоинтов.
    Один GET-запрос → все записи. Retry с backoff при 429.
    """
    client = await _get_client()

    logger.info("[FT-API] GET %s — отправляю запрос...", endpoint)
    t0 = time.monotonic()

    async with _semaphore:
        resp = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await client.get(
                    f"/v1/{endpoint}",
                    params=params if params else None,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "[FT-API] %s — 429 Too Many Requests, retry %d/%d через %.0f сек",
                        endpoint,
                        attempt,
                        _MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        else:
            # Все попытки исчерпаны (все 429)
            raise httpx.HTTPStatusError(
                f"[FT-API] {endpoint} — 429 после {_MAX_RETRIES} попыток",
                request=resp.request,
                response=resp,
            )

    data = resp.json()
    items = data.get("items", [])

    elapsed = time.monotonic() - t0
    logger.info("[FT-API] GET %s — %d записей, %.1f сек", label, len(items), elapsed)
    return items


# ═══════════════════════════════════════════════════════
# Public API — 1 функция = 1 эндпоинт
# ═══════════════════════════════════════════════════════


async def fetch_categories() -> list[dict[str, Any]]:
    """Статьи ДДС."""
    return await _fetch_list("category", "categories")


async def fetch_moneybags() -> list[dict[str, Any]]:
    """Счета."""
    return await _fetch_list("moneybag", "moneybags")


async def fetch_partners() -> list[dict[str, Any]]:
    """Контрагенты."""
    return await _fetch_list("partner", "partners")


async def fetch_directions() -> list[dict[str, Any]]:
    """Направления."""
    return await _fetch_list("direction", "directions")


async def fetch_moneybag_groups() -> list[dict[str, Any]]:
    """Группы счетов."""
    return await _fetch_list("moneybag-group", "moneybag_groups")


async def fetch_goods() -> list[dict[str, Any]]:
    """Товары."""
    return await _fetch_list("goods", "goods")


async def fetch_obtainings() -> list[dict[str, Any]]:
    """Закупки."""
    return await _fetch_list("obtaining", "obtainings")


async def fetch_jobs() -> list[dict[str, Any]]:
    """Услуги."""
    return await _fetch_list("job", "jobs")


async def fetch_deals() -> list[dict[str, Any]]:
    """Сделки."""
    return await _fetch_list("deal", "deals")


async def fetch_obligation_statuses() -> list[dict[str, Any]]:
    """Статусы обязательств."""
    return await _fetch_list("obligation-status", "obligation_statuses")


async def fetch_obligations() -> list[dict[str, Any]]:
    """Обязательства."""
    return await _fetch_list("obligation", "obligations")


async def fetch_pnl_categories() -> list[dict[str, Any]]:
    """Статьи ПиУ."""
    return await _fetch_list("pnl-category", "pnl_categories")


async def fetch_employees() -> list[dict[str, Any]]:
    """Сотрудники."""
    return await _fetch_list("employees", "employees")
