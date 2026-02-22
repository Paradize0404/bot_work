"""
Адаптер iiko REST API.
Единственный модуль, который знает про HTTP-эндпоинты iiko.
Возвращает «сырые» данные (dict / list[dict]) — без бизнес-логики.

Оптимизации:
  - Один persistent httpx.AsyncClient с keep-alive connection pool
  - Нет пересоздания TCP/TLS на каждый запрос
  - limits: до 20 параллельных коннектов (для asyncio.gather)
"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from use_cases._helpers import now_kgd as _now_kgd
from typing import Any

import httpx

from iiko_auth import get_auth_token, get_base_url

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
_LIMITS = httpx.Limits(
    max_connections=20, max_keepalive_connections=10, keepalive_expiry=120
)

# Persistent client — один на весь процесс.
# Переиспользует TCP/TLS соединения (connection pooling).
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Lazy-init persistent httpx client."""
    global _client
    if _client is None or _client.is_closed:
        from config import IIKO_VERIFY_SSL

        _client = httpx.AsyncClient(
            verify=IIKO_VERIFY_SSL,
            timeout=_TIMEOUT,
            limits=_LIMITS,
            http2=False,  # iiko не поддерживает h2
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


# ═════════════════════════════════════════════════════
# Retry-обёртка для GET-запросов
# ═════════════════════════════════════════════════════

_RETRYABLE = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
_MAX_RETRIES = 3
_RETRY_DELAYS = (1, 3, 7)  # секунды между попытками


async def _get_with_retry(
    url: str,
    params: dict,
    *,
    label: str = "",
) -> httpx.Response:
    """
    GET-запрос с retry при transient-ошибках (disconnect, timeout).
    До _MAX_RETRIES попыток с экспоненциальной задержкой.
    """
    client = await _get_client()
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "[API] %s — попытка %d/%d: %s. Повтор через %d сек...",
                    label,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[API] %s — все %d попыток не удались: %s",
                    label,
                    _MAX_RETRIES,
                    exc,
                )
    raise last_exc  # type: ignore[misc]


# ─────────────────────────────────────────────────────
# 1. entities/list  (справочники — JSON)
# ─────────────────────────────────────────────────────


async def fetch_entities(
    root_type: str, include_deleted: bool = True
) -> list[dict[str, Any]]:
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/entities/list"
    params = {
        "key": key,
        "rootType": root_type,
        "includeDeleted": str(include_deleted).lower(),
    }

    label = f"entities rootType={root_type}"
    logger.info("[API] GET %s — отправляю запрос...", label)
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label=label)
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info(
        "[API] GET entities rootType=%s — %d записей, HTTP %d, %.1f сек, %d байт",
        root_type,
        len(data),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="suppliers")

    logger.info(
        "[API] GET suppliers — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="departments")

    logger.info(
        "[API] GET departments — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="stores")

    logger.info(
        "[API] GET stores — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="groups")

    logger.info(
        "[API] GET groups — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="products")
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info(
        "[API] GET products — %d записей, HTTP %d, %.1f сек, %d байт",
        len(data),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="employees")

    logger.info(
        "[API] GET employees — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
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
    resp = await _get_with_retry(url, params, label="employee_roles")

    logger.info(
        "[API] GET employee_roles — HTTP %d, %.1f сек, %d байт",
        resp.status_code,
        time.monotonic() - t0,
        len(resp.content),
    )
    return _parse_roles_xml(resp.text)


# ─────────────────────────────────────────────────────
# 9. product groups (JSON)
# ─────────────────────────────────────────────────────


async def fetch_product_groups() -> list[dict[str, Any]]:
    """
    Получить список номенклатурных групп.
    GET /resto/api/v2/entities/products/group/list
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/entities/products/group/list"
    params = {"key": key}

    logger.info("[API] GET product_groups — отправляю запрос...")
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label="product_groups")
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info(
        "[API] GET product_groups — %d записей, HTTP %d, %.1f сек, %d байт",
        len(data),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return data


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
        timestamp = _now_kgd().strftime(
            "%Y-%m-%dT%H:%M:%S"
        )  # yyyy-MM-ddTHH:mm:ss (Калининград)

    key = await _get_key()
    url = f"{_base()}/resto/api/v2/reports/balance/stores"
    params = {"key": key, "timestamp": timestamp}

    logger.info("[API] GET stock_balances — timestamp=%s", timestamp)
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label="stock_balances")
    elapsed = time.monotonic() - t0

    data = resp.json()
    logger.info(
        "[API] GET stock_balances — %d записей, HTTP %d, %.1f сек, %d байт",
        len(data),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return data


# ─────────────────────────────────────────────────────
# 9b. OLAP остатки (v1 GET) — с иерархией групп Product.TopParent
# ─────────────────────────────────────────────────────

_OLAP_V1_DIMENSIONS = (
    "Account.Name",  # склад / счёт
    "Product.TopParent",  # группа 1 уровня (TopParent)
    "Product.Name",  # позиция номенклатуры
    "Product.MeasureUnit",  # единица измерения
)
_OLAP_V1_METRICS = (
    "FinalBalance.Amount",  # остаток кол-во
    "FinalBalance.Money",  # остаток денежный
)


def _parse_olap_xml(xml_text: str) -> list[dict[str, Any]]:
    """Парсинг XML-ответа OLAP v1 (<rows><r><Account.Name>...</Account.Name>...</r></rows>)."""
    import xml.etree.ElementTree as ET

    def _cast(v: str | None) -> Any:
        if v is None:
            return None
        text = v.strip()
        if not text:
            return None
        for caster in (int, float):
            try:
                return caster(text)
            except (ValueError, TypeError):
                pass
        return text

    root = ET.fromstring(xml_text)
    rows = []
    for elem in root.findall("./r"):
        row: dict[str, Any] = {}
        for child in elem:
            row[child.tag] = _cast(child.text)
        rows.append(row)
    return rows


async def fetch_olap_transactions_v1(
    date_from_ddmmyyyy: str,
    date_to_ddmmyyyy: str,
) -> list[dict[str, Any]]:
    """
    Получить остатки по складам через OLAP v1 GET endpoint.

    Endpoint: GET /resto/api/reports/olap
    Формат дат: DD.MM.YYYY (например, "20.02.2026").
    Возвращает строки с полями: Account.Name, Product.TopParent, Product.Name,
                                Product.MeasureUnit, FinalBalance.Amount, FinalBalance.Money.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/reports/olap"

    # httpx требует список пар для multi-value params
    params: list[tuple[str, str]] = [
        ("key", key),
        ("report", "TRANSACTIONS"),
        ("from", date_from_ddmmyyyy),
        ("to", date_to_ddmmyyyy),
    ]
    for dim in _OLAP_V1_DIMENSIONS:
        params.append(("groupRow", dim))
    for metric in _OLAP_V1_METRICS:
        params.append(("agr", metric))

    label = f"olap_v1 {date_from_ddmmyyyy}..{date_to_ddmmyyyy}"
    logger.info("[API] GET %s — запрашиваю...", label)
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] GET %s FAIL — HTTP %d, %.1fs, body=%s",
            label,
            resp.status_code,
            elapsed,
            body,
        )
        resp.raise_for_status()

    content_type = (resp.headers.get("content-type") or "").lower()
    if "json" in content_type:
        payload = resp.json()
        rows = payload.get("data") or payload.get("rows") or payload.get("report") or []
    elif "xml" in content_type or content_type.startswith("text/"):
        rows = _parse_olap_xml(resp.text)
    else:
        # Попробуем JSON, затем XML
        try:
            payload = resp.json()
            rows = payload.get("data") or payload.get("rows") or []
        except Exception:
            rows = _parse_olap_xml(resp.text)

    logger.info(
        "[API] GET %s — %d строк, HTTP %d, %.1f сек, %d байт",
        label,
        len(rows),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return rows


# ─────────────────────────────────────────────────────
# 9b1b. OLAP v1 продажи (исторические данные)
# ─────────────────────────────────────────────────────

# Измерения, совпадающие с полями пресета «Выручка себестоимость бот»
_OLAP_SALES_DIMENSIONS = ["Department", "PayTypes", "CookingPlaceType"]
_OLAP_SALES_METRICS = ["DishDiscountSumInt", "ProductCostBase.ProductCost"]


async def fetch_olap_sales_v1(
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """
    Получить данные о продажах через OLAP v1 GET endpoint (report=SALES).

    Поддерживает произвольные исторические диапазоны дат.
    Возвращает строки с теми же полями, что и fetch_olap_by_preset:
        Department, PayTypes, CookingPlaceType,
        DishDiscountSumInt, ProductCostBase.ProductCost

    Параметры date_from / date_to принимаются в формате ISO
    (YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS) и конвертируются в DD.MM.YYYY.
    """
    # Конвертация ISO → DD.MM.YYYY (v1 API принимает только этот формат)
    def _to_ddmmyyyy(iso: str) -> str:
        date_part = iso[:10]  # берём YYYY-MM-DD
        y, m, d = date_part.split("-")
        return f"{d}.{m}.{y}"

    from_ddmm = _to_ddmmyyyy(date_from)
    to_ddmm = _to_ddmmyyyy(date_to)

    key = await _get_key()
    url = f"{_base()}/resto/api/reports/olap"
    params: list[tuple[str, str]] = [
        ("key", key),
        ("report", "SALES"),
        ("from", from_ddmm),
        ("to", to_ddmm),
    ]
    for dim in _OLAP_SALES_DIMENSIONS:
        params.append(("groupRow", dim))
    for metric in _OLAP_SALES_METRICS:
        params.append(("agr", metric))

    label = f"olap_sales_v1 {from_ddmm}"
    logger.info("[API] GET %s — запрашиваю...", label)
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.get(url, params=params)
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] GET %s FAIL — HTTP %d, %.1fs, body=%s",
            label,
            resp.status_code,
            elapsed,
            body,
        )
        resp.raise_for_status()

    rows = _parse_olap_xml(resp.text)

    logger.info(
        "[API] GET %s — %d строк, HTTP %d, %.1f сек, %d байт",
        label,
        len(rows),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return rows


# ─────────────────────────────────────────────────────
# 9b2. OLAP v2 по preset ID (продажи / себестоимость)
# ─────────────────────────────────────────────────────


async def fetch_olap_by_preset(
    preset_id: str,
    date_from: str,
    date_to: str,
    *,
    department_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Получить данные OLAP-отчёта по preset ID через iiko REST API v2.

    Endpoint: GET /resto/api/v2/reports/olap/byPresetId/{preset_id}
    Параметры:
        date_from / date_to — формат ISO: YYYY-MM-DDTHH:MM:SS
        summary=true — включить суммарные строки
        department_ids — список UUID подразделений для фильтрации (опционально)
    Возвращает список строк (dict) из поля «data» ответа.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/reports/olap/byPresetId/{preset_id}"
    params: dict[str, Any] = {
        "key": key,
        "dateFrom": date_from,
        "dateTo": date_to,
        "summary": "true",
    }
    if department_ids:
        params["departmentIds"] = ",".join(department_ids)

    label = f"olap_preset {preset_id[:8]}… {date_from[:10]}"
    logger.info("[API] GET %s — запрашиваю...", label)
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label=label)
    elapsed = time.monotonic() - t0

    data = resp.json()
    rows = data.get("data", [])

    logger.info(
        "[API] GET %s — %d строк, HTTP %d, %.1f сек, %d байт",
        label,
        len(rows),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return rows


# ─────────────────────────────────────────────────────
# 9c. Внутреннее перемещение (POST)
# ─────────────────────────────────────────────────────


async def send_internal_transfer(document: dict[str, Any]) -> dict[str, Any]:
    """
    Отправить внутреннее перемещение в iiko.

    POST /resto/api/v2/documents/internalTransfer?key={token}
    Документ: {"dateIncoming", "status", "comment", "storeFromId", "storeToId", "items":[...]}
    items: [{"productId": UUID, "amount": float, "measureUnitId": UUID}, ...]
    Возвращает {"ok": True} при успехе, иначе выбрасывает исключение.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/documents/internalTransfer"
    params = {"key": key}

    logger.info(
        "[API] POST internalTransfer — from=%s, to=%s, items=%d",
        document.get("storeFromId"),
        document.get("storeToId"),
        len(document.get("items", [])),
    )
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.post(url, params=params, json=document)
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] POST internalTransfer FAIL — HTTP %d, %.1f сек, body=%s",
            resp.status_code,
            elapsed,
            body,
        )
        resp.raise_for_status()

    logger.info(
        "[API] POST internalTransfer OK — HTTP %d, %.1f сек",
        resp.status_code,
        elapsed,
    )
    return {"ok": True}


# ═════════════════════════════════════════════════════
# 10. Отправка документов (JSON POST)
# ═════════════════════════════════════════════════════


async def send_writeoff(document: dict[str, Any]) -> dict[str, Any]:
    """
    Отправить акт списания в iiko.

    POST /resto/api/v2/documents/writeoff?key={token}
    Возвращает {"ok": True} при успехе, иначе выбрасывает исключение.
    """
    from use_cases.errors import is_transient

    key = await _get_key()
    url = f"{_base()}/resto/api/v2/documents/writeoff"
    params = {"key": key}

    doc_id = document.get("id", "unknown")
    logger.info(
        "[API] POST writeoff — id=%s, store=%s, account=%s, items=%d",
        doc_id,
        document.get("storeId"),
        document.get("accountId"),
        len(document.get("items", [])),
    )

    client = await _get_client()
    max_retries = 2
    backoff = (2, 5)

    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            resp = await client.post(url, params=params, json=document)
            elapsed = time.monotonic() - t0

            if resp.status_code >= 400:
                body = resp.text[:500] if resp.text else ""
                logger.error(
                    "[API] POST writeoff FAIL — HTTP %d, %.1f сек, body=%s",
                    resp.status_code,
                    elapsed,
                    body,
                )
                resp.raise_for_status()

            logger.info(
                "[API] POST writeoff OK — HTTP %d, %.1f сек",
                resp.status_code,
                elapsed,
            )
            return {"ok": True}

        except Exception as e:
            if attempt == max_retries or not is_transient(e):
                logger.error(
                    "[API] POST writeoff failed permanently after %d attempts: %s",
                    attempt + 1,
                    e,
                )
                raise

            delay = backoff[attempt] if attempt < len(backoff) else backoff[-1]
            logger.warning(
                "[API] POST writeoff retry %d/%d for doc %s: %s. Waiting %ds...",
                attempt + 1,
                max_retries,
                doc_id,
                e,
                delay,
            )
            await asyncio.sleep(delay)

    return {"ok": False}


def _build_outgoing_invoice_xml(document: dict[str, Any]) -> str:
    """Собрать XML расходной накладной для iiko import API.

    Outgoing invoice DTO (outgoingInvoiceDto) использует ДРУГИЕ имена тегов,
    чем incoming invoice:
      - defaultStoreId  (не defaultStore)
      - counteragentId  (не supplier)
      - productId       (не product)   — в items
      - useDefaultDocumentTime (не useDefaultDocumentNumber)
    См. https://ru.iiko.help/articles/api-documentations/
         zagruzka-i-redaktirovanie-raskhodnoy-nakladnoy
    """
    from uuid import uuid4

    root = ET.Element("document")
    ET.SubElement(root, "documentNumber").text = f"BOT-{uuid4().hex[:8].upper()}"
    ET.SubElement(root, "dateIncoming").text = document.get("dateIncoming", "")
    ET.SubElement(root, "useDefaultDocumentTime").text = "false"
    ET.SubElement(root, "status").text = document.get("status", "NEW")
    comment = document.get("comment", "")
    if comment:
        ET.SubElement(root, "comment").text = comment

    ET.SubElement(root, "defaultStoreId").text = document["storeId"]
    ET.SubElement(root, "counteragentId").text = document["counteragentId"]

    items_el = ET.SubElement(root, "items")
    for idx, item in enumerate(document.get("items", []), 1):
        item_el = ET.SubElement(items_el, "item")
        ET.SubElement(item_el, "num").text = str(idx)
        ET.SubElement(item_el, "productId").text = item["productId"]
        ET.SubElement(item_el, "productArticle").text = ""
        ET.SubElement(item_el, "amount").text = str(round(item["amount"], 4))
        unit_id = item.get("measureUnitId", "")
        if unit_id:
            ET.SubElement(item_el, "amountUnit").text = unit_id
        container_id = item.get("containerId", "")
        if container_id:
            ET.SubElement(item_el, "containerId").text = container_id
        ET.SubElement(item_el, "price").text = str(round(item.get("price", 0), 2))
        ET.SubElement(item_el, "sum").text = str(round(item.get("sum", 0), 2))

    xml_decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return xml_decl + ET.tostring(root, encoding="unicode")


async def send_outgoing_invoice(document: dict[str, Any]) -> dict[str, Any]:
    """
    Отправить расходную накладную в iiko — XML import.

    POST /resto/api/documents/import/outgoingInvoice?key={token}
    Content-Type: application/xml
    Возвращает {"ok": True} при успехе, иначе выбрасывает исключение.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/documents/import/outgoingInvoice"
    params = {"key": key}

    xml_body = _build_outgoing_invoice_xml(document)

    logger.info(
        "[API] POST outgoingInvoice (XML import) — store=%s, counteragent=%s, items=%d",
        document.get("storeId"),
        document.get("counteragentId"),
        len(document.get("items", [])),
    )
    logger.debug(
        "[API] outgoingInvoice XML body (first 2000 chars):\n%s", xml_body[:2000]
    )
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.post(
        url,
        params=params,
        content=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] POST outgoingInvoice FAIL — HTTP %d, %.1f сек, body=%s\nXML sent:\n%s",
            resp.status_code,
            elapsed,
            body,
            xml_body[:1000],
        )
        resp.raise_for_status()

    resp_body = resp.text[:500] if resp.text else ""
    logger.info(
        "[API] POST outgoingInvoice HTTP %d, %.1f сек, body=%s",
        resp.status_code,
        elapsed,
        resp_body,
    )

    # iiko возвращает 200 даже при ошибке валидации — проверяем <valid>true/false</valid>
    try:
        resp_root = ET.fromstring(resp.text or "")
        valid_el = resp_root.find("valid")
        if valid_el is not None and valid_el.text == "false":
            err_el = resp_root.find("errorMessage")
            err_msg = err_el.text if err_el is not None else "неизвестная ошибка"
            doc_num_el = resp_root.find("documentNumber")
            doc_num = doc_num_el.text if doc_num_el is not None else "?"
            logger.error(
                "[API] outgoingInvoice VALIDATION FAILED — doc=%s, error: %s",
                doc_num,
                err_msg,
            )
            return {"ok": False, "error": err_msg, "documentNumber": doc_num}
    except ET.ParseError:
        logger.warning("[API] Не удалось распарсить XML-ответ: %s", resp_body)

    return {"ok": True, "response": resp_body}


# ── 10b. Приходная накладная — XML import ────────────


def _build_incoming_invoice_xml(document: dict[str, Any]) -> str:
    """Собрать XML приходной накладной для iiko import API.

    Incoming invoice DTO использует ДРУГИЕ имена тегов,
    чем outgoing invoice:
      - defaultStore   (не defaultStoreId)
      - supplier       (не counteragentId)
      - product        (не productId)   — в items
    См. https://ru.iiko.help/articles/api-documentations/
         zagruzka-i-redaktirovanie-nakl
    """
    from uuid import uuid4

    root = ET.Element("document")

    doc_number = document.get("documentNumber") or f"INC-{uuid4().hex[:8].upper()}"
    ET.SubElement(root, "documentNumber").text = doc_number
    ET.SubElement(root, "dateIncoming").text = document.get("dateIncoming", "")
    ET.SubElement(root, "useDefaultDocumentTime").text = "false"
    # PROCESSED = накладная проводится автоматически при импорте
    ET.SubElement(root, "status").text = document.get("status", "PROCESSED")

    comment = document.get("comment", "")
    if comment:
        ET.SubElement(root, "comment").text = comment

    ET.SubElement(root, "defaultStore").text = document["storeId"]
    ET.SubElement(root, "supplier").text = document["supplierId"]

    store_id = document["storeId"]
    items_el = ET.SubElement(root, "items")
    for idx, item in enumerate(document.get("items", []), 1):
        item_el = ET.SubElement(items_el, "item")
        ET.SubElement(item_el, "num").text = str(idx)
        ET.SubElement(item_el, "product").text = item["productId"]
        ET.SubElement(item_el, "productArticle").text = item.get("productArticle", "")
        ET.SubElement(item_el, "store").text = store_id
        ET.SubElement(item_el, "amount").text = str(round(item["amount"], 4))
        unit_id = item.get("measureUnitId", "")
        if unit_id:
            ET.SubElement(item_el, "amountUnit").text = unit_id
        container_id = item.get("containerId", "")
        if container_id:
            ET.SubElement(item_el, "containerId").text = container_id
        ET.SubElement(item_el, "price").text = str(round(item.get("price", 0), 2))
        ET.SubElement(item_el, "sum").text = str(round(item.get("sum", 0), 2))

    xml_decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return xml_decl + ET.tostring(root, encoding="unicode")


async def send_incoming_invoice(document: dict[str, Any]) -> dict[str, Any]:
    """
    Отправить приходную накладную в iiko — XML import.

    POST /resto/api/documents/import/incomingInvoice?key={token}
    Content-Type: application/xml
    Возвращает {"ok": True} при успехе, иначе {"ok": False, "error": "..."}.
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/documents/import/incomingInvoice"
    params = {"key": key}

    xml_body = _build_incoming_invoice_xml(document)

    logger.info(
        "[API] POST incomingInvoice (XML import) — store=%s, supplier=%s, items=%d",
        document.get("storeId"),
        document.get("supplierId"),
        len(document.get("items", [])),
    )
    logger.debug(
        "[API] incomingInvoice XML body (first 2000 chars):\n%s", xml_body[:2000]
    )
    t0 = time.monotonic()
    client = await _get_client()
    resp = await client.post(
        url,
        params=params,
        content=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    elapsed = time.monotonic() - t0

    if resp.status_code >= 400:
        body = resp.text[:500] if resp.text else ""
        logger.error(
            "[API] POST incomingInvoice FAIL — HTTP %d, %.1f сек, body=%s\nXML sent:\n%s",
            resp.status_code,
            elapsed,
            body,
            xml_body,
        )
        # Не бросаем исключение — возвращаем структурированную ошибку
        return {
            "ok": False,
            "error": (
                f"iiko HTTP {resp.status_code}: {body}"
                if body
                else f"iiko HTTP {resp.status_code}"
            ),
        }

    resp_body = resp.text[:500] if resp.text else ""
    logger.info(
        "[API] POST incomingInvoice HTTP %d, %.1f сек, body=%s",
        resp.status_code,
        elapsed,
        resp_body,
    )

    # iiko возвращает 200 даже при ошибке валидации — проверяем <valid>true/false</valid>
    try:
        resp_root = ET.fromstring(resp.text or "")
        valid_el = resp_root.find("valid")
        if valid_el is not None and valid_el.text == "false":
            err_el = resp_root.find("errorMessage")
            err_msg = err_el.text if err_el is not None else "неизвестная ошибка"
            doc_num_el = resp_root.find("documentNumber")
            doc_num = doc_num_el.text if doc_num_el is not None else "?"
            logger.error(
                "[API] incomingInvoice VALIDATION FAILED — doc=%s, error: %s",
                doc_num,
                err_msg,
            )
            return {"ok": False, "error": err_msg, "documentNumber": doc_num}
    except ET.ParseError:
        logger.warning("[API] Не удалось распарсить XML-ответ: %s", resp_body)

    return {"ok": True, "response": resp_body}


# ═════════════════════════════════════════════════════
# 11. Приходные накладные — экспорт (XML GET)
# ═════════════════════════════════════════════════════


async def fetch_incoming_invoices(
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """
    Экспорт приходных накладных за период.

    GET /resto/api/documents/export/incomingInvoice?from=...&to=...
    date_from, date_to — формат YYYY-MM-DD.
    Возвращает список документов, каждый с items[{productId, amount, price, sum}].
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/documents/export/incomingInvoice"
    params = {"key": key, "from": date_from, "to": date_to}

    label = f"incoming_invoices {date_from}..{date_to}"
    logger.info("[API] GET %s — отправляю запрос...", label)
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label=label)
    elapsed = time.monotonic() - t0

    xml_str = resp.text
    logger.info(
        "[API] GET %s — HTTP %d, %.1f сек, %d байт",
        label,
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return _parse_incoming_invoices_xml(xml_str)


def _parse_incoming_invoices_xml(xml_str: str) -> list[dict[str, Any]]:
    """Парсинг XML-ответа приходных накладных → список документов.

    Реальная XML-структура iiko:
      <incomingInvoiceDtoes>
        <document>
          <id>UUID</id>
          <dateIncoming>YYYY-MM-DD ...</dateIncoming>
          <status>...</status>
          <supplier>UUID</supplier>
          <defaultStore>UUID</defaultStore>
          <items>
            <item>
              <product>UUID</product>       ← НЕ productId!
              <store>UUID</store>           ← НЕ storeId!
              <price>...</price>
              <amount>...</amount>
              <sum>...</sum>
              <priceWithoutVat>...</priceWithoutVat>  ← НЕ priceWithoutNds!
              <actualAmount>...</actualAmount>
              ...
            </item>
          </items>
        </document>
      </incomingInvoiceDtoes>
    """
    root = ET.fromstring(xml_str)
    documents: list[dict[str, Any]] = []

    # Маппинг XML-тегов → ключей в результате (для обратной совместимости)
    _ITEM_TAG_MAP: dict[str, str] = {
        "product": "productId",
        "store": "storeId",
        "priceWithoutVat": "priceWithoutNds",
        # остальные — 1-к-1
        "actualAmount": "actualAmount",
        "amount": "amount",
        "price": "price",
        "sum": "sum",
        "vatPercent": "ndsPercent",
    }

    for doc_el in root.findall("document"):
        doc: dict[str, Any] = {
            "id": None,
            "dateIncoming": None,
            "status": None,
            "supplier": None,
            "defaultStore": None,
            "items": [],
        }
        for child in doc_el:
            if child.tag == "id":
                doc["id"] = (child.text or "").strip()
            elif child.tag == "dateIncoming":
                doc["dateIncoming"] = (child.text or "").strip()
            elif child.tag == "status":
                doc["status"] = (child.text or "").strip()
            elif child.tag == "supplier":
                doc["supplier"] = (child.text or "").strip()
            elif child.tag == "defaultStore":
                doc["defaultStore"] = (child.text or "").strip()
            elif child.tag == "items":
                for item_el in child.findall("item"):
                    item: dict[str, Any] = {}
                    for ic in item_el:
                        mapped = _ITEM_TAG_MAP.get(ic.tag)
                        if mapped:
                            item[mapped] = (ic.text or "").strip()
                    if item.get("productId"):
                        doc["items"].append(item)

        if doc["items"]:
            documents.append(doc)

    logger.info("Parsed %d incoming invoices from XML", len(documents))
    return documents


# ═════════════════════════════════════════════════════
# 12. Технологические карты (JSON GET)
# ═════════════════════════════════════════════════════


async def fetch_assembly_charts(
    date_from: str,
    date_to: str,
    include_prepared: bool = True,
) -> dict[str, Any]:
    """
    Получить все техкарты за период.

    GET /resto/api/v2/assemblyCharts/getAll?dateFrom=...&dateTo=...&includePreparedCharts=...
    Возвращает ChartResultDto: {assemblyCharts, preparedCharts, ...}
    """
    key = await _get_key()
    url = f"{_base()}/resto/api/v2/assemblyCharts/getAll"
    params = {
        "key": key,
        "dateFrom": date_from,
        "dateTo": date_to,
        "includeDeletedProducts": "false",
        "includePreparedCharts": str(include_prepared).lower(),
    }

    label = f"assembly_charts {date_from}..{date_to}"
    logger.info("[API] GET %s — отправляю запрос...", label)
    t0 = time.monotonic()
    resp = await _get_with_retry(url, params, label=label)
    elapsed = time.monotonic() - t0

    data = resp.json()
    ac = data.get("assemblyCharts") or []
    pc = data.get("preparedCharts") or []
    logger.info(
        "[API] GET %s — %d assembly + %d prepared charts, HTTP %d, %.1f сек, %d байт",
        label,
        len(ac),
        len(pc),
        resp.status_code,
        elapsed,
        len(resp.content),
    )
    return data
