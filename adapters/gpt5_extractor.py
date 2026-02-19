"""
GPT-5.2 Extractor — извлечение полей из текста документа.

Использует OpenAI GPT-5.2 для извлечения структурированных данных
из распознанного текста российских бухгалтерских документов.
"""

import json
import re
from typing import Dict, Any
from openai import AsyncOpenAI
from config import OPENAI_API_KEY


# Модель GPT-5.2
OCR_MODEL = "gpt-5.2-chat-latest"


# Системный промпт для извлечения полей
SYSTEM_PROMPT = """
Ты — эксперт по российским бухгалтерским документам.
Твоя задача: извлечь структурированные данные из текста документа.

Типы документов:
- upd — Универсальный передаточный документ (счёт-фактура + накладная)
- receipt — Кассовый чек
- act — Акт выполненных работ/услуг
- cash_order — Расходный/приходный кассовый ордер

Извлеки поля в формате JSON:
{
  "doc_type": "upd|receipt|act|cash_order",
  "supplier": {"name": "...", "inn": "..."},
  "buyer": {"name": "...", "inn": "..."},
  "date": "YYYY-MM-DD",
  "doc_number": "...",
  "items": [
    {
      "name": "...",
      "unit": "кг|шт|л",
      "qty": 0.0,
      "price": 0.0,
      "sum": 0.0,
      "vat_rate": "10%|20%|без НДС"
    }
  ],
  "total_amount": 0.0,
  "total_vat": 0.0,
  "currency": "RUB",
  "quality_check": {
    "confidence_score": 0-100,
    "warnings": ["..."]
  }
}

Важно:
- Цифры — числами (не строками)
- Даты в формате YYYY-MM-DD
- Суммы с точностью до 2 знаков
- Если не можешь прочитать поле — укажи null
- Названия товаров — точно как в документе
"""


async def extract_fields(text: str, doc_type: str = "unknown") -> Dict[str, Any]:
    """
    Извлечь поля из текста документа через GPT-5.2.
    
    Args:
        text: Распознанный текст документа
        doc_type: Тип документа (если известен заранее)
    
    Returns:
        Словарь с извлечёнными полями
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    
    # Формируем промпт
    if doc_type == "unknown":
        prompt = f"""
{SYSTEM_PROMPT}

Текст документа:
{text}
"""
    else:
        prompt = f"""
{SYSTEM_PROMPT}

Тип документа: {doc_type}

Текст документа:
{text}
"""
    
    # Вызываем GPT-5.2
    response = await client.chat.completions.create(
        model=OCR_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        max_tokens=4096,
        temperature=0.1  # Минимальная случайность для точности
    )
    
    # Парсим ответ
    content = response.choices[0].message.content.strip()
    
    # Извлекаем JSON (если есть markdown-обёртка)
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        content = json_match.group()
    
    result = json.loads(content)
    
    # Добавляем метаинформацию
    result['_raw_response'] = content
    result['_model'] = OCR_MODEL
    result['_usage'] = {
        'prompt_tokens': response.usage.prompt_tokens,
        'completion_tokens': response.usage.completion_tokens,
        'total_tokens': response.usage.total_tokens,
    }
    
    return result


async def classify_document(text: str) -> str:
    """
    Классифицировать тип документа по тексту.
    
    Args:
        text: Распознанный текст документа
    
    Returns:
        Тип документа: 'upd', 'receipt', 'act', 'cash_order', 'unknown'
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    
    prompt = """
Классифицируй тип российского бухгалтерского документа.

Варианты:
- upd — Универсальный передаточный документ (есть таблица с товарами, подписи, печати)
- receipt — Кассовый чек (длинная узкая лента, фискальные данные, QR-код)
- act — Акт выполненных работ/услуг (таблица с услугами, подписи)
- cash_order — Кассовый ордер (форма КО-2, рукописные данные)
- unknown — Не удалось определить

Верни ТОЛЬКО одно слово (upd/receipt/act/cash_order/unknown).

Текст документа:
{text[:2000]}
""".format(text=text)
    
    response = await client.chat.completions.create(
        model=OCR_MODEL,
        messages=[
            {"role": "user", "content": prompt}
        ],
        max_tokens=10
    )
    
    doc_type = response.choices[0].message.content.strip().lower()
    
    # Валидация
    valid_types = ['upd', 'receipt', 'act', 'cash_order', 'unknown']
    if doc_type not in valid_types:
        doc_type = 'unknown'
    
    return doc_type
