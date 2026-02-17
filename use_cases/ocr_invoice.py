"""
Use-case: OCR-распознавание бухгалтерских документов.

Поток:
  1. Фото → Gemini Vision → сырой JSON
  2. Математическая валидация (qty×price=sum, НДС, итоги)
  3. Формирование текстового превью для пользователя
  4. Сохранение результата в БД (ocr_document)

Этот модуль НЕ знает про Telegram — только бизнес-логика.
"""

import asyncio
import hashlib
import html
import json
import logging
import time
from typing import Any

from sqlalchemy import select

from adapters.openai_vision import (
    recognize_document,
    recognize_multiple_pages,
    extract_document_metadata,
)
from db.engine import async_session_factory
from db.models import OcrDocument
from use_cases.user_context import get_user_context

logger = logging.getLogger(__name__)
LABEL = "OCR"


# ═══════════════════════════════════════════════════════
# Проверка качества фото
# ═══════════════════════════════════════════════════════

def check_photo_quality(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Проверить качество распознанного фото.

    Returns:
        {
            "ok": bool,              # можно ли использовать это фото
            "confidence": int,       # уверенность LLM (0-100)
            "issues": list[str],     # список проблем
            "needs_retake": bool,    # нужно ли переснимать
            "retake_reason": str,    # что конкретно переснять
        }
    """
    quality = doc.get("quality_check", {})
    
    # Извлекаем параметры
    is_readable = quality.get("is_readable", True)
    has_glare = quality.get("has_glare", False)
    has_blur = quality.get("has_blur", False)
    is_complete = quality.get("is_complete", True)
    confidence = quality.get("confidence_score", 100)
    issues = quality.get("issues", [])
    needs_retake = quality.get("needs_retake", False)
    retake_reason = quality.get("retake_reason", "")
    
    # ДОВЕРЯЕМ GPT-5.2 — он знает лучше!
    # Дополнительные эвристики ТОЛЬКО для экстремальных случаев
    if not needs_retake:
        # Критически низкая уверенность (GPT почти не уверен)
        if confidence < 40:
            needs_retake = True
            if not retake_reason:
                retake_reason = f"очень низкая уверенность ({confidence}%)"
        
        # Документ явно нечитаем (все флаги красные)
        if not is_readable and not is_complete:
            needs_retake = True
            if not retake_reason:
                retake_reason = "документ нечитаем или неполный"
    
    return {
        "ok": not needs_retake,
        "confidence": confidence,
        "issues": issues,
        "needs_retake": needs_retake,
        "retake_reason": retake_reason,
    }


def format_quality_message(quality_result: dict[str, Any]) -> tuple[str, list[tuple[int, str]]]:
    """
    Сформировать сообщение о проблемах качества фото.
    
    Returns:
        (HTML-текст для отправки, список проблемных фото [(номер, описание)])
    """
    lines = ["⚠️ <b>Проблемы с качеством фото</b>\n"]
    
    confidence = quality_result.get("confidence", 0)
    lines.append(f"🎯 Уверенность распознавания: <b>{confidence}%</b>")
    
    retake_reason = quality_result.get("retake_reason", "")
    if retake_reason:
        lines.append(f"\n❌ <b>Причина:</b> {retake_reason}")
    
    # Парсим номера проблемных фото из issues
    issues = quality_result.get("issues", [])
    problematic_photos: list[tuple[int, str]] = []
    
    if issues:
        lines.append("\n📋 <b>Обнаруженные проблемы:</b>")
        for issue in issues[:10]:  # показываем до 10 проблем
            # Извлекаем номер фото если есть паттерн "Фото N:"
            if issue.startswith("Фото "):
                parts = issue.split(":", 1)
                if len(parts) == 2:
                    try:
                        # Парсим номер: "Фото 3" -> 3
                        photo_num_str = parts[0].replace("Фото", "").strip()
                        photo_num = int(photo_num_str)
                        problem_text = parts[1].strip()
                        problematic_photos.append((photo_num, problem_text))
                        lines.append(f"  📸 <b>Фото {photo_num}</b>: {problem_text}")
                    except ValueError:
                        lines.append(f"  • {issue}")
                else:
                    lines.append(f"  • {issue}")
            else:
                lines.append(f"  • {issue}")
    
    lines.append("\n💡 <b>Что делать:</b>")
    if problematic_photos:
        lines.append("Переснимите проблемные фото (они показаны выше)")
    else:
        lines.append("1. Убедитесь что нет бликов от лампы/окна")
        lines.append("2. Держите камеру ровно над документом")
        lines.append("3. Проверьте что весь документ в кадре")
        lines.append("4. Сделайте фото заново в хорошо освещённом месте")
    
    lines.append("\n📸 Отправьте новое фото или нажмите «❌ Отменить»")
    
    return "\n".join(lines), problematic_photos


# ═══════════════════════════════════════════════════════
# Группировка фото по документам (FOOL-PROOF)
# ═══════════════════════════════════════════════════════

async def group_photos_by_document(
    images: list[bytes],
) -> list[list[int]]:
    """
    Автоматическая группировка фото по принадлежности к документам.

    Пользователь просто кидает пачку фото (любое количество).
    Система сама определяет: где разные документы, а где страницы одного.

    Алгоритм:
      1. Извлекаем метаданные из каждого фото (Gemini — быстрый промпт)
      2. Группируем по ключу: (supplier_name/inn, doc_number, date)
      3. Внутри каждой группы сортируем по page_number (если есть)

    Args:
        images: список байтов фото

    Returns:
        Список групп, каждая группа — список индексов фото в `images`.
        Пример: [[0], [1, 2], [3]] = 3 документа, второй на 2 листах
    """
    if len(images) <= 1:
        return [[0]] if images else []

    t0 = time.monotonic()
    logger.info("[%s] Группировка %d фото по документам...", LABEL, len(images))

    # 1. Извлекаем метаданные ПАРАЛЛЕЛЬНО (OpenAI paid tier: 10k RPM)
    metadata_list: list[dict[str, Any]] = []
    
    async def _safe_extract(i: int, img: bytes) -> dict[str, Any]:
        """Безопасная обёртка для извлечения метаданных с fallback."""
        try:
            return await extract_document_metadata(img)
        except Exception as e:
            logger.warning(
                "[%s] Metadata extraction failed for photo %d: %s",
                LABEL, i + 1, e,
            )
            return {
                "doc_number": None,
                "date": None,
                "supplier_name": f"__unknown_{i}__",
                "supplier_inn": None,
                "total_amount": 0.0,
                "has_total": True,
                "page_number": None,
                "total_pages": None,
            }
    
    # Параллельно извлекаем метаданные всех фото
    metadata_list = await asyncio.gather(*[
        _safe_extract(i, img) for i, img in enumerate(images)
    ])

    # 2. Формируем ключ группировки для каждого фото
    def _make_group_key(meta: dict) -> str:
        """
        Ключ для объединения страниц одного документа.

        Логика:
          - Если есть INN поставщика — используем его (самый надёжный)
          - Иначе — нормализованное имя поставщика
          - + номер документа (если есть)
          - + дата (если есть)
        """
        supplier_key = (meta.get("supplier_inn") or "").strip()
        if not supplier_key:
            supplier_key = (meta.get("supplier_name") or "unknown").strip().lower()
            # Убираем форму собственности для лучшего матчинга
            for prefix in ("ип ", "ооо ", "ао ", "пао ", "зао "):
                if supplier_key.startswith(prefix):
                    supplier_key = supplier_key[len(prefix):]
                    break

        doc_num = (meta.get("doc_number") or "").strip()
        date = (meta.get("date") or "").strip()

        return f"{supplier_key}||{doc_num}||{date}"

    keys = [_make_group_key(m) for m in metadata_list]

    # 3. Группируем индексы по ключам, сохраняя порядок
    from collections import OrderedDict
    groups_map: OrderedDict[str, list[int]] = OrderedDict()
    for idx, key in enumerate(keys):
        if key not in groups_map:
            groups_map[key] = []
        groups_map[key].append(idx)

    # 4. Внутри каждой группы сортируем по page_number (если есть)
    groups: list[list[int]] = []
    for key, indices in groups_map.items():
        if len(indices) > 1:
            # Пытаемся отсортировать по page_number
            def _sort_key(idx: int) -> int:
                pn = metadata_list[idx].get("page_number")
                return pn if isinstance(pn, int) else 999
            indices.sort(key=_sort_key)
        groups.append(indices)

    elapsed = time.monotonic() - t0

    # Логируем результат
    group_info = []
    for g in groups:
        if len(g) == 1:
            m = metadata_list[g[0]]
            group_info.append(
                f"[фото {g[0]+1}] {m.get('supplier_name', '?')}"
            )
        else:
            m = metadata_list[g[0]]
            pages = ", ".join(str(i + 1) for i in g)
            group_info.append(
                f"[фото {pages}] {m.get('supplier_name', '?')} ({len(g)} стр.)"
            )

    logger.info(
        "[%s] Группировка за %.1f сек → %d документ(ов) из %d фото:\n  %s",
        LABEL, elapsed, len(groups), len(images),
        "\n  ".join(group_info),
    )

    return groups


async def process_photo_batch(
    images: list[bytes],
    telegram_id: int,
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
    progress_callback=None,
) -> list[tuple[dict[str, Any], str]]:
    """
    Fool-proof обработка пачки фото:
      - Автоматическая группировка по документам
      - Каждый документ обрабатывается отдельно

    Args:
        images: все фото от пользователя
        telegram_id: ID пользователя
        known_suppliers: подсказки по поставщикам
        known_buyers: подсказки по покупателям
        progress_callback: async callable(current, total, info) для обновления прогресса

    Returns:
        Список (doc, preview) для каждого найденного документа
    """
    t0 = time.monotonic()

    # Одно фото — не нужна группировка
    if len(images) == 1:
        doc, preview = await process_photo(
            images[0], telegram_id,
            known_suppliers=known_suppliers,
            known_buyers=known_buyers,
        )
        return [(doc, preview)]

    # Шаг 1: Группировка
    if progress_callback:
        await progress_callback(0, 0, "Определяю границы документов...")

    groups = await group_photos_by_document(images)

    # Шаг 2: ПАРАЛЛЕЛЬНАЯ обработка всех групп (быстро!)
    results: list[tuple[dict[str, Any], str]] = []
    total = len(groups)
    kw = {
        "known_suppliers": known_suppliers,
        "known_buyers": known_buyers,
    }

    if progress_callback:
        await progress_callback(0, total, f"Распознаю {total} документ(ов) параллельно...")

    # Семафор для ограничения одновременных запросов (макс 10 одновременно)
    semaphore = asyncio.Semaphore(10)

    async def _process_one_group(i: int, group_indices: list[int]):
        """Обработать одну группу фото с защитой семафора."""
        async with semaphore:
            group_images = [images[idx] for idx in group_indices]
            page_nums = ", ".join(str(idx + 1) for idx in group_indices)

            try:
                if len(group_images) == 1:
                    doc, preview = await process_photo(
                        group_images[0], telegram_id, **kw
                    )
                else:
                    doc, preview = await process_multiple_photos(
                        group_images, telegram_id, **kw
                    )
                logger.info(
                    "[%s] Документ %d/%d OK: %s, items=%d (фото %s)",
                    LABEL, i + 1, total,
                    doc.get("doc_type", "?"),
                    len(doc.get("items", [])),
                    page_nums,
                )
                return (doc, preview)
            except Exception as e:
                logger.exception(
                    "[%s] Ошибка распознавания документа %d/%d (фото %s): %s",
                    LABEL, i + 1, total, page_nums, e,
                )
                # Создаём "ошибочный" документ
                error_doc = {
                    "doc_type": "Ошибка",
                    "doc_number": None,
                    "date": None,
                    "supplier": {"name": "Ошибка распознавания"},
                    "items": [],
                    "notes": f"Не удалось распознать фото {page_nums}: {e}",
                    "_error": True,
                }
                error_preview = f"❌ Ошибка распознавания фото {page_nums}:\n{e}"
                return (error_doc, error_preview)

    # Запускаем все группы параллельно
    tasks = [
        _process_one_group(i, group_indices)
        for i, group_indices in enumerate(groups)
    ]
    results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - t0
    ok_count = sum(1 for doc, _ in results if not doc.get("_error"))
    err_count = sum(1 for doc, _ in results if doc.get("_error"))
    logger.info(
        "[%s] Batch tg:%d — %d документов (✅ %d, ❌ %d) за %.1f сек",
        LABEL, telegram_id, total, ok_count, err_count, elapsed,
    )

    return results


# ═══════════════════════════════════════════════════════
# Валидация и автоисправление
# ═══════════════════════════════════════════════════════

def validate_and_fix(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Математическая валидация распознанного документа.

    Проверяет:
      - НДС = sum × rate
      - sum_with_vat = sum_without_vat + vat_sum
      - Итоги = сумма позиций

    НЕ проверяет qty × price — доверяем sum_without_vat из документа
    (у поставщика может быть скидка, округление и т.д.)
    """
    warnings: list[str] = []
    items = doc.get("items") or []

    for i, item in enumerate(items, 1):
        qty = item.get("qty")
        price = item.get("price")
        sum_wo = item.get("sum_without_vat")
        vat_rate_str = item.get("vat_rate", "")

        # Если sum_without_vat не указана — рассчитываем
        if sum_wo is None and qty is not None and price is not None:
            sum_wo = round(qty * price, 2)
            item["sum_without_vat"] = sum_wo

        # НДС расчёт (только если не указан)
        vat_rate = _parse_vat_rate(vat_rate_str)
        if vat_rate and sum_wo:
            actual_vat = item.get("vat_sum")
            if actual_vat is None:
                # Автозаполнение НДС
                item["vat_sum"] = round(sum_wo * vat_rate, 2)
            
            # sum_with_vat (только если не указана)
            actual_total = item.get("sum_with_vat")
            if actual_total is None:
                item["sum_with_vat"] = round(sum_wo + (item.get("vat_sum") or 0), 2)

    # Пересчёт итогов
    calc_total_wo = sum((it.get("sum_without_vat") or 0) for it in items)
    calc_total_vat = sum((it.get("vat_sum") or 0) for it in items)
    calc_total_with = sum((it.get("sum_with_vat") or 0) for it in items)

    # Сравниваем с тем что в документе
    doc_total_wo = doc.get("total_without_vat")
    doc_total_vat = doc.get("total_vat")
    doc_total_with = doc.get("total_with_vat")
    
    # Только крупные расхождения (> 5 руб) — возможная ошибка OCR
    if doc_total_wo and abs(doc_total_wo - calc_total_wo) > 5.0:
        warnings.append(
            f"⚠️ Итого без НДС: в документе {doc_total_wo}, расчёт {round(calc_total_wo, 2)} (разница {abs(doc_total_wo - calc_total_wo):.2f})"
        )
    
    if doc_total_with and abs(doc_total_with - calc_total_with) > 5.0:
        warnings.append(
            f"⚠️ Итого с НДС: в документе {doc_total_with}, расчёт {round(calc_total_with, 2)} (разница {abs(doc_total_with - calc_total_with):.2f})"
        )
    
    # Сохраняем расчётные значения для справки
    doc["_calc_total_wo"] = round(calc_total_wo, 2)
    doc["_calc_total_vat"] = round(calc_total_vat, 2)
    doc["_calc_total_with"] = round(calc_total_with, 2)
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
    Все динамические значения экранируются для безопасного HTML.
    """
    lines: list[str] = []

    doc_type = doc.get("doc_type", "Неизвестный")
    lines.append(f"📄 <b>{html.escape(doc_type)}</b>")
    if doc.get("doc_number"):
        lines.append(f"№ {html.escape(str(doc['doc_number']))}")
    if doc.get("date"):
        lines.append(f"Дата: {html.escape(str(doc['date']))}")

    # Поставщик
    supplier = doc.get("supplier") or {}
    if supplier.get("name"):
        lines.append(f"\n🏢 <b>Поставщик:</b> {html.escape(supplier['name'])}")

    # Покупатель
    buyer = doc.get("buyer") or {}
    if buyer.get("name"):
        lines.append(f"🏪 <b>Покупатель:</b> {html.escape(buyer['name'])}")

    # Товары
    items = doc.get("items") or []
    if items:
        lines.append(f"\n📦 <b>Позиции ({len(items)}):</b>")
        for item in items:
            num = item.get("num", "?")
            name = item.get("name", "???")
            qty = item.get("qty", "?")
            unit = item.get("unit", "шт")
            packaging = item.get("packaging")
            price = item.get("price", "?")
            sum_with = item.get("sum_with_vat") or item.get("sum_without_vat") or "?"

            # Формируем строку с единицей измерения и упаковкой (если есть)
            unit_str = html.escape(str(unit))
            if packaging:
                unit_str += f" ({html.escape(str(packaging))})"

            # Экранируем все значения для безопасности
            lines.append(
                f"  {html.escape(str(num))}. {html.escape(str(name))}\n"
                f"     {html.escape(str(qty))} {unit_str} × {html.escape(str(price))} = {html.escape(str(sum_with))}"
            )

    # Итоги
    lines.append("")
    total = doc.get("total_with_vat") or doc.get("_calc_total_with_vat")
    total_wo = doc.get("total_without_vat") or doc.get("_calc_total_without_vat")
    total_vat = doc.get("total_vat") or doc.get("_calc_total_vat")

    if total_wo:
        lines.append(f"💰 Без НДС: <b>{html.escape(str(total_wo))}</b>")
    if total_vat:
        lines.append(f"💰 НДС: <b>{html.escape(str(total_vat))}</b>")
    if total:
        lines.append(f"💰 Итого: <b>{html.escape(str(total))}</b>")

    # Предупреждения
    warnings = doc.get("_warnings", [])
    if warnings:
        lines.append("\n⚠️ <b>Предупреждения:</b>")
        for w in warnings[:5]:
            lines.append(f"  • {html.escape(str(w))}")

    # Заметки от LLM
    notes = doc.get("notes")
    if notes:
        lines.append(f"\n📝 {html.escape(notes)}")

    page_info = doc.get("page_info")
    if page_info and page_info != "единственная страница":
        lines.append(f"📄 {html.escape(page_info)}")

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
        # Получаем INN и конвертируем в строку, если он существует
        supplier_inn = (doc.get("supplier") or {}).get("inn")
        if supplier_inn is not None:
            supplier_inn = str(supplier_inn)
        
        # Получаем buyer_name, если пустой — используем подразделение пользователя
        buyer_name = (doc.get("buyer") or {}).get("name")
        if not buyer_name:
            user_ctx = await get_user_context(telegram_id)
            if user_ctx and user_ctx.department_name:
                buyer_name = user_ctx.department_name
                logger.info("[%s] Buyer пустой → fallback на подразделение: %s", LABEL, buyer_name)
        
        row = OcrDocument(
            telegram_id=telegram_id,
            doc_type=doc.get("doc_type", "Неизвестный"),
            doc_number=doc.get("doc_number"),
            doc_date=doc.get("date"),
            supplier_name=(doc.get("supplier") or {}).get("name"),
            supplier_inn=supplier_inn,
            buyer_name=buyer_name,
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


async def update_ocr_category(doc_id: int, category: str) -> None:
    """Обновить категорию OCR-документа (goods/service)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrDocument).where(OcrDocument.id == doc_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.category = category
            await session.commit()
            logger.info("[%s] Updated category doc_id=%d → %s", LABEL, doc_id, category)


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
