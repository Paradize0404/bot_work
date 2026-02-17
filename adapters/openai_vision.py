"""
Адаптер OpenAI GPT-4o Vision для OCR российских бухгалтерских документов.

Фото → GPT-4o → структурированный JSON.
"""

import base64
import json
import logging
import re
import time
from typing import Any

from openai import AsyncOpenAI
from PIL import Image, ImageEnhance
import io

from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)
LABEL = "GPT4o-OCR"

# OpenAI GPT-4o (paid tier) - 10,000 RPM, no daily limits
GPT_MODEL = "gpt-4o"


# ═══════════════════════════════════════════════════════
# Инициализация клиента
# ═══════════════════════════════════════════════════════

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Ленивая инициализация — создаём клиента при первом вызове."""
    global _client
    if _client is not None:
        return _client
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан — OCR невозможен")
    _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


# ═══════════════════════════════════════════════════════
# Системный промпт для OCR
# ═══════════════════════════════════════════════════════

_SYSTEM_PROMPT = """Ты — высокоточный OCR-движок для российских бухгалтерских документов.

ЗАДАЧА: Распознать фото документа и вернуть структурированный JSON.

ТИПЫ ДОКУМЕНТОВ:
- УПД (Универсальный передаточный документ / счёт-фактура)
- Кассовый чек
- РКО (Расходный кассовый ордер)
- Акт выполненных работ/услуг
- Товарный чек
- Неизвестный (если не можешь определить тип)

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Цифры — ЧИСЛАМИ (float/int), не строками. Исключение vat_rate ("20%", "10%", "без НДС")
2. НИКОГДА не используй разделители тысяч: 73221.82 а не 73,221.82 или 73_221.82
3. Если не можешь прочитать значение — ставь null
4. Если таблица обрезана или недостаёт листа — укажи в notes и в page_info
5. Названия товаров пиши ТОЧНО как на фото в поле "name"
6. В поле "name_normalized" — стандартное (нормализованное) название товара
7. Формат ответа — СТРОГО JSON, без markdown, без ```json, без пояснений
8. Единицы измерения: шт, кг, л, уп, м, м2, м3, п.м — как на документе

⚠️ ПРОВЕРКА КАЧЕСТВА ФОТО (ОБЯЗАТЕЛЬНО):
- Оцени качество фото перед распознаванием
- Проверь наличие бликов, размытости, обрезанных краёв
- Оцени свою уверенность в правильности распознавания (0-100%)
- Если уверенность < 70% ИЛИ есть критические проблемы → укажи needs_retake: true
- Критические проблемы: блики закрывают цены/суммы, размыто так что не прочитать позиции, обрезаны важные части
- Незначительные проблемы (лёгкая размытость, небольшой наклон) → можно распознать, needs_retake: false
- ДЛЯ НЕСКОЛЬКИХ ФОТО: в "issues" указывай НОМЕР ФОТО с проблемой (Фото 3: блики на суммах, Фото 7: размыто)

{supplier_hint}
{buyer_hint}

ФОРМАТ JSON:
{{
  "quality_check": {{
    "is_readable": true,
    "has_glare": false,
    "has_blur": false,
    "is_complete": true,
    "confidence_score": 95,
    "issues": ["Фото 3: блик на позициях 5-7", "Фото 7: размыто в нижней части"],
    "needs_retake": false,
    "retake_reason": "Укажи ЧТО ИМЕННО нужно переснять если needs_retake=true"
  }},
  "doc_type": "УПД | Кассовый чек | РКО | Акт | Товарный чек | Неизвестный",
  "doc_number": "номер документа или null",
  "date": "ДД.ММ.ГГГГ или null",
  "supplier": {{
    "name": "название поставщика/продавца",
    "inn": "ИНН или null",
    "kpp": "КПП или null",
    "address": "адрес или null"
  }},
  "buyer": {{
    "name": "название покупателя/получателя",
    "inn": "ИНН или null"
  }},
  "items": [
    {{
      "num": 1,
      "name": "Наименование товара/услуги (КАК НА ФОТО)",
      "name_normalized": "Стандартное название товара",
      "unit": "шт/кг/л/уп",
      "qty": 1.0,
      "price": 100.00,
      "sum_without_vat": 100.00,
      "vat_rate": "20%/10%/без НДС",
      "vat_sum": 20.00,
      "sum_with_vat": 120.00
    }}
  ],
  "total_without_vat": 0.00,
  "total_vat": 0.00,
  "total_with_vat": 0.00,
  "payment_method": "наличный/безналичный/null",
  "page_info": "Лист 1 из 2 / null / единственная страница",
  "notes": "любые дополнительные наблюдения, предупреждения"
}}
"""


def _build_prompt(
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
) -> str:
    """Собрать промпт с подсказками по известным поставщикам/покупателям."""
    supplier_hint = ""
    if known_suppliers:
        items = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(known_suppliers))
        supplier_hint = (
            f"ПОСТАВЩИК — одно из этих названий:\n{items}\n"
            "Определи по фото какое из них. Если не подходит ни одно — верни как на фото."
        )

    buyer_hint = ""
    if known_buyers:
        items = "\n".join(f"  {i+1}. {b}" for i, b in enumerate(known_buyers))
        buyer_hint = (
            f"ПОКУПАТЕЛЬ — одно из этих заведений:\n{items}\n"
            "Определи по фото какое из них. Если не можешь — верни null."
        )

    return _SYSTEM_PROMPT.format(
        supplier_hint=supplier_hint,
        buyer_hint=buyer_hint,
    )


# ═══════════════════════════════════════════════════════
# Предобработка изображения
# ═══════════════════════════════════════════════════════

def preprocess_image(image_bytes: bytes) -> bytes:
    """
    Предобработка фото для улучшения OCR:
    - Конвертация в RGB
    - Увеличение контраста
    - Увеличение резкости
    Возвращает JPEG bytes.
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Конвертируем в RGB (убираем альфа-канал если есть)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Увеличиваем контраст (×1.5)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)

    # Увеличиваем резкость (×2.0)
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


# ═══════════════════════════════════════════════════════
# Основная функция OCR
# ═══════════════════════════════════════════════════════

def _clean_json_response(text: str) -> str:
    """Убрать markdown-обёртку если LLM вернул ```json...```."""
    text = text.strip()
    # Убираем ```json ... ``` или ``` ... ```
    if text.startswith("```"):
        # Находим первую и последнюю ```
        first_newline = text.find("\n")
        if first_newline > 0:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _fix_number_separators(text: str) -> str:
    """Исправить разделители тысяч: 73,221.82 → 73221.82, 1_144.46 → 1144.46."""
    # Pattern: digit, then comma/underscore, then exactly 3 digits, then dot or end
    text = re.sub(r'(?<=\d)[,_](?=\d{3}(?:\.\d|\b))', '', text)
    return text


async def recognize_document(
    image_bytes: bytes,
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
    preprocess: bool = True,
) -> dict[str, Any]:
    """
    Основная функция: фото → структурированный JSON.

    Args:
        image_bytes: байты фото (JPEG/PNG)
        known_suppliers: список известных поставщиков для подсказки
        known_buyers: список известных покупателей для подсказки
        preprocess: предобработка изображения (контраст, резкость)

    Returns:
        dict — распознанный документ (формат из промпта)

    Raises:
        ValueError: если GPT вернул не JSON
        RuntimeError: если OPENAI_API_KEY не задан
    """
    client = _get_client()

    t0 = time.monotonic()

    # Предобработка
    if preprocess:
        image_bytes = preprocess_image(image_bytes)

    # Собираем промпт
    prompt = _build_prompt(
        known_suppliers=known_suppliers,
        known_buyers=known_buyers,
    )

    # Конвертируем в base64
    base64_image = base64.b64encode(image_bytes).decode('utf-8')

    # Отправляем фото + промпт
    logger.info("[%s] Отправляю фото в GPT-4o, size=%d bytes", LABEL, len(image_bytes))

    response = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "high"
                        }
                    }
                ]
            }
        ],
        temperature=0.1,
        max_tokens=4096,
    )

    elapsed = time.monotonic() - t0
    raw_text = response.choices[0].message.content or ""
    logger.info("[%s] Ответ GPT-4o за %.1f сек, len=%d", LABEL, elapsed, len(raw_text))
    logger.debug("[%s] Raw response:\n%s", LABEL, raw_text[:2000])

    # Парсим JSON
    clean_text = _clean_json_response(raw_text)
    clean_text = _fix_number_separators(clean_text)

    try:
        result = json.loads(clean_text)
    except json.JSONDecodeError as e:
        logger.error("[%s] GPT-4o вернул не JSON: %s\nRaw:\n%s", LABEL, e, clean_text[:1000])
        raise ValueError(f"GPT-4o вернул не JSON: {e}") from e

    logger.info(
        "[%s] Распознано: doc_type=%s, items=%d, supplier=%s",
        LABEL,
        result.get("doc_type"),
        len(result.get("items", [])),
        result.get("supplier", {}).get("name", "?"),
    )

    return result


async def recognize_multiple_pages(
    images: list[bytes],
    *,
    known_suppliers: list[str] | None = None,
    known_buyers: list[str] | None = None,
) -> dict[str, Any]:
    """
    Распознать многостраничный документ (несколько фото).

    Отправляем все фото в одном запросе — LLM сам объединит.
    """
    client = _get_client()

    t0 = time.monotonic()

    prompt = _build_prompt(
        known_suppliers=known_suppliers,
        known_buyers=known_buyers,
    )
    prompt += (
        "\n\nЭто МНОГОСТРАНИЧНЫЙ документ. Фото идут по порядку."
        " Объедини все страницы в один JSON. "
        "Если нумерация продолжается — объедини items."
        " В page_info укажи сколько страниц распознано."
    )

    # Формируем content с несколькими изображениями
    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img_bytes in images:
        processed = preprocess_image(img_bytes)
        base64_image = base64.b64encode(processed).decode('utf-8')
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{base64_image}",
                "detail": "high"
            }
        })

    logger.info("[%s] Отправляю %d фото в GPT-4o (multi-page)", LABEL, len(images))

    response = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=[{"role": "user", "content": content_parts}],
        temperature=0.1,
        max_tokens=8192,  # больше токенов для multi-page
    )

    elapsed = time.monotonic() - t0
    raw_text = response.choices[0].message.content or ""
    logger.info("[%s] Multi-page ответ за %.1f сек", LABEL, elapsed)

    clean_text = _clean_json_response(raw_text)
    clean_text = _fix_number_separators(clean_text)

    try:
        result = json.loads(clean_text)
    except json.JSONDecodeError as e:
        logger.error("[%s] GPT-4o multi-page не JSON: %s", LABEL, e)
        raise ValueError(f"GPT-4o вернул не JSON: {e}") from e

    return result


# ═══════════════════════════════════════════════════════
# Извлечение метаданных документа (для группировки)
# ═══════════════════════════════════════════════════════

_METADATA_PROMPT = """Ты — OCR-система для определения метаданных документа.

ЗАДАЧА: Быстро извлечь ТОЛЬКО ключевые метаданные документа, БЕЗ полного распознавания таблицы товаров.

ВЕРНИ ТОЛЬКО:
{{
  "doc_number": "номер документа или null",
  "date": "ДД.ММ.ГГГГ или null",
  "supplier_name": "название поставщика/продавца",
  "supplier_inn": "ИНН поставщика или null",
  "total_amount": 0.00,
  "has_total": true,
  "page_number": 1,
  "total_pages": 2
}}

ПРАВИЛА:
- has_total: true если на странице есть ИТОГОВАЯ СУММА документа (последняя страница)
- page_number: номер страницы (если указан на документе, иначе null)
- total_pages: общее кол-во страниц (если указано "Лист 2 из 3", иначе null)
- Если номер документа не найден — попробуй из штрих-кода, QR, или заголовка
- Формат ответа — СТРОГО JSON, без markdown, без ```json, без пояснений
"""


async def extract_document_metadata(image_bytes: bytes) -> dict[str, Any]:
    """
    Быстрое извлечение метаданных документа для группировки.
    
    Используется чтобы определить: это разные документы или страницы одного?
    Гораздо быстрее и дешевле чем полное распознавание.
    
    Returns:
        {
            "doc_number": str | None,
            "date": str | None,  # ДД.ММ.ГГГГ
            "supplier_name": str,
            "supplier_inn": str | None,
            "total_amount": float,
            "has_total": bool,  # есть ли итоговая сумма (признак последней страницы)
            "page_number": int | None,
            "total_pages": int | None
        }
    """
    client = _get_client()
    
    t0 = time.monotonic()
    
    # Предобработка
    processed = preprocess_image(image_bytes)
    base64_image = base64.b64encode(processed).decode('utf-8')
    
    logger.info("[%s] Извлекаю метаданные (metadata-only)", LABEL)
    
    response = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _METADATA_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "low"  # low detail для metadata - быстрее и дешевле
                        }
                    }
                ]
            }
        ],
        temperature=0.0,
        max_tokens=512,
    )
    
    elapsed = time.monotonic() - t0
    raw_text = response.choices[0].message.content or ""
    logger.debug("[%s] Metadata за %.1f сек: %s", LABEL, elapsed, raw_text[:200])
    
    clean_text = _clean_json_response(raw_text)
    clean_text = _fix_number_separators(clean_text)
    
    try:
        result = json.loads(clean_text)
    except json.JSONDecodeError as e:
        logger.warning("[%s] Metadata не JSON, возвращаю fallback: %s", LABEL, e)
        # Fallback: считаем что это отдельный документ
        return {
            "doc_number": None,
            "date": None,
            "supplier_name": "Неизвестно",
            "supplier_inn": None,
            "total_amount": 0.0,
            "has_total": True,  # считаем что это целый документ
            "page_number": None,
            "total_pages": None,
        }
    
    logger.info(
        "[%s] Metadata: doc=%s, date=%s, supplier=%s (%.2f₽)",
        LABEL,
        result.get("doc_number"),
        result.get("date"),
        result.get("supplier_name"),
        result.get("total_amount", 0),
    )
    
    return result
