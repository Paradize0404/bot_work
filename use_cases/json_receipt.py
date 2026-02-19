"""
Use-case: обработка JSON-файлов с кассовыми чеками (ФНС формат).

Поток:
  1. Пользователь отправляет .json файл в бота (без кнопок, без FSM)
  2. parse_receipt_json(data) — парсинг JSON → стандартные документы
  3. Применяется базовый маппинг (из GSheet «Маппинг»)
  4. Если всё замаплено → build_iiko_invoices() → превью бухгалтеру →
     «📤 Отправить в iiko» / «❌ Отменить»
  5. Если есть незамапленные → write_transfer → уведомление о маппинге

Формат JSON (ФНС / «Проверка чеков»):
  [{
    "_id": "...",
    "createdAt": "...",
    "ticket": {
      "document": {
        "receipt": {
          "items": [{"name": "...", "price": 900, "quantity": 1, "sum": 900}],
          "totalSum": 596600,          # в копейках!
          "dateTime": "2026-02-16T09:01:00",
          "user": "ООО \"...\"",       # поставщик
          "userInn": "3906130283  ",
          "fiscalDocumentNumber": 28684,
          ...
        }
      }
    }
  }]

Цены в JSON указаны в КОПЕЙКАХ — конвертируем в рубли (÷ 100).
"""

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def parse_receipt_json(raw_data: str | bytes) -> list[dict[str, Any]]:
    """
    Распарсить JSON-файл с кассовыми чеками ФНС.

    Поддерживает:
      - Массив чеков: [{ticket: ...}, ...]
      - Один чек: {ticket: ...}

    Возвращает list[dict] в формате, совместимом с OCR-результатами:
      {
        "doc_type": "receipt_json",
        "doc_number": "ФД-28684",
        "doc_date": "2026-02-16",
        "supplier": {"name": "...", "inn": "..."},
        "items": [{"name": "...", "qty": 1, "price": 9.00, "sum": 9.00, "unit": "шт"}],
        "total_amount": 5966.00,
        "fiscal_data": {...},
        "confidence_score": 100,
      }
    """
    try:
        data = json.loads(raw_data) if isinstance(raw_data, (str, bytes)) else raw_data
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("[json_receipt] Невалидный JSON: %s", exc)
        raise ValueError(f"Невалидный JSON-файл: {exc}") from exc

    # Нормализация: массив или одиночный объект
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON должен содержать массив чеков или один чек")

    results: list[dict[str, Any]] = []

    for idx, entry in enumerate(data):
        try:
            doc = _parse_single_receipt(entry, idx)
            if doc:
                results.append(doc)
        except Exception as exc:
            logger.warning("[json_receipt] Ошибка парсинга чека #%d: %s", idx, exc)

    if not results:
        raise ValueError("Не удалось извлечь ни одного чека из JSON-файла")

    logger.info("[json_receipt] Распарсено %d чеков из JSON", len(results))
    return results


def _parse_single_receipt(entry: dict, idx: int) -> dict[str, Any] | None:
    """Распарсить один чек из JSON."""
    # Навигация: ticket.document.receipt
    ticket = entry.get("ticket") or {}
    document = ticket.get("document") or {}
    receipt = document.get("receipt") or {}

    if not receipt:
        logger.debug("[json_receipt] Чек #%d: нет ticket.document.receipt — пропуск", idx)
        return None

    # ── Поставщик ──
    supplier_name = (receipt.get("user") or "").strip()
    supplier_inn = (receipt.get("userInn") or "").strip()

    # ── Дата ──
    raw_dt = receipt.get("dateTime") or ""
    doc_date: str = ""
    doc_date_obj: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            doc_date_obj = datetime.strptime(raw_dt[:19], fmt[:19].replace("%z", ""))
            doc_date = doc_date_obj.strftime("%Y-%m-%d")
            break
        except (ValueError, IndexError):
            continue

    # ── Номер документа (фискальный номер) ──
    fiscal_doc_num = receipt.get("fiscalDocumentNumber")
    doc_number = f"ФД-{fiscal_doc_num}" if fiscal_doc_num else f"JSON-{idx + 1}"

    # ── Позиции (цены в КОПЕЙКАХ → рубли) ──
    items: list[dict[str, Any]] = []
    for item_data in receipt.get("items") or []:
        name = (item_data.get("name") or "").strip()
        if not name:
            continue
        qty = float(item_data.get("quantity") or 0)
        price_kop = float(item_data.get("price") or 0)
        sum_kop = float(item_data.get("sum") or 0)

        items.append({
            "name": name,
            "qty": qty,
            "price": round(price_kop / 100, 2),
            "sum": round(sum_kop / 100, 2),
            "unit": "шт",
        })

    if not items:
        logger.debug("[json_receipt] Чек #%d: нет позиций — пропуск", idx)
        return None

    # ── Итого (копейки → рубли) ──
    total_kop = float(receipt.get("totalSum") or 0)
    total_rub = round(total_kop / 100, 2)

    # ── Фискальные данные (для справки) ──
    fiscal_data = {
        "fiscalDocumentNumber": receipt.get("fiscalDocumentNumber"),
        "fiscalDriveNumber": receipt.get("fiscalDriveNumber"),
        "fiscalSign": receipt.get("fiscalSign"),
        "kktRegId": (receipt.get("kktRegId") or "").strip(),
        "retailPlace": receipt.get("retailPlace"),
        "retailPlaceAddress": receipt.get("retailPlaceAddress"),
        "operator": receipt.get("operator"),
        "operatorInn": receipt.get("operatorInn"),
    }

    return {
        "doc_type": "receipt_json",
        "doc_number": doc_number,
        "doc_date": doc_date,
        "date": doc_date,
        "supplier": {
            "name": supplier_name,
            "inn": supplier_inn,
        },
        "items": items,
        "total_amount": total_rub,
        "fiscal_data": fiscal_data,
        "confidence_score": 100,
        "warnings": [],
        "page_count": 1,
    }


def format_json_receipt_preview(
    doc: dict[str, Any],
    invoices: list[dict],
    fully_mapped: bool,
    unmapped_suppliers: list[str],
    unmapped_products: list[str],
) -> str:
    """Сформировать текст превью JSON-чека для Telegram."""
    lines: list[str] = []

    sup = doc.get("supplier") or {}
    sup_name = sup.get("name") or "—"
    sup_inn = sup.get("inn") or ""
    num = doc.get("doc_number") or "б/н"
    date_str = doc.get("doc_date") or "—"
    total = doc.get("total_amount")

    lines.append(f"📄 <b>Чек №{num}</b> от {date_str}")
    lines.append(f"🏭 {sup_name}")
    if sup_inn:
        lines.append(f"ИНН: {sup_inn}")
    if total:
        lines.append(f"💰 Итого: {total:,.2f}\u202f₽".replace(",", "\u00a0"))
    lines.append("")

    items = doc.get("items") or []
    lines.append(f"📦 <b>Позиций: {len(items)}</b>")
    for item in items[:15]:
        name = item.get("name") or "?"
        qty = item.get("qty") or 0
        price = item.get("price") or 0
        mapped_icon = "✅" if item.get("iiko_id") else "❌"
        mapped_name = item.get("iiko_name") or ""
        display = f"{mapped_icon} {name} — {qty} × {price:,.2f}\u202f₽".replace(",", "\u00a0")
        if mapped_name and mapped_name != name:
            display += f"\n      → {mapped_name}"
        lines.append(f"  {display}")
    if len(items) > 15:
        lines.append(f"  … ещё {len(items) - 15} позиций")

    lines.append("")

    if fully_mapped:
        lines.append("✅ <b>Все позиции замаплены!</b>")
        if invoices:
            lines.append(f"📦 Подготовлено накладных: {len(invoices)}")
    else:
        total_unmapped = len(unmapped_suppliers) + len(unmapped_products)
        lines.append(f"⚠️ <b>Не замаплено: {total_unmapped} позиций</b>")
        if unmapped_suppliers:
            lines.append("  Поставщики: " + ", ".join(unmapped_suppliers[:3]))
        if unmapped_products:
            lines.append("  Товары: " + ", ".join(unmapped_products[:5]))
            if len(unmapped_products) > 5:
                lines.append(f"  … и ещё {len(unmapped_products) - 5}")

    return "\n".join(lines)
