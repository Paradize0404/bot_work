"""
Use-case: отправка OCR-документа в iiko как приходная накладная.

Потоки:
  1. Загрузить OcrDocument из БД (mapped_json)
  2. Найти склад (store) — по buyer → fuzzy match или env DEFAULT
  3. Собрать iiko-документ (supplierId, storeId, items[])
  4. Вызвать adapters.iiko_api.send_incoming_invoice()
  5. Сохранить ответ iiko в OcrDocument.iiko_response
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from thefuzz import fuzz

from adapters.iiko_api import send_incoming_invoice
from db.engine import async_session_factory
from db.models import OcrDocument, Store

logger = logging.getLogger(__name__)
LABEL = "OCR→iiko"

# Если задан, используем как дефолтный склад для приходных
DEFAULT_INCOMING_STORE_ID: str | None = os.getenv("OCR_DEFAULT_STORE_ID")


# ═══════════════════════════════════════════════════════
# Главная функция
# ═══════════════════════════════════════════════════════

async def send_ocr_to_iiko(doc_id: int) -> dict[str, Any]:
    """
    Отправить OCR-документ в iiko как приходную накладную.

    Returns:
        {"ok": True, "response": "..."} или {"ok": False, "error": "..."}
    """
    logger.info("[%s] Отправка doc_id=%d в iiko", LABEL, doc_id)

    # 1. Загрузить документ из БД
    doc_row = await _load_document(doc_id)
    if not doc_row:
        return {"ok": False, "error": f"Документ #{doc_id} не найден в БД"}

    doc = doc_row.mapped_json or doc_row.raw_json
    if not doc:
        return {"ok": False, "error": "Нет данных документа (mapped_json и raw_json пусты)"}

    # 2. Проверить наличие supplier_id
    supplier_id = doc.get("_supplier_id")
    if not supplier_id:
        return {"ok": False, "error": "Поставщик не замаплен (нет _supplier_id)"}

    # 3. Определить склад
    store_id = await _resolve_store_id(doc)
    if not store_id:
        return {"ok": False, "error": "Не удалось определить склад (storeId). Задайте OCR_DEFAULT_STORE_ID."}

    # 4. Собрать items
    items = _build_items(doc)
    if not items:
        return {"ok": False, "error": "Нет замапленных товаров для отправки"}

    # 5. Собрать документ iiko
    date_incoming = _parse_date(doc.get("date", ""))
    iiko_doc: dict[str, Any] = {
        "storeId": store_id,
        "supplierId": supplier_id,
        "dateIncoming": date_incoming,
        "status": "NEW",
        "comment": f"OCR #{doc_id} | {doc.get('doc_type', '')} | {doc.get('doc_number', '')}",
        "items": items,
    }

    if doc.get("doc_number"):
        iiko_doc["documentNumber"] = doc["doc_number"]

    logger.info(
        "[%s] doc_id=%d → iiko: supplier=%s, store=%s, items=%d",
        LABEL, doc_id, supplier_id, store_id, len(items),
    )

    # 6. Отправить
    try:
        result = await send_incoming_invoice(iiko_doc)
    except Exception as e:
        logger.exception("[%s] doc_id=%d — ошибка отправки: %s", LABEL, doc_id, e)
        await _save_iiko_response(doc_id, f"EXCEPTION: {e}")
        return {"ok": False, "error": str(e)}

    # 7. Сохранить ответ
    resp_text = result.get("response") or result.get("error") or str(result)
    await _save_iiko_response(doc_id, resp_text)

    return result


# ═══════════════════════════════════════════════════════
# Вспомогательные функции
# ═══════════════════════════════════════════════════════

async def _load_document(doc_id: int) -> OcrDocument | None:
    """Загрузить OcrDocument из БД."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        return result.scalar_one_or_none()


def _build_items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Собрать список items для iiko из OCR-документа.

    Каждый элемент items после маппинга содержит:
      _product_id  — UUID товара в iiko
      qty          — количество
      price        — цена за единицу
      sum_without_vat / sum_with_vat — суммы
    """
    result: list[dict[str, Any]] = []
    for item in doc.get("items") or []:
        product_id = item.get("_product_id")
        if not product_id:
            continue  # пропускаем незамапленные

        qty = item.get("qty") or 0
        price = item.get("price") or 0
        # Предпочитаем sum_without_vat, но если его нет — берём sum_with_vat
        total = item.get("sum_without_vat") or item.get("sum_with_vat") or round(qty * price, 2)

        result.append({
            "productId": product_id,
            "amount": qty,
            "price": price,
            "sum": round(total, 2),
        })
    return result


def _parse_date(date_str: str) -> str:
    """Конвертировать дату из формата LLM (ДД.ММ.ГГГГ) в iiko (YYYY-MM-DD HH:mm:ss).

    Если не удалось — возвращаем текущую дату.
    """
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Попробовать различные форматы
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    # Если есть цифры — попробовать извлечь
    digits = re.findall(r"\d+", date_str)
    if len(digits) >= 3:
        try:
            day, month, year = int(digits[0]), int(digits[1]), int(digits[2])
            if year < 100:
                year += 2000
            dt = datetime(year, month, day)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            pass

    logger.warning("[%s] Не удалось распарсить дату '%s', используем текущую", LABEL, date_str)
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _resolve_store_id(doc: dict[str, Any]) -> str | None:
    """Определить UUID склада для приходной накладной.

    Приоритет:
      1. OCR_DEFAULT_STORE_ID из env
      2. Fuzzy match buyer_name → store.name в БД
      3. Первый не-удалённый склад
    """
    # 1. Из env
    if DEFAULT_INCOMING_STORE_ID:
        return DEFAULT_INCOMING_STORE_ID

    # 2. Fuzzy match по buyer
    buyer_name = (doc.get("buyer") or {}).get("name", "")

    async with async_session_factory() as session:
        result = await session.execute(
            select(Store).where(Store.deleted.is_(False))
        )
        stores = result.scalars().all()

    if not stores:
        logger.error("[%s] Нет складов в БД", LABEL)
        return None

    if buyer_name:
        best_score = 0
        best_store = None
        buyer_lower = buyer_name.lower().strip()

        for s in stores:
            sname = (s.name or "").lower().strip()
            score = fuzz.token_sort_ratio(buyer_lower, sname)
            if score > best_score:
                best_score = score
                best_store = s

        if best_store and best_score >= 70:
            logger.info(
                "[%s] Buyer '%s' → store '%s' (score=%d)",
                LABEL, buyer_name, best_store.name, best_score,
            )
            return str(best_store.id)

    # 3. Fallback: первый склад
    first = stores[0]
    logger.warning(
        "[%s] Не удалось определить склад по buyer '%s', используем '%s'",
        LABEL, buyer_name, first.name,
    )
    return str(first.id)


async def _save_iiko_response(doc_id: int, response: str) -> None:
    """Сохранить ответ iiko в OCR-документ."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.iiko_response = response[:2000]  # ограничиваем длину
            await session.commit()
