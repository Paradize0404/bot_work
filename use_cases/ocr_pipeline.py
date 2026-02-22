"""
OCR Pipeline — обработка пачки фото документов.

Flow:
1. Загрузка всех фото
2. Проверка качества каждого фото
3. Распознавание через GPT-5.2 Vision
4. Проверка QR-кодов (чеки с QR → отклонять)
5. Группировка по group_key (многостраничные документы)
6. Проверка полноты страниц
7. Объединение страниц
8. Возврат JSON результатов
"""

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from adapters.gpt5_vision_ocr import recognize_document
from utils.photo_validator import validate_photo, get_quality_message
from utils.qr_detector import detect_qr

logger = logging.getLogger(__name__)

ALLOWED_INVOICE_TYPES = {"upd"}
STATUS_REJECTED_NON_INVOICE = "rejected_non_invoice"
STATUS_NEEDS_REVIEW = "needs_review"


@dataclass
class DocumentGroup:
    """Группа страниц одного документа."""

    group_key: str
    pages: List[Dict[str, Any]] = field(default_factory=list)
    total_pages: int = 1
    doc_type: str = "unknown"
    doc_number: str = ""
    doc_date: str = ""
    supplier_name: str = ""
    supplier_inn: str = ""
    is_complete: bool = False
    missing_pages: int = 0


@dataclass
class OCRResult:
    """Результат обработки документа."""

    status: str  # "success", "rejected_qr", "incomplete", "error"
    doc_type: str
    doc_number: Optional[str]
    doc_date: Optional[str]
    supplier: Optional[Dict[str, str]]
    buyer: Optional[Dict[str, str]]
    items: List[Dict[str, Any]]
    total_amount: Optional[float]
    page_count: int
    total_pages: int
    is_merged: bool = False
    group_key: Optional[str] = None
    confidence_score: Optional[float] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw_json: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "doc_type": self.doc_type,
            "doc_number": self.doc_number,
            "doc_date": self.doc_date,
            "supplier": self.supplier,
            "buyer": self.buyer,
            "items": self.items,
            "total_amount": self.total_amount,
            "page_count": self.page_count,
            "total_pages": self.total_pages,
            "is_merged": self.is_merged,
            "group_key": self.group_key,
            "confidence_score": self.confidence_score,
            "warnings": self.warnings,
            "errors": self.errors,
        }


async def mark_docs_pending_mapping(doc_ids: list[str]) -> None:
    """Установить status='pending_mapping' для документов, ожидающих маппинг."""
    if not doc_ids:
        return
    from db.engine import async_session_factory
    from models.ocr import OcrDocument
    from sqlalchemy import update

    async with async_session_factory() as session:
        await session.execute(
            update(OcrDocument)
            .where(OcrDocument.id.in_(doc_ids))
            .values(status="pending_mapping")
        )
        await session.commit()
    logger.info("[ocr] Статус pending_mapping: %d документов", len(doc_ids))

async def save_ocr_document(
    tg_id: int, result_data: dict, file_ids: list[str] | None = None
) -> str | None:
    """Сохранить распознанный документ в БД."""
    try:
        import datetime
        from models.ocr import OcrDocument, OcrItem
        from db.engine import async_session_factory
        from use_cases import user_context as uctx

        doc_date: datetime.datetime | None = None
        raw_date = result_data.get("doc_date") or result_data.get("date") or ""
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                doc_date = datetime.datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

        ctx = await uctx.get_user_context(tg_id)
        supplier = result_data.get("supplier") or {}
        buyer = result_data.get("buyer") or {}

        async with async_session_factory() as session:
            # Удаляем дубликаты с тем же номером/типом, которые ещё не финализированы
            _doc_number = result_data.get("doc_number")
            _doc_type = result_data.get("doc_type") or "unknown"
            if _doc_number:
                from sqlalchemy import (
                    select as _sa_select,
                    delete as _sa_delete,
                    and_ as _sa_and,
                )
                from models.ocr import OcrItem as _OcrItem

                dup_ids = (
                    (
                        await session.execute(
                            _sa_select(OcrDocument.id).where(
                                _sa_and(
                                    OcrDocument.doc_number == _doc_number,
                                    OcrDocument.doc_type == _doc_type,
                                    OcrDocument.status.in_(
                                        ["recognized", "pending_mapping"]
                                    ),
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if dup_ids:
                    # Сначала удаляем дочерние items, затем сам документ
                    await session.execute(
                        _sa_delete(_OcrItem).where(_OcrItem.document_id.in_(dup_ids))
                    )
                    await session.execute(
                        _sa_delete(OcrDocument).where(OcrDocument.id.in_(dup_ids))
                    )
                    logger.info(
                        "[ocr] Удалены дубликаты документа %s (%d шт.)",
                        _doc_number,
                        len(dup_ids),
                    )
            doc = OcrDocument(
                telegram_id=str(tg_id),
                user_id=str(ctx.employee_id) if ctx and ctx.employee_id else None,
                department_id=(
                    str(ctx.department_id) if ctx and ctx.department_id else None
                ),
                doc_type=_doc_type,
                doc_number=_doc_number,
                doc_date=doc_date,
                supplier_name=supplier.get("name"),
                supplier_inn=supplier.get("inn"),
                supplier_id=supplier.get(
                    "iiko_id"
                ),  # сохраняем iiko UUID если уже замаплен
                buyer_name=buyer.get("name"),
                buyer_inn=buyer.get("inn"),
                total_amount=result_data.get("total_amount"),
                status="recognized",
                confidence_score=result_data.get("confidence_score"),
                page_count=result_data.get("page_count") or 1,
                is_multistage=result_data.get("is_merged", False),
                validated_json=result_data,
                tg_file_ids=file_ids or None,
            )
            session.add(doc)
            await session.flush()

            for i, item in enumerate(result_data.get("items") or [], start=1):
                session.add(
                    OcrItem(
                        document_id=doc.id,
                        num=i,
                        raw_name=item.get("name") or "",
                        unit=item.get("unit"),
                        qty=item.get("qty"),
                        price=item.get("price"),
                        sum=item.get("sum"),
                        vat_rate=(
                            str(item.get("vat_rate"))
                            if item.get("vat_rate") is not None
                            else None
                        ),
                        iiko_name=item.get("iiko_name"),
                        iiko_id=item.get("iiko_id"),
                        store_type=item.get("store_type"),
                    )
                )

            await session.commit()
            doc_id = str(doc.id)

        logger.info(
            "[ocr] Сохранён id=%s tg:%d тип=%s №=%s",
            doc_id,
            tg_id,
            result_data.get("doc_type"),
            result_data.get("doc_number"),
        )
        return doc_id

    except Exception:
        logger.exception("[ocr] Ошибка сохранения tg:%d", tg_id)
        return None

async def process_photo_batch(
    photos: List[bytes],
    user_id: int = 0,
    invoices_only: bool = True,
    min_confidence: int = 70,
) -> List[OCRResult]:
    """
    Обработать пачку фото документов.

    Args:
        photos: Список изображений в байтах
        user_id: ID пользователя (для логирования)

    Returns:
        Список результатов OCR для каждого документа
    """
    logger.info(
        "[OCR Pipeline] Start processing %d photos for user %s (invoices_only=%s, min_confidence=%d)",
        len(photos),
        user_id,
        invoices_only,
        min_confidence,
    )

    results: List[OCRResult] = []

    # ── Шаг 1: Параллельная проверка качества + OCR всех фото ──
    # Каждый вызов GPT Vision независим → запускаем все одновременно.

    async def _process_one(idx: int, photo: bytes):
        """Проверить качество + распознать одно фото. Возвращает (idx, outcome).
        outcome — либо dict с recognized-данными, либо OCRResult с ошибкой/отклонением.
        """
        logger.info("[OCR Pipeline] Processing photo %d/%d", idx + 1, len(photos))

        # 1.1 Проверка качества
        quality = await validate_photo(photo)
        if not quality.is_good:
            logger.warning(
                "[OCR Pipeline] Photo %d failed quality check: %s",
                idx + 1,
                quality.issues,
            )
            return idx, OCRResult(
                status="error",
                doc_type="unknown",
                doc_number=None,
                doc_date=None,
                supplier=None,
                buyer=None,
                items=[],
                total_amount=None,
                page_count=0,
                total_pages=0,
                errors=[f"Плохое качество фото: {', '.join(quality.issues)}"],
            )

        # 1.2 Распознавание через GPT Vision
        try:
            result = await recognize_document(photo)
            result = _normalize_invoice_result(result)

            if invoices_only and result.get("doc_type") not in ALLOWED_INVOICE_TYPES:
                logger.info(
                    "[OCR Pipeline] Photo %d rejected: doc_type=%s is not invoice",
                    idx + 1,
                    result.get("doc_type"),
                )
                return idx, OCRResult(
                    status=STATUS_REJECTED_NON_INVOICE,
                    doc_type=result.get("doc_type", "unknown"),
                    doc_number=result.get("doc_number"),
                    doc_date=result.get("date"),
                    supplier=result.get("supplier"),
                    buyer=result.get("buyer"),
                    items=[],
                    total_amount=result.get("total_amount"),
                    page_count=1,
                    total_pages=1,
                    warnings=[
                        "Документ не является накладной/УПД и не будет загружен в систему."
                    ],
                )

            logger.info(
                "[OCR Pipeline] Photo %d recognized: %s",
                idx + 1,
                result.get("doc_type"),
            )
            return idx, {"photo_index": idx, "result": result, "image_bytes": photo}

        except Exception as e:
            logger.error("[OCR Pipeline] Photo %d recognition failed: %s", idx + 1, e)
            return idx, OCRResult(
                status="error",
                doc_type="unknown",
                doc_number=None,
                doc_date=None,
                supplier=None,
                buyer=None,
                items=[],
                total_amount=None,
                page_count=0,
                total_pages=0,
                errors=[f"Ошибка распознавания: {str(e)}"],
            )

    # Запускаем все фото параллельно
    outcomes = await asyncio.gather(*[_process_one(i, p) for i, p in enumerate(photos)])

    # Разбираем результаты, сохраняя порядок
    recognized = []
    for _idx, outcome in sorted(outcomes, key=lambda x: x[0]):
        if isinstance(outcome, OCRResult):
            results.append(outcome)
        else:
            recognized.append(outcome)

    # Шаг 2: Проверка QR-кодов (чеки с QR → отклонять)
    logger.info("[OCR Pipeline] Checking QR codes")
    filtered = []
    for item in recognized:
        result = item["result"]
        doc_type = result.get("doc_type", "unknown")
        has_qr = result.get("has_qr", False)

        # Отклоняем ТОЛЬКО чеки с QR
        if doc_type == "receipt" and has_qr:
            logger.warning(
                f"[OCR Pipeline] Receipt with QR rejected: {result.get('doc_number')}"
            )
            results.append(
                OCRResult(
                    status="rejected_qr",
                    doc_type=doc_type,
                    doc_number=result.get("doc_number"),
                    doc_date=result.get("date"),
                    supplier=result.get("supplier"),
                    buyer=result.get("buyer"),
                    items=[],
                    total_amount=None,
                    page_count=1,
                    total_pages=1,
                    warnings=[
                        "Чек с QR-кодом. Используйте ФНС: https://check.nalog.ru/"
                    ],
                )
            )
        else:
            filtered.append(item)

    logger.info(f"[OCR Pipeline] {len(filtered)} photos passed QR check")

    # Шаг 3: Группировка по group_key (двухпроходная)
    logger.info("[OCR Pipeline] Grouping by group_key")
    groups: Dict[str, DocumentGroup] = {}
    orphan_items = []  # страницы 2+ без явного group_key

    # Первый проход: items с group_key или страница 1
    for item in filtered:
        result = item["result"]
        group_key = result.get("group_key")
        page_number = result.get("page_number", 1)

        if not group_key and page_number > 1:
            # Страница 2+ без group_key — откладываем до второго прохода
            orphan_items.append(item)
            continue

        if not group_key:
            # Нет group_key → создаём уникальный
            group_key = f"single_{item['photo_index']}_{time.monotonic()}"

        if group_key not in groups:
            groups[group_key] = DocumentGroup(
                group_key=group_key,
                doc_type=result.get("doc_type", "unknown"),
                doc_number=result.get("doc_number", ""),
                doc_date=result.get("date", ""),
                supplier_name=result.get("supplier", {}).get("name", ""),
                supplier_inn=result.get("supplier", {}).get("inn", ""),
            )

        groups[group_key].pages.append(result)

        # Обновляем total_pages
        page_total = result.get("total_pages", 1)
        if page_total > groups[group_key].total_pages:
            groups[group_key].total_pages = page_total

    # Второй проход: осиротевшие страницы 2+ — пробуем сопоставить по ИНН + дате
    for item in orphan_items:
        result = item["result"]
        inn = (result.get("supplier") or {}).get("inn") or ""
        date_val = result.get("date") or ""
        matched_key = None

        if inn and date_val:
            for gk, grp in groups.items():
                if grp.supplier_inn == inn and grp.doc_date == date_val:
                    matched_key = gk
                    logger.info(
                        "[OCR Pipeline] Orphan page matched to group %s by INN=%s date=%s",
                        gk,
                        inn,
                        date_val,
                    )
                    break

        if not matched_key:
            matched_key = f"single_{item['photo_index']}_{time.monotonic()}"
            groups[matched_key] = DocumentGroup(
                group_key=matched_key,
                doc_type=result.get("doc_type", "unknown"),
                doc_number=result.get("doc_number", ""),
                doc_date=date_val,
                supplier_name=(result.get("supplier") or {}).get("name", ""),
                supplier_inn=inn,
            )
            logger.warning(
                "[OCR Pipeline] Orphan page %d: no matching group found (INN=%s date=%s)",
                item["photo_index"],
                inn,
                date_val,
            )

        groups[matched_key].pages.append(result)
        page_total = result.get("total_pages", 1)
        if page_total > groups[matched_key].total_pages:
            groups[matched_key].total_pages = page_total

    # Шаг 4: Проверка полноты страниц
    logger.info("[OCR Pipeline] Checking page completeness")
    for group_key, group in groups.items():
        page_count = len(group.pages)
        total_pages = group.total_pages

        if page_count < total_pages:
            group.missing_pages = total_pages - page_count
            group.is_complete = False
            logger.warning(
                f"[OCR Pipeline] Incomplete document {group_key}: "
                f"{page_count} of {total_pages} pages"
            )
        else:
            group.is_complete = True
            logger.info(
                f"[OCR Pipeline] Complete document {group_key}: {page_count} pages"
            )

    # Шаг 5: Обработка каждой группы
    logger.info("[OCR Pipeline] Processing groups")
    for group_key, group in groups.items():
        ocr_result = await process_document_group(group, min_confidence=min_confidence)
        results.append(ocr_result)

    logger.info(f"[OCR Pipeline] Finished. {len(results)} documents processed")
    return results


async def process_document_group(
    group: DocumentGroup, min_confidence: int = 70
) -> OCRResult:
    """
    Обработать группу страниц одного документа.

    Args:
        group: Группа страниц

    Returns:
        Результат OCR
    """
    warnings = []
    errors = []

    # Проверка на неполноту
    if not group.is_complete:
        warnings.append(
            f"Документ неполный: загружено {len(group.pages)} из {group.total_pages} стр. "
            f"Не хватает {group.missing_pages} стр."
        )

    # Сортировка страниц по номеру
    group.pages.sort(key=lambda p: p.get("page_number", 1))

    # Объединение страниц
    merged = merge_pages(group.pages)

    # Пост-валидация и автопоправки для стабильного импорта накладных.
    merged, validation_warnings, validation_errors, needs_review = (
        _validate_invoice_document(merged, min_confidence=min_confidence)
    )
    warnings.extend(validation_warnings)
    errors.extend(validation_errors)

    # Извлекаем данные из объединённого документа
    supplier = merged.get("supplier")
    buyer = merged.get("buyer")

    # Собираем все warnings
    quality_check = merged.get("quality_check", {})
    if quality_check.get("warnings"):
        warnings.extend(quality_check["warnings"])

    # Добавляем предупреждение о неполноте
    if not group.is_complete:
        quality_check["missing_pages_warning"] = True

    status = "success" if group.is_complete else "incomplete"
    if needs_review:
        status = STATUS_NEEDS_REVIEW

    return OCRResult(
        status=status,
        doc_type=merged.get("doc_type", "unknown"),
        doc_number=merged.get("doc_number"),
        doc_date=merged.get("date"),
        supplier=supplier,
        buyer=buyer,
        items=merged.get("items", []),
        total_amount=merged.get("total_amount"),
        page_count=len(group.pages),
        total_pages=group.total_pages,
        is_merged=len(group.pages) > 1,
        group_key=group.group_key if len(group.pages) > 1 else None,
        confidence_score=quality_check.get("confidence_score"),
        warnings=warnings,
        errors=errors,
        raw_json=merged,
    )


def merge_pages(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Объединить несколько страниц в один документ.

    Args:
        pages: Список результатов распознавания страниц

    Returns:
        Объединённый документ
    """
    if not pages:
        return {}

    if len(pages) == 1:
        return pages[0]

    # Берём данные из первой страницы
    merged = pages[0].copy()

    # Объединяем товары из всех страниц
    all_items = []
    for page in pages:
        items = page.get("items", [])
        all_items.extend(items)

    merged["items"] = all_items
    merged["page_count"] = len(pages)
    merged["is_merged"] = True

    # Пересчитываем total_amount
    total = sum(item.get("sum", 0) for item in all_items)
    merged["total_amount"] = round(total, 2)

    # Обновляем quality_check
    quality_check = merged.get("quality_check", {})
    quality_check["merged_from_pages"] = [p.get("page_number") for p in pages]
    quality_check["confidence_score"] = min(
        p.get("quality_check", {}).get("confidence_score", 100) for p in pages
    )
    merged["quality_check"] = quality_check

    logger.info(
        f"[OCR Pipeline] Merged {len(pages)} pages, {len(all_items)} items, total={total}"
    )

    return merged


def _normalize_invoice_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Привести поля к предсказуемому виду до группировки/валидации."""
    result = dict(result)

    supplier = result.get("supplier") or {}
    buyer = result.get("buyer") or {}
    result["supplier"] = {
        "name": (supplier.get("name") or "").strip() or None,
        "inn": _normalize_inn(supplier.get("inn")),
    }
    result["buyer"] = (
        {
            "name": (buyer.get("name") or "").strip() or None,
            "inn": _normalize_inn(buyer.get("inn")),
        }
        if buyer
        else None
    )

    result["doc_number"] = (result.get("doc_number") or "").strip() or None
    result["date"] = (result.get("date") or "").strip() or None

    normalized_items: List[Dict[str, Any]] = []
    for item in result.get("items", []) or []:
        normalized_items.append(
            {
                "name": (item.get("name") or "").strip(),
                "unit": (item.get("unit") or "").strip() or None,
                "qty": _to_float(item.get("qty")),
                "price": _to_float(item.get("price")),
                "sum": _to_float(item.get("sum")),
                "vat_rate": item.get("vat_rate"),
            }
        )
    result["items"] = normalized_items
    result["total_amount"] = _to_float(result.get("total_amount"))

    # Стабильный group_key для многостраничных накладных.
    if not result.get("group_key"):
        inn = result["supplier"].get("inn")
        if inn and result.get("doc_number") and result.get("date"):
            result["group_key"] = f"{inn}_{result['doc_number']}_{result['date']}"

    return result


_VAT_RATE_MAP: Dict[str, Decimal] = {
    "10%": Decimal("0.10"),
    "20%": Decimal("0.20"),
    "22%": Decimal("0.22"),  # OCR иногда читает 20% как 22%, плюс акцизы
    "5%": Decimal("0.05"),
    "7%": Decimal("0.07"),
    "без ндс": Decimal("0.00"),
    "без нДС": Decimal("0.00"),
    "без НДС": Decimal("0.00"),
}


def _parse_vat(vat_str) -> Optional[Decimal]:
    """Вернуть десятичную ставку НДС или None если неизвестно."""
    if not vat_str:
        return None
    return _VAT_RATE_MAP.get(str(vat_str).strip(), None)


def _validate_invoice_document(
    doc: Dict[str, Any],
    min_confidence: int = 70,
) -> tuple[Dict[str, Any], List[str], List[str], bool]:
    """Проверка критических полей и арифметики с мягкой автокоррекцией.

    Для УПД/акта: sum по позиции = стоимость С НДС (последний столбец).
    Если GPT вернул sum = qty × price (без НДС) — автокорректируем.
    """
    warnings: List[str] = []
    errors: List[str] = []
    needs_review = False

    supplier = doc.get("supplier") or {}
    if not supplier.get("name"):
        errors.append("Не найдено название поставщика.")
    if not supplier.get("inn"):
        errors.append("Не найден ИНН поставщика.")
    if not doc.get("doc_number"):
        errors.append("Не найден номер документа.")
    if not doc.get("date"):
        errors.append("Не найдена дата документа.")
    if not (doc.get("items") or []):
        errors.append("Не найдены товарные позиции.")

    # Для УПД и актов — sum должен быть С НДС.
    # Для чеков (receipt) — цены уже включают НДС, sum = qty × price.
    apply_vat_correction = doc.get("doc_type") in ("upd", "act", "other", None)

    fixed_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(doc.get("items", []) or [], start=1):
        qty = _to_float(item.get("qty"))
        price = _to_float(item.get("price"))
        row_sum = _to_float(item.get("sum"))

        if qty is not None and price is not None:
            q = Decimal(str(qty))
            p = Decimal(str(price))
            excl = (q * p).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # без НДС

            raw_vat = (
                _parse_vat(item.get("vat_rate"))
                if apply_vat_correction
                else Decimal("0")
            )
            vat_unknown = (
                raw_vat is None
            )  # ставка есть в документе но не в нашей таблице
            vat_dec = raw_vat if raw_vat is not None else Decimal("0")

            if vat_dec > 0:
                expected_incl = (excl * (1 + vat_dec)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                expected_incl = excl

            excl_f = float(excl)
            incl_f = float(expected_incl)

            if row_sum is None:
                # Нет суммы — ставим с НДС
                item["sum"] = incl_f
                warnings.append(
                    f"Позиция {idx}: сумма не указана, подставлено {incl_f}."
                )
            elif apply_vat_correction and vat_dec > 0 and abs(row_sum - excl_f) <= 0.51:
                # GPT вернул сумму без НДС → корректируем
                item["sum"] = incl_f
                warnings.append(
                    f"Позиция {idx}: сумма без НДС исправлена {row_sum} → {incl_f}."
                )
            elif (
                not vat_unknown
                and abs(row_sum - incl_f) > 0.51
                and abs(row_sum - excl_f) > 0.51
            ):
                # Не совпадает ни с тем ни с тем — предупреждаем, оставляем.
                # Если ставка НДС нам неизвестна — молчим: сумма из документа
                # берётся как достоверная (столбец 9 «Стоимость с налогом»).
                warnings.append(
                    f"Позиция {idx}: сумма {row_sum} не совпадает с расчётной {incl_f}."
                )

        fixed_items.append(item)

    doc["items"] = fixed_items
    calc_total = round(sum(_to_float(it.get("sum")) or 0.0 for it in fixed_items), 2)
    total_amount = _to_float(doc.get("total_amount"))
    if total_amount is None:
        doc["total_amount"] = calc_total
        warnings.append(f"Итоговая сумма не найдена: подставлена {calc_total}.")
    elif abs(total_amount - calc_total) > 5:
        warnings.append(
            f"Итоговая сумма скорректирована {total_amount} → {calc_total}."
        )
        doc["total_amount"] = calc_total

    confidence = doc.get("quality_check", {}).get("confidence_score")
    confidence_num = _to_float(confidence)
    if confidence_num is not None and confidence_num < min_confidence:
        warnings.append(
            f"Низкая уверенность OCR: {confidence_num}% (< {min_confidence}%)."
        )
        needs_review = True

    # Любая критическая нехватка полей отправляет документ в ручную проверку.
    if errors:
        needs_review = True
    return doc, warnings, errors, needs_review


def _normalize_inn(value: Any) -> Optional[str]:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits or None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(" ", "").replace(",", ".")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError, TypeError):
        return None
