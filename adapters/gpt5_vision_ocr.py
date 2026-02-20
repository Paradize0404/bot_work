"""
GPT-5.2 Vision OCR — прямое распознавание документов через GPT-5.2 Vision.

Использует OpenAI GPT-5.2 Vision API для распознавания российских
бухгалтерских документов (УПД, чеки, акты, ордера) напрямую из изображений.
"""

import io
import json
import re
import base64
import logging
from typing import Dict, Any
from openai import AsyncOpenAI
from PIL import Image, ImageOps
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# Модель GPT-5.2
OCR_MODEL = "gpt-5.2-chat-latest"

# Singleton клиент — переиспользуем HTTP-соединения
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Получить singleton AsyncOpenAI клиент."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


async def close_client() -> None:
    """Закрыть HTTP-клиент при shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("OpenAI client closed")


# Системный промпт для распознавания документов
SYSTEM_PROMPT = """
Ты — эксперт по российским бухгалтерским документам.

ЗАДАЧА:
Распознай этот документ и извлеки ВСЕ данные в структурированном JSON.

ТИПЫ ДОКУМЕНТОВ:
- upd — Универсальный передаточный документ (УПД, счёт-фактура)
- receipt — Кассовый чек (фискальный, с QR-кодом)
- act — Акт выполненных работ/услуг
- cash_order — Расходный кассовый ордер (форма КО-2)
- other — Другой документ

ВАЖНО ПРО QR-КОД:
- Осмотри изображение внимательно
- QR-код обычно находится в нижней части чека (квадратный штрих-код)
- Все кассовые чеки в России имеют QR-код (требование 54-ФЗ)
- УПД, акты, ордера обычно НЕ имеют QR-кода (но могут иметь QR для верификации)

ВАЖНО ПРО МНОГОСТРАНИЧНОСТЬ:
- Ищи признаки многостраничного документа:
  
  **Явные признаки:**
  - "Лист X" или "Лист X из Y" вверху справа
  - "Документ составлен на Y листах" слева внизу
  - "Страница X из Y"
  
  **Косвенные признаки (Страница 2+):**
  - Вверху только номер документа (нет продавца/покупателя)
  - Таблица с товарами без полной шапки
  - Подписи и печати внизу
  
  **Признаки Страницы 1:**
  - Полная шапка с продавцом и покупателем
  - Нет "Лист 2" или "Лист 3" вверху
  - Дата и номер документа в начале
  
- Если найден "Документ составлен на Y листах" → total_pages = Y
- Если найден "Лист X" → page_number = X
- Если нет явных признаков → page_number = 1, total_pages = 1
- is_multistage = true если total_pages > 1

**ВАЖНО:**
- Если видишь "Документ составлен на Y листах" но не видишь "Лист X" → это Страница 1
  - page_number = 1, total_pages = Y, is_multistage = true
- Если видишь "Лист X" но не видишь "Документ составлен на Y листах" → 
  - page_number = X, total_pages = X (предполагаем что это последняя)

**КЛЮЧ ДЛЯ ГРУППИРОВКИ:**
- Для многостраничных документов создай group_key:
  - group_key = "{supplier_inn}_{doc_number}_{date}"
  - Пример: "422373281150_УТ-41076_2025-11-28"
- Этот ключ одинаковый для всех страниц одного документа

ИЗВЛЕКИ ПОЛЯ:

1. Для ВСЕХ документов:
   - doc_type: тип документа (upd/receipt/act/cash_order/other)
   - has_qr: true/false (есть ли QR-код на изображении)
   - doc_number: номер документа
   - date: дата в формате YYYY-MM-DD
   - supplier: { name, inn } — ТОЛЬКО имя и ИНН (без адреса)
   - buyer: { name, inn } — ТОЛЬКО имя и ИНН (без адреса, null если нет)
   - total_amount: итоговая сумма (числом)
   - page_number: номер текущей страницы (1, 2, 3...)
   - total_pages: общее количество страниц (1 если одностраничный)
   - is_multistage: true если total_pages > 1

2. Для документов с товарами (upd, receipt, act):
   - items: массив [{
       name: наименование (ТОЧНО как в документе),
       unit: ед.изм. (кг, шт, л),
       qty: количество (числом),
       price: цена за единицу БЕЗ НДС (числом) — столбец "Цена" или "Цена без НДС",
       sum: СТОИМОСТЬ С УЧЁТОМ НДС (числом) — ПОСЛЕДНИЙ числовой столбец таблицы
            ("Стоимость товаров с налогом" / "Итоговая стоимость с НДС").
            НЕ путать с "Стоимостью без НДС"! Берётся именно финальная сумма по строке.
       vat_rate: ставка НДС ("10%", "20%", "5%", "без НДС", null)
     }]

3. Для кассового чека (receipt):
   - fiscal_sign: фискальный признак (ФПД)
   - kkt_number: номер ККТ
   - shift_number: номер смены

4. Для расходного кассового ордера (cash_order):
   - recipient: кому выдать (поле "Выдать" — рукописное ФИО или имя)
   - purpose: основание/назначение (поле "Основание" или "Назначение" — рукописный текст)
   Рукописный текст читай максимально точно, даже если кажется неразборчивым.

СТОП-ГАЛЛЮЦИНАЦИИ (КРИТИЧЕСКИ ВАЖНО):
- НИКОГДА не добавляй товары, которых нет в таблице — считай строки визуально, items в JSON = строки в таблице
- НИКОГДА не заменяй слово на похожее по смыслу: если написано "Фундук" — пиши "Фундук", не "Ваниль"; если "Миндаль" — пиши "Миндаль", не "Шоколад"
- НИКОГДА не изменяй имена поставщиков/покупателей фонетически: копируй букву за буквой
- Если слово трудно прочитать — пиши то, что видишь, даже если выглядит необычно. НЕ заменяй на "похожее"
- "HORECA" (HoReCa) — стандартный отраслевой термин (Hotels, Restaurants, Cafes), встречается в названиях товаров. Пиши именно "HORECA"
- "Green Milk" — название торговой марки, пиши точно
- Перед записью items: мысленно пересчитай строки в таблице (не считая заголовок и "Итого"). Убедись, что количество объектов в items равно количеству строк с товарами

ВАЖНО:
- Цифры пиши ЧИСЛАМИ (не строками): 1234.56 а не "1 234.56"
- Разделитель дробной части: ТОЧКА (не запятая)
- Даты: ТОЛЬКО YYYY-MM-DD
- Названия товаров: ТОЧНО как в документе (не сокращай, не перефразируй)
- Если поле совсем не читается: null (не выдумывай замену)
- Рукописный текст: распознавай максимально точно, если неразборчиво — пиши символы которые видишь
- supplier и buyer: ТОЛЬКО name и inn (адрес не нужен)
- buyer может отсутствовать (например, в чеках) — тогда null
- page_number и total_pages: определяй по тексту "Лист X из Y" или "Составлен на Y листах"

ПРОВЕРКА:
- qty × price ≈ стоимость БЕЗ НДС (не поле sum!)
- sum = стоимость С НДС (последний столбец) = qty × price × (1 + ставка НДС)
- Сумма всех items[sum] должна равняться total_amount (допуск ±5)

ФОРМАТ ОТВЕТА:
ТОЛЬКО JSON (без markdown, без обратных кавычек):
{
  "doc_type": "...",
  "has_qr": true/false,
  "doc_number": "...",
  "date": "YYYY-MM-DD",
  "supplier": {"name": "...", "inn": "..."},
  "buyer": {"name": "...", "inn": "..."} или null,
  "recipient": null,
  "purpose": null,
  "items": [...],
  "total_amount": 0.00,
  "page_number": 1,
  "total_pages": 1,
  "is_multistage": false,
  "group_key": "supplier_inn_doc-number_date",
  "quality_check": {
    "confidence_score": 85,
    "warnings": ["рукописный текст", "печать закрывает текст"],
    "missing_pages_warning": false
  }
}
"""


def _auto_rotate(image_bytes: bytes) -> bytes:
    """
    Исправить ориентацию изображения по EXIF-тегу.
    Телефоны часто сохраняют фото повёрнутыми с EXIF-пометкой о повороте —
    exif_transpose() физически поворачивает пиксели и убирает тег.
    Горизонтальные документы НЕ трогаем: А4 может быть сфотографирован
    в ландшафтной ориентации — это нормально.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)   # убираем EXIF-поворот, если есть
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception:
        return image_bytes                   # при ошибке возвращаем исходные байты


async def recognize_document(image_bytes: bytes) -> Dict[str, Any]:
    """
    Распознать документ через GPT-5.2 Vision.
    
    Args:
        image_bytes: Изображение в формате JPEG/PNG
    
    Returns:
        Словарь с распознанными данными
    """
    client = _get_client()

    # Авто-поворот: коррекция EXIF + горизонтальные → портрет
    image_bytes = _auto_rotate(image_bytes)

    # Кодируем изображение в base64
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    # Вызываем GPT-5.2 Vision
    response = await client.chat.completions.create(
        model=OCR_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Распознай этот документ и извлеки все данные в JSON.\n"
                            "ОБЯЗАТЕЛЬНО:\n"
                            "1. Сначала посчитай строки с товарами в таблице (не считая заголовок и 'Итого').\n"
                            "2. Пиши названия товаров и имена ТОЧНО как видишь — букву за буквой.\n"
                            "3. НЕ заменяй непонятные слова похожими по смыслу — если не уверен, пиши то что видишь.\n"
                            "4. Количество items в JSON = количество строк в таблице (не больше, не меньше)."
                        )
                    },
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
        max_completion_tokens=4096  # GPT-5.2 использует этот параметр
        # temperature не поддерживается (только 1 по умолчанию)
    )
    
    # Парсим ответ
    content = response.choices[0].message.content.strip()
    
    # Извлекаем JSON (если есть markdown-обёртка)
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        content = json_match.group()

    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("[gpt5_ocr] GPT вернул невалидный JSON: %s … (%s)", content[:200], exc)
        return {
            "error": "Не удалось распознать документ (невалидный JSON от GPT)",
            "_raw_response": content,
            "_model": OCR_MODEL,
        }
    
    # Добавляем метаинформацию
    result['_raw_response'] = content
    result['_model'] = OCR_MODEL
    result['_usage'] = {
        'prompt_tokens': response.usage.prompt_tokens,
        'completion_tokens': response.usage.completion_tokens,
        'total_tokens': response.usage.total_tokens,
    }
    
    return result
