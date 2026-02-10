"""
Адаптер iiko REST API.
Единственный модуль, который знает про HTTP-эндпоинты iiko.
Возвращает «сырые» данные (dict / list[dict]) — без бизнес-логики.

Оптимизации:
  - Один persistent httpx.AsyncClient с keep-alive connection pool
  - Нет пересоздания TCP/TLS на каждый запрос
  - limits: до 20 параллельных коннектов (для asyncio.gather)
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime as _datetime
from typing import Any

import httpx

from iiko_auth import get_auth_token, get_base_url

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120)

# Persistent client — один на весь процесс.
# Переиспользует TCP/TLS соединения (connection pooling).
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Lazy-init persistent httpx client."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            verify=False,
            timeout=_TIMEOUT,
            limits=_LIMITS,
            http2=False,      # iiko не поддерживает h2
        )
    return _client


async def close_client() -> None:
    """Закрыть HTTP-клиент при остановке (вызывается из main.py)."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
        logger.info("httpx client closed")


async def _get_key() -> str:
    return await get_auth_token()


def _base() -> str:
    return get_base_url()


# ─────────────────────────────────────────────────────
# 1. entities/list  (справочники — JSON)
# ─────────────────────────────────────────────────────

async def fetch_entities(root_type: str, include_deleted: bool = True) -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/entities/list"
    params = {"key": key, "rootType": root_type, "includeDeleted": str(include_deleted).lower()}

    logger.info("[API] GET entities rootType=%s — отправляю запрос...", root_type)
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info("[API] GET entities rootType=%s — %d записей, HTTP %d, %.1f сек, %d байт",
                root_type, len(data), resp.status_code, elapsed, len(resp.content))
    return data


# ─────────────────────────────────────────────────────
# 2. suppliers (XML)
# ─────────────────────────────────────────────────────

async def fetch_suppliers() -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/suppliers"
    params = {"key": key}

    logger.info("[API] GET suppliers — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET suppliers — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_employees_xml(resp.text, entity_name="supplier")


# ─────────────────────────────────────────────────────
# 3. departments (XML)
# ─────────────────────────────────────────────────────

async def fetch_departments() -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/corporation/departments"
    params = {"key": key}

    logger.info("[API] GET departments — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET departments — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_corporate_items_xml(resp.text)


# ─────────────────────────────────────────────────────
# 4. stores (XML)
# ─────────────────────────────────────────────────────

async def fetch_stores() -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/corporation/stores"
    params = {"key": key}

    logger.info("[API] GET stores — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET stores — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_corporate_items_xml(resp.text)


# ─────────────────────────────────────────────────────
# 5. groups (XML)
# ─────────────────────────────────────────────────────

async def fetch_groups() -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/corporation/groups"
    params = {"key": key}

    logger.info("[API] GET groups — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET groups — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_corporate_items_xml(resp.text)


# ─────────────────────────────────────────────────────
# 6. products (JSON)
# ─────────────────────────────────────────────────────

async def fetch_products(include_deleted: bool = False) -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/entities/products/list"
    params = {"key": key, "includeDeleted": str(include_deleted).lower()}

    logger.info("[API] GET products — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info("[API] GET products — %d записей, HTTP %d, %.1f сек, %d байт",
                len(data), resp.status_code, elapsed, len(resp.content))
    return data


# ─────────────────────────────────────────────────────
# 7. employees (XML)
# ─────────────────────────────────────────────────────

async def fetch_employees(include_deleted: bool = True) -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/employees"
    params = {"key": key}
    if include_deleted:
        params["includeDeleted"] = "true"

    logger.info("[API] GET employees — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET employees — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_employees_xml(resp.text, entity_name="employee")


# ─────────────────────────────────────────────────────
# 8. employee roles (XML)
# ─────────────────────────────────────────────────────

async def fetch_employee_roles() -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/employees/roles"
    params = {"key": key}

    logger.info("[API] GET employee_roles — отправляю запрос...")
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()

    logger.info("[API] GET employee_roles — HTTP %d, %.1f сек, %d байт",
                resp.status_code, time.monotonic() - t0, len(resp.content))
    return _parse_roles_xml(resp.text)


# ═════════════════════════════════════════════════════
# XML parsers
# ═════════════════════════════════════════════════════

def _parse_employees_xml(xml_str: str, entity_name: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_str)
    result: list[dict[str, Any]] = []
    # findall — только прямые дочерние <employee>, НЕ вложенные
    # (каждый <employee> содержит вложенные теги <employee>, <supplier>, <client> как bool-флаги)
    for emp in root.findall("employee"):
        item: dict[str, Any] = {}
        for child in emp:
            item[child.tag] = child.text
        result.append(item)
    logger.info("Parsed %d %ss from XML", len(result), entity_name)
    return result


def _parse_corporate_items_xml(xml_str: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_str)
    result: list[dict[str, Any]] = []
    for item_el in root.iter("corporateItemDto"):
        result.append(_element_to_dict(item_el))
    for item_el in root.iter("groupDto"):
        result.append(_element_to_dict(item_el))
    logger.info("Parsed %d corporate items from XML", len(result))
    return result


def _parse_roles_xml(xml_str: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_str)
    result: list[dict[str, Any]] = []
    for role in root.iter("role"):
        item: dict[str, Any] = {}
        for child in role:
            item[child.tag] = child.text
        result.append(item)
    logger.info("Parsed %d roles from XML", len(result))
    return result


def _element_to_dict(el: ET.Element) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for child in el:
        if len(child) == 0:
            d[child.tag] = child.text
    return d


# ═════════════════════════════════════════════════════
# 9. Остатки по складам (JSON)
# ═════════════════════════════════════════════════════


async def fetch_stock_balances(
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """
    Получить текущие остатки по складам.

    Endpoint: GET /resto/api/v2/reports/balance/stores?timestamp=...
    timestamp — учётная дата-время (yyyy-MM-dd'T'HH:mm:ss).
              По умолчанию — текущее время (актуальные остатки).
              Важно: если передать только дату без времени (yyyy-MM-dd),
              iiko интерпретирует как 00:00:00 = начало дня = конец вчерашнего,
              и сегодняшние проводки НЕ будут учтены.

    Ответ — JSON: [{"store": UUID, "product": UUID, "amount": float, "sum": float}, ...]
    """
    if not timestamp:
        timestamp = _datetime.now().strftime("%Y-%m-%dT%H:%M:%S")  # yyyy-MM-ddTHH:mm:ss

    key = await _get_key()
    url = f"{_base()}/resto/api/v2/reports/balance/stores"
    params = {"key": key, "timestamp": timestamp}

    logger.info("[API] GET stock_balances — timestamp=%s", timestamp)
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info(
        "[API] GET stock_balances — %d записей, HTTP %d, %.1f сек, %d байт",
        len(data), resp.status_code, elapsed, len(resp.content),
    )
    return data


# ═════════════════════════════════════════════════════
# 10. Отправка документов (JSON POST)
# ═════════════════════════════════════════════════════

async def send_writeoff(document: dict[str, Any]) -> dict[str, Any]:
    """
    Отправить акт списания в iiko.

    POST /resto/api/v2/documents/writeoff?key={token}
    Возвращает {"ok": True} при успехе, иначе выбрасывает исключение.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/documents/writeoff"
    params = {"key": key}

    logger.info(
        "[API] POST writeoff — store=%s, account=%s, items=%d",
        document.get("storeId"),
        document.get("accountId"),
        len(document.get("items", [])),
    )
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.post(url, params=params, json=document)
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] POST writeoff FAIL — HTTP %d, %.1f сек, body=%s",
            resp.status_code, elapsed, body,
        )
        resp.raise_for_status()

    logger.info(
        "[API] POST writeoff OK — HTTP %d, %.1f сек",
        resp.status_code, elapsed,
    )
    return {"ok": True}
