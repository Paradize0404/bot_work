"""
Use-case: OCR-распознавание бухгалтерских документов.

Поток:
  1. Фото → Gemini Vision → сырой JSON
  2. Математическая валидация (qty×price=sum, НДС, итоги)
  3. Формирование текстового превью для пользователя
  4. Сохранение результата в БД (ocr_document)

Этот модуль НЕ знает про Telegram — только бизнес-логика.
"""

import hashlib
import json
import logging
import time
from typing import Any

from sqlalchemy import select

from adapters.gemini_vision import recognize_document, recognize_multiple_pages
from db.engine import async_session_factory
from db.models import OcrDocument

logger = logging.getLogger(__name__)
LABEL = "OCR"


# ═══════════════════════════════════════════════════════
# Валидация и автоисправление
# ═══════════════════════════════════════════════════════

def validate_and_fix(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Математическая валидация распознанного документа.

    Проверяет:
      - qty × price ≈ sum_without_vat
      - НДС = sum × rate
      - sum_with_vat = sum_without_vat + vat_sum
      - Итоги = сумма позиций

    Автоисправляет при расхождении. Помечает предупреждения.
    """
    warnings: list[str] = []
    items = doc.get("items") or []

    for i, item in enumerate(items, 1):
        qty = item.get("qty")
        price = item.get("price")
        sum_wo = item.get("sum_without_vat")
        vat_rate_str = item.get("vat_rate", "")

        # qty × price = sum_without_vat
        if qty is not None and price is not None and qty > 0 and price > 0:
            expected = round(qty * price, 2)
            if sum_wo is not None and abs(sum_wo - expected) > 0.5:
                warnings.append(
                    f"Поз.{i}: сумма {sum_wo} ≠ кол-во({qty}) × цена({price}) = {expected}"
                )
                # Не перезаписываем автоматически — помечаем
                item["_sum_mismatch"] = True
            elif sum_wo is None:
                item["sum_without_vat"] = expected

        # НДС расчёт
        vat_rate = _parse_vat_rate(vat_rate_str)
        if vat_rate and sum_wo:
            expected_vat = round(sum_wo * vat_rate, 2)
            actual_vat = item.get("vat_sum")
            if actual_vat is not None and abs(actual_vat - expected_vat) > 0.5:
                warnings.append(
                    f"Поз.{i}: НДС {actual_vat} ≠ {sum_wo} × {vat_rate_str} = {expected_vat}"
                )
            elif actual_vat is None:
                item["vat_sum"] = expected_vat

            # sum_with_vat
            expected_total = round(sum_wo + (item.get("vat_sum") or 0), 2)
            actual_total = item.get("sum_with_vat")
            if actual_total is not None and abs(actual_total - expected_total) > 0.5:
                warnings.append(
                    f"Поз.{i}: итого с НДС {actual_total} ≠ {expected_total}"
                )
            elif actual_total is None:
                item["sum_with_vat"] = expected_total

    # Пересчёт итогов
    calc_total_wo = sum((it.get("sum_without_vat") or 0) for it in items)
    calc_total_vat = sum((it.get("vat_sum") or 0) for it in items)
    calc_total_with = sum((it.get("sum_with_vat") or 0) for it in items)

    doc_total_wo = doc.get("total_without_vat")
    if doc_total_wo and abs(doc_total_wo - calc_total_wo) > 1.0:
        warnings.append(
            f"Итого без НДС: документ {doc_total_wo} ≠ сумма позиций {round(calc_total_wo, 2)}"
        )
    doc["_calc_total_without_vat"] = round(calc_total_wo, 2)
    doc["_calc_total_vat"] = round(calc_total_vat, 2)
    doc["_calc_total_with_vat"] = round(calc_total_with, 2)

    doc["_warnings"] = warnings
    return doc


def _parse_vat_rate(rate_str: str | None) -> float | None:
    """'20%' → 0.2, '10%' → 0.1, 'без НДС' → None."""
    if not rate_str:
        return None
    rate_str = rate_str.strip().lower()
    if "без" in rate_str or "0%" in rate_str:
        return None
    if "20" in rate_str:
        return 0.2
    if "10" in rate_str:
        return 0.1
    return None


# ═══════════════════════════════════════════════════════
# Форматирование превью
# ═══════════════════════════════════════════════════════

def format_preview(doc: dict[str, Any]) -> str:
    """
    Формируем текстовое превью распознанного документа для Telegram.
    """
    lines: list[str] = []

    doc_type = doc.get("doc_type", "Неизвестный")
    lines.append(f"📄 <b>{doc_type}</b>")
    if doc.get("doc_number"):
        lines.append(f"№ {doc['doc_number']}")
    if doc.get("date"):
        lines.append(f"Дата: {doc['date']}")

    # Поставщик
    supplier = doc.get("supplier") or {}
    if supplier.get("name"):
        s_line = f"\n🏢 <b>Поставщик:</b> {supplier['name']}"
        if supplier.get("inn"):
            s_line += f" (ИНН {supplier['inn']})"
        lines.append(s_line)

    # Покупатель
    buyer = doc.get("buyer") or {}
    if buyer.get("name"):
        lines.append(f"🏪 <b>Покупатель:</b> {buyer['name']}")

    # Товары
    items = doc.get("items") or []
    if items:
        lines.append(f"\n📦 <b>Позиции ({len(items)}):</b>")
        for item in items:
            num = item.get("num", "?")
            name = item.get("name", "???")
            qty = item.get("qty", "?")
            unit = item.get("unit", "шт")
            price = item.get("price", "?")
            sum_with = item.get("sum_with_vat") or item.get("sum_without_vat") or "?"

            mismatch = " ⚠️" if item.get("_sum_mismatch") else ""
            lines.append(
                f"  {num}. {name}\n"
                f"     {qty} {unit} × {price} = {sum_with}{mismatch}"
            )

    # Итоги
    lines.append("")
    total = doc.get("total_with_vat") or doc.get("_calc_total_with_vat")
    total_wo = doc.get("total_without_vat") or doc.get("_calc_total_without_vat")
    total_vat = doc.get("total_vat") or doc.get("_calc_total_vat")

    if total_wo:
        lines.append(f"💰 Без НДС: <b>{total_wo}</b>")
    if total_vat:
        lines.append(f"💰 НДС: <b>{total_vat}</b>")
    if total:
        lines.append(f"💰 Итого: <b>{total}</b>")

    # Предупреждения
    warnings = doc.get("_warnings", [])
    if warnings:
        lines.append("\n⚠️ <b>Предупреждения:</b>")
        for w in warnings[:5]:
            lines.append(f"  • {w}")

    # Заметки от LLM
    notes = doc.get("notes")
    if notes:
        lines.append(f"\n📝 {notes}")

    page_info = doc.get("page_info")
    if page_info and page_info != "единственная страница":
        lines.append(f"📄 {page_info}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# OCR pipeline
# ═══════════════════════════════════════════════════════

async def process_photo(
    image_bytes: bytes,
    telegram_id: int,
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Полный пайплайн: фото → OCR → валидация → превью.

    Returns:
        (doc_dict, preview_text)
    """
    t0 = time.monotonic()
    logger.info("[%s] Начинаю OCR для tg:%d", LABEL, telegram_id)

    # 1. OCR через Gemini
    raw_doc = await recognize_document(
        image_bytes,
        known_suppliers=known_suppliers,
        known_buyers=known_buyers,
    )

    # 2. Валидация + автоисправление
    doc = validate_and_fix(raw_doc)

    # 3. Превью
    preview = format_preview(doc)

    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] tg:%d — %s, items=%d, %.1f сек",
        LABEL, telegram_id,
        doc.get("doc_type", "?"),
        len(doc.get("items", [])),
        elapsed,
    )

    return doc, preview


async def process_multiple_photos(
    images: list[bytes],
    telegram_id: int,
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Многостраничный OCR: несколько фото → один документ.

    Returns:
        (doc_dict, preview_text)
    """
    t0 = time.monotonic()
    logger.info("[%s] Multi-page OCR для tg:%d, pages=%d", LABEL, telegram_id, len(images))

    raw_doc = await recognize_multiple_pages(
        images,
        known_suppliers=known_suppliers,
        known_buyers=known_buyers,
    )
    doc = validate_and_fix(raw_doc)
    preview = format_preview(doc)

    elapsed = time.monotonic() - t0
    logger.info("[%s] Multi-page tg:%d — %s, items=%d, %.1f сек",
                LABEL, telegram_id, doc.get("doc_type", "?"),
                len(doc.get("items", [])), elapsed)

    return doc, preview


# ═══════════════════════════════════════════════════════
# Сохранение в БД
# ═══════════════════════════════════════════════════════

async def save_ocr_result(
    telegram_id: int,
    doc: dict[str, Any],
    raw_json: dict[str, Any] | None = None,
) -> int:
    """
    Сохранить результат OCR в таблицу ocr_document.

    Returns:
        id записи.
    """
    async with async_session_factory() as session:
        row = OcrDocument(
            telegram_id=telegram_id,
            doc_type=doc.get("doc_type", "Неизвестный"),
            doc_number=doc.get("doc_number"),
            doc_date=doc.get("date"),
            supplier_name=(doc.get("supplier") or {}).get("name"),
            supplier_inn=(doc.get("supplier") or {}).get("inn"),
            buyer_name=(doc.get("buyer") or {}).get("name"),
            items_count=len(doc.get("items", [])),
            total_with_vat=doc.get("total_with_vat") or doc.get("_calc_total_with_vat"),
            status="recognized",
            raw_json=doc,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info("[%s] Saved OCR doc id=%d, tg:%d", LABEL, row.id, telegram_id)
        return row.id


async def get_ocr_document(doc_id: int) -> OcrDocument | None:
    """Получить OCR-документ по id."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        return result.scalar_one_or_none()


async def update_ocr_status(doc_id: int, status: str) -> None:
    """Обновить статус OCR-документа."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.status = status
            await session.commit()


async def update_ocr_mapped_json(doc_id: int, mapped_doc: dict[str, Any]) -> None:
    """Сохранить замапленный JSON (с _product_id, _supplier_id) в БД."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.mapped_json = mapped_doc
            await session.commit()
            logger.info("[%s] Saved mapped_json doc_id=%d", LABEL, doc_id)


# ═══════════════════════════════════════════════════════
# Хелперы для получения известных поставщиков из БД
# ═══════════════════════════════════════════════════════

async def get_known_suppliers() -> list[str]:
    """Получить список названий поставщиков из iiko_supplier."""
    from db.models import Supplier
    async with async_session_factory() as session:
        result = await session.execute(
            select(Supplier.name).where(Supplier.deleted.is_(False))
        )
        return [r[0] for r in result.all() if r[0]]


async def get_known_buyers() -> list[str]:
    """Получить список названий подразделений (покупателей)."""
    from db.models import Department
    async with async_session_factory() as session:
        result = await session.execute(
            select(Department.name)
        )
        return [r[0] for r in result.all() if r[0]]
