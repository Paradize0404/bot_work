# 📸 План реализации OCR: Гибридное решение (Yandex + GPT-5.2)

> Версия: 1.0  
> Дата: 2026-02-18  
> Статус: **На согласовании**

---

## 🎯 Цель

Создать **надёжную систему распознавания бухгалтерских документов** с минимальным участием человека и автоматическим обучением.

**Принцип:** Пользователь просто кидает фото → система сама разбирается.

---

## 🏗 Архитектура (Гибрид Yandex + GPT-5.2)

```
┌─────────────────┐
│ Фото от юзера   │
│ (Telegram)      │
└────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  1. Проверка качества           │
│     • Размытость (OpenCV)       │
│     • Освещение (OpenCV)        │
│     • Ориентация (OpenCV)       │
│     • QR-код (OpenCV/pyzbar)    │
└────────┬────────────────────────┘
         │
         ├─❌ Плохое качество → Вернуть фото с инструкцией
         ├─❌ QR-код обнаружен → Инструкция про ФНС
         │
         ▼
┌─────────────────────────────────┐
│  2. Классификация документа     │
│     GPT-5.2 (быстрый промпт)    │
│     Тип: УПД / чек / акт / ордер│
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  3. Yandex Vision OCR           │
│     Распознавание текста        │
│     (~₽0.03/страница)           │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  4. GPT-5.2                     │
│     Извлечение полей из текста  │
│     (~$0.002/документ)          │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  5. Валидация                   │
│     • qty × price = sum         │
│     • total = sum(items)        │
│     • Дата не в будущем         │
└────────┬────────────────────────┘
         │
         ├─❌ Ошибки → Повторный прогон GPT
         │
         ▼
┌─────────────────────────────────┐
│  6. Авто-маппинг                │
│     • ocr_mapping (≥85%) → авто │
│     • iiko_product (≥85%) → авто│
│     • GSheet → ручной           │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  7. Сохранение в БД             │
│     • ocr_document              │
│     • ocr_item                  │
│     • ocr_mapping (обучение)    │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│  8. Отправка пользователю       │
│     JSON + кнопки               │
│     [✅] [✏️] [❌]              │
└─────────────────────────────────┘
```

---

## 💰 Стоимость (гибрид vs чистый GPT)

| Объём | Чистый GPT-5.2 | Гибрид Yandex+GPT | Экономия |
|-------|----------------|-------------------|----------|
| 100 документов | ~$2.60 | ~$0.80 | **69%** |
| 1000 документов | ~$26 | ~$8 | **69%** |
| 10 000 документов | ~$260 | ~$80 | **69%** |

**Расчёт на 1 документ (~1 страница):**
- Yandex OCR: ~₽0.03 ≈ $0.0003
- GPT-5.2 (текст ~3K токенов): ~$0.005
- **Итого:** ~$0.0053 vs $0.026 (чистый GPT)

---

## 📁 Структура файлов

```
test/
├── adapters/
│   ├── yandex_ocr.py              # Yandex Vision OCR (текст)
│   └── gpt5_extractor.py          # GPT-5.2 (извлечение полей)
│
├── utils/
│   ├── photo_validator.py         # Проверка качества фото
│   ├── qr_detector.py             # Детекция QR-кодов
│   ├── document_classifier.py     # Тип документа (GPT-5.2)
│   └── multistage_detector.py     # Многостраничность
│
├── models/
│   └── ocr.py                     # SQLAlchemy модели
│       • OcrDocument              # ocr_document
│       • OcrItem                  # ocr_item
│       • OcrMapping               # ocr_mapping (автообучение)
│       • OcrSupplierMapping       # ocr_supplier_mapping
│       • OcrCorrectionLog         # ocr_correction_log
│       • OcrConfidenceStats       # ocr_confidence_stats
│
├── use_cases/
│   ├── ocr_processing.py          # Основной pipeline
│   ├── ocr_auto_mapping.py        # Авто-маппинг с обучением
│   ├── ocr_validation.py          # Валидация сумм, дат
│   ├── ocr_statistics.py          # Статистика и метрики
│   ├── ocr_to_iiko.py             # Отправка в iiko (XML)
│   └── ocr_gsheet_mapping.py      # GSheet маппинг (новые товары)
│
├── bot/
│   ├── document_handlers.py       # Приём фото → OCR → превью
│   └── ocr_review.py              # Исправление, повтор, история
│
├── db/
│   └── migrations_ocr.py          # Миграции БД
│
├── tests/
│   └── test_ocr.py                # Тест на photo_test
│
└── docs/
    └── OCR.md                     # Документация системы
```

---

## 🗄 База данных

### Таблицы (6 новых)

| № | Таблица | Назначение | Индексы |
|---|---------|------------|---------|
| **1** | `ocr_document` | Распознанные документы | `ix_ocr_document_tg`, `ix_ocr_document_status` |
| **2** | `ocr_item` | Товары из документов | `ix_ocr_item_doc_id` |
| **3** | `ocr_mapping` | **Автообучение**: raw_name → iiko_id (товары) | `ix_ocr_mapping_raw_trgm` (GIN trigram) |
| **4** | `ocr_supplier_mapping` | **Автообучение**: raw_name → iiko_id (поставщики) | `ix_ocr_supplier_mapping_raw_trgm` |
| **5** | `ocr_correction_log` | История исправлений | `ix_ocr_correction_doc_id` |
| **6** | `ocr_confidence_stats` | Статистика confidence | `ix_ocr_stats_date` |

### SQL миграции

```sql
-- 1. ocr_document
CREATE TABLE IF NOT EXISTS ocr_document (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT NOT NULL,
    user_id UUID,
    doc_type VARCHAR(20) NOT NULL,  -- 'upd', 'receipt', 'act', 'cash_order'
    doc_number VARCHAR(100),
    doc_date DATE,
    supplier_name TEXT,
    supplier_inn VARCHAR(20),
    supplier_id UUID,  -- ссылка на iiko_supplier
    buyer_name TEXT,
    buyer_inn VARCHAR(20),
    total_amount DECIMAL(15,2),
    total_vat DECIMAL(15,2),
    currency VARCHAR(3) DEFAULT 'RUB',
    status VARCHAR(30) NOT NULL DEFAULT 'recognized',
    category VARCHAR(20) DEFAULT 'goods',  -- 'goods' или 'service'
    raw_json JSONB NOT NULL,  -- сырой ответ GPT
    validated_json JSONB,  -- после валидации
    confidence_score REAL,  -- 0-100
    page_count INT DEFAULT 1,
    is_multistage BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE,
    sent_to_iiko_at TIMESTAMP WITH TIME ZONE,
    iiko_document_id UUID,
    _metadata JSONB  -- служебные данные
);

CREATE INDEX IF NOT EXISTS ix_ocr_document_tg ON ocr_document (telegram_id);
CREATE INDEX IF NOT EXISTS ix_ocr_document_status ON ocr_document (status);
CREATE INDEX IF NOT EXISTS ix_ocr_document_created ON ocr_document (created_at);

-- 2. ocr_item
CREATE TABLE IF NOT EXISTS ocr_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES ocr_document(id) ON DELETE CASCADE,
    num INT,
    raw_name TEXT NOT NULL,  -- как распознано
    name_normalized TEXT,  -- нормализованное
    product_id UUID,  -- ссылка на iiko_product
    unit VARCHAR(20),  -- кг, шт, л
    qty DECIMAL(15,3),
    price DECIMAL(15,2),
    sum DECIMAL(15,2),
    vat_rate VARCHAR(10),  -- '10%', '20%', 'без НДС'
    confidence_score REAL,
    is_auto_corrected BOOLEAN DEFAULT FALSE,
    mapping_status VARCHAR(20) DEFAULT 'pending',  -- 'auto', 'manual', 'pending'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ocr_item_doc_id ON ocr_item (document_id);
CREATE INDEX IF NOT EXISTS ix_ocr_item_raw_name ON ocr_item USING gin (raw_name gin_trgm_ops);

-- 3. ocr_mapping (автообучение товары)
CREATE TABLE IF NOT EXISTS ocr_mapping (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_name TEXT NOT NULL UNIQUE,  -- "Помидор свеж."
    corrected_name TEXT NOT NULL,  -- "Помидоры свежие"
    iiko_id UUID NOT NULL,
    iiko_type VARCHAR(20) NOT NULL,  -- 'product' | 'supplier'
    iiko_name TEXT NOT NULL,  -- название в iiko
    confidence REAL NOT NULL,  -- 0.85-1.0
    source VARCHAR(20) DEFAULT 'auto',  -- 'auto' | 'manual' | 'gsheet'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE,
    use_count INT DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_ocr_mapping_raw_trgm ON ocr_mapping USING gin (raw_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_ocr_mapping_iiko_id ON ocr_mapping (iiko_id);

-- 4. ocr_supplier_mapping (автообучение поставщики)
CREATE TABLE IF NOT EXISTS ocr_supplier_mapping (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_name TEXT NOT NULL UNIQUE,
    corrected_name TEXT NOT NULL,
    iiko_id UUID NOT NULL,
    inn VARCHAR(20),  -- для точного匹配 по ИНН
    confidence REAL NOT NULL,
    source VARCHAR(20) DEFAULT 'auto',
    category VARCHAR(20) DEFAULT 'goods',  -- 'goods' | 'service'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE,
    use_count INT DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_ocr_supplier_mapping_raw_trgm ON ocr_supplier_mapping USING gin (raw_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_ocr_supplier_mapping_inn ON ocr_supplier_mapping (inn);

-- 5. ocr_correction_log (история исправлений)
CREATE TABLE IF NOT EXISTS ocr_correction_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES ocr_document(id),
    item_id UUID REFERENCES ocr_item(id),
    field_name VARCHAR(50) NOT NULL,  -- 'name', 'qty', 'price', 'sum'
    old_value TEXT,
    new_value TEXT,
    corrected_by BIGINT,  -- telegram_id
    corrected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reason VARCHAR(100)  -- 'auto_correct', 'manual', 'gpt_reread'
);

CREATE INDEX IF NOT EXISTS ix_ocr_correction_doc_id ON ocr_correction_log (document_id);
CREATE INDEX IF NOT EXISTS ix_ocr_correction_field ON ocr_correction_log (field_name);

-- 6. ocr_confidence_stats (статистика)
CREATE TABLE IF NOT EXISTS ocr_confidence_stats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE NOT NULL,
    doc_type VARCHAR(20),
    total_documents INT NOT NULL,
    avg_confidence REAL NOT NULL,
    auto_mapped_items INT DEFAULT 0,
    manual_mapped_items INT DEFAULT 0,
    auto_mapping_pct REAL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ocr_stats_date ON ocr_confidence_stats (date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ocr_stats_date_type ON ocr_confidence_stats (date, doc_type);
```

---

## 🔑 Переменные окружения (.env)

### Обязательно:

```bash
# OpenAI API (GPT-5.2)
OPENAI_API_KEY=sk-...
OPENAI_OCR_MODEL=gpt-5.2-chat-latest

# Yandex Cloud (OCR)
YANDEX_CLOUD_ID=...
YANDEX_FOLDER_ID=...
YANDEX_SERVICE_ACCOUNT_ID=...
YANDEX_API_KEY=...

# Telegram Bot (уже есть)
TELEGRAM_BOT_TOKEN=...

# Database (уже есть)
DATABASE_URL=postgresql+asyncpg://...

# Google Sheets (маппинг)
GOOGLE_SHEETS_CREDENTIALS=...
OCR_MAPPING_SHEET_ID=...  # отдельная таблица для OCR маппинга
```

### Опционально:

```bash
# Object Storage (хранение оригиналов)
S3_ENDPOINT=https://storage.yandexcloud.net
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=ocr-documents

# Настройки
OCR_AUTO_APPROVE_THRESHOLD=90  # ≥90% confidence → авто
OCR_MAX_RETRIES=2  # макс. попыток распознавания
OCR_DEFAULT_STORE_ID=...  # склад по умолчанию
OCR_ENABLE_QR_REJECT=true  # отклонять чеки с QR
```

---

## 📊 Flow распознавания

### 1. Приём фото

```python
# bot/document_handlers.py

async def handle_photo(message: Message, state: FSMContext):
    # 1. Сохраняем file_id
    # 2. Скачиваем фото
    # 3. Отправляем на валидацию
    quality = await validate_photo(image_bytes)
    
    if not quality.is_good:
        await message.answer(f"❌ {quality.message}")
        return
    
    # 4. Детекция QR
    has_qr = await detect_qr(image_bytes)
    if has_qr:
        await message.answer("📱 Обнаружен QR-код. Используйте ФНС...")
        return
    
    # 5. OCR pipeline
    await process_ocr(message, image_bytes, state)
```

---

### 2. Проверка качества

```python
# utils/photo_validator.py

async def validate_photo(image_bytes: bytes) -> QualityResult:
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    
    # 1. Размытость (Laplacian variance)
    blur_score = cv2.Laplacian(img, cv2.CV_64F).var()
    if blur_score < 80:
        return QualityResult(
            is_good=False,
            issue=f"Фото размытое (резкость {blur_score:.0f}, нужно >80)"
        )
    
    # 2. Освещённость
    brightness = np.mean(img)
    if brightness < 60:
        return QualityResult(
            is_good=False,
            issue=f"Фото слишком тёмное (яркость {brightness:.0f})"
        )
    if brightness > 220:
        return QualityResult(
            is_good=False,
            issue=f"Фото слишком светлое (яркость {brightness:.0f})"
        )
    
    # 3. Ориентация
    height, width = img.shape
    if height > width:
        return QualityResult(
            is_good=False,
            issue="Фото повёрнуто вертикально — сделайте горизонтальный снимок"
        )
    
    # 4. Разрешение
    if width < 800 or height < 600:
        return QualityResult(
            is_good=False,
            issue=f"Низкое разрешение ({width}x{height}, нужно минимум 800x600)"
        )
    
    return QualityResult(is_good=True)
```

---

### 3. Классификация документа

```python
# utils/document_classifier.py

async def classify_document(image_bytes: bytes) -> str:
    """
    Быстрая классификация типа документа через GPT-5.2.
    Возвращает: 'upd', 'receipt', 'act', 'cash_order', 'unknown'
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    
    prompt = """
Классифицируй тип документа.
Варианты: upd, receipt, act, cash_order, unknown.
Верни ТОЛЬКО одно слово.
"""
    
    response = await client.chat.completions.create(
        model="gpt-5.2-chat-latest",
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]}
        ],
        max_tokens=10
    )
    
    return response.choices[0].message.content.strip().lower()
```

---

### 4. Yandex OCR

```python
# adapters/yandex_ocr.py

async def recognize_text(image_bytes: bytes) -> str:
    """
    Распознавание текста через Yandex Vision OCR.
    Возвращает полный текст документа.
    """
    session = yandexcloud.Session(api_key=YANDEX_API_KEY)
    client = VisionAsyncClient(session)
    
    result = await client.document_text_detection(
        DocumentTextDetectionRequest(
            folder_id=YANDEX_FOLDER_ID,
            model="document-text-detection",
            pages=[
                DocumentPage(
                    uri=f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
                )
            ]
        )
    )
    
    # Собираем текст из всех строк
    text = "\n".join(
        page.text
        for page in result.pages
    )
    
    return text
```

---

### 5. GPT-5.2 извлечение полей

```python
# adapters/gpt5_extractor.py

async def extract_fields(text: str, doc_type: str) -> dict:
    """
    Извлечение полей из текста через GPT-5.2.
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    
    prompt = f"""
Извлеки поля из документа типа {doc_type}.

Верни JSON:
{{
  "doc_type": "{doc_type}",
  "supplier": {{"name": "...", "inn": "..."}},
  "buyer": {{"name": "...", "inn": "..."}},
  "date": "YYYY-MM-DD",
  "doc_number": "...",
  "items": [
    {{
      "name": "...",
      "unit": "кг|шт|л",
      "qty": 0.0,
      "price": 0.0,
      "sum": 0.0,
      "vat_rate": "10%|20%|без НДС"
    }}
  ],
  "total_amount": 0.0,
  "total_vat": 0.0,
  "currency": "RUB",
  "quality_check": {{
    "confidence_score": 0-100,
    "warnings": ["..."]
  }}
}}
"""
    
    response = await client.chat.completions.create(
        model="gpt-5.2-chat-latest",
        messages=[
            {"role": "system", "content": "Ты эксперт по российским бухгалтерским документам."},
            {"role": "user", "content": prompt + "\n\nТекст документа:\n" + text}
        ],
        max_tokens=4096,
        temperature=0.1
    )
    
    return json.loads(response.choices[0].message.content)
```

---

### 6. Валидация

```python
# use_cases/ocr_validation.py

async def validate_document(doc: dict) -> ValidationResult:
    errors = []
    warnings = []
    auto_corrected = []
    
    # 1. Проверка дат
    if doc.get('date'):
        try:
            doc_date = datetime.strptime(doc['date'], '%Y-%m-%d').date()
            if doc_date > date.today():
                errors.append(f"Дата в будущем: {doc_date}")
        except ValueError:
            errors.append(f"Некорректная дата: {doc['date']}")
    
    # 2. Проверка сумм
    for i, item in enumerate(doc.get('items', [])):
        expected_sum = round(item['qty'] * item['price'], 2)
        if abs(item['sum'] - expected_sum) > 0.5:
            auto_corrected.append({
                'field': f'items[{i}].sum',
                'old': item['sum'],
                'new': expected_sum
            })
            item['sum'] = expected_sum
            item['_auto_corrected'] = True
    
    # 3. Проверка total
    calculated_total = sum(item['sum'] for item in doc.get('items', []))
    if abs(doc.get('total_amount', 0) - calculated_total) > 5:
        warnings.append(f"total_amount не сходится: {doc['total_amount']} vs {calculated_total}")
    
    # 4. Проверка confidence
    confidence = doc.get('quality_check', {}).get('confidence_score', 0)
    if confidence < 70:
        errors.append(f"Низкая уверенность: {confidence}%")
    
    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        auto_corrected=auto_corrected
    )
```

---

### 7. Авто-маппинг

```python
# use_cases/ocr_auto_mapping.py

async def find_mapping(raw_name: str, iiko_type: str) -> MappingResult | None:
    """
    Поиск маппинга:
    1. ocr_mapping (обученные) ≥85%
    2. iiko_product/supplier (fuzzy) ≥85%
    3. None (нужен ручной маппинг)
    """
    # 1. Проверка ocr_mapping
    async with session() as db:
        mapping = await db.execute(
            select(OcrMapping).where(
                OcrMapping.iiko_type == iiko_type
            )
        )
        
        best_match = fuzzy_match(raw_name, [m.raw_name for m in mapping])
        if best_match and best_match.confidence >= 0.85:
            # Обновляем статистику
            best_match.last_used_at = now()
            best_match.use_count += 1
            await db.commit()
            
            return MappingResult(
                iiko_id=best_match.iiko_id,
                iiko_name=best_match.iiko_name,
                confidence=best_match.confidence,
                source='auto'
            )
    
    # 2. Fuzzy по iiko справочнику
    if iiko_type == 'product':
        iiko_items = await fetch_all_products()  # GOODS+DISH
    else:
        iiko_items = await fetch_all_suppliers()
    
    best_match = fuzzy_match(raw_name, [i.name for i in iiko_items])
    if best_match and best_match.confidence >= 0.85:
        # Сохраняем в ocr_mapping (обучение)
        new_mapping = OcrMapping(
            raw_name=raw_name,
            corrected_name=best_match.name,
            iiko_id=best_match.id,
            iiko_type=iiko_type,
            iiko_name=best_match.name,
            confidence=best_match.confidence,
            source='auto'
        )
        async with session() as db:
            db.add(new_mapping)
            await db.commit()
        
        return MappingResult(
            iiko_id=best_match.id,
            iiko_name=best_match.name,
            confidence=best_match.confidence,
            source='auto'
        )
    
    # 3. Не найдено → ручной маппинг
    return None
```

---

## 📈 Статистика и мониторинг

### Метрики

| Метрика | Формула | Цель |
|---------|---------|------|
| **% авто-маппинга** | `auto_mapped / total_items × 100` | >90% |
| **Средний confidence** | `avg(confidence_score)` | >85% |
| **Кол-во исправлений** | `count(correction_log)` | <10% от документов |
| **Время обработки** | `finished_at - started_at` | <30 сек |
| **Топ ошибок** | `GROUP BY field_name ORDER BY count` | — |

### Дашборд (будущее)

```
📊 OCR Статистика (за 30 дней)

Распознано документов: 1 247
├─ УПД: 856
├─ Чеки: 234
├─ Акты: 89
└─ Ордера: 68

Авто-маппинг: 94.2% ✅
Средний confidence: 87.3% ✅
Исправлений: 7.1% ✅
Среднее время: 18 сек ✅

Топ ошибок:
1. sum (автопересчёт) — 34
2. qty (размыто) — 12
3. name (не найден) — 8
```

---

## 🧪 Тестирование

### Тестовые данные

| Файл | Тип | Страниц | Ожидаемый результат |
|------|-----|---------|---------------------|
| `photo_2026-02-18_07-20-11.jpg` | УПД | 1 | Распознать, найти товары |
| `photo_2026-02-18_07-20-12.jpg` | УПД (лист 2) | 1 | Сгруппировать с предыдущим |
| `photo_2026-02-18_07-20-28.jpg` | Чек | 1 | Распознать (без QR) |
| `photo_2026-02-18_07-20-33.jpg` | РКО | 1 | Распознать рукописный |
| `photo_2026-02-18_07-20-34.jpg` | Акт | 1 | Распознать услугу |

### Критерии приёмки

- [ ] Все 17 фото из `photo_test` распознаются
- [ ] Чеки с QR отклоняются с инструкцией
- [ ] УПД на 2 страницах группируются
- [ ] Суммы автопересчитываются при несовпадении
- [ ] Новые товары попадают в GSheet
- [ ] Повторный товар маппится автоматически

---

## 📅 План работ (вдвоём, интенсивно)

| Этап | Задача | Кто | Время |
|------|--------|-----|-------|
| **1** | Ключи + миграции БД | Ты: ключи, Я: код | 1-2 часа |
| **2** | Ядро OCR (`adapters/*` + `utils/*`) | Я: код, Ты: тесты | 3-4 часа |
| **3** | БД + Маппинг (`models/` + `use_cases/ocr_auto_mapping.py`) | Я: код, Ты: GSheet | 2-3 часа |
| **4** | Pipeline + Валидация | Я: код, Ты: тесты | 3-4 часа |
| **5** | Бот (`bot/document_handlers.py` + `ocr_review.py`) | Я: код, Ты: тесты в TG | 3-4 часа |
| **6** | Статистика + Финал | Я: код, Ты: тесты всех 17 фото | 2-3 часа |

**Итого: 1-2 дня** (не 10 дней!)

---

### 🎯 Реалистичный график

**День 1 (6-9 часов):**
- [ ] Ключи получены
- [ ] Миграции БД созданы
- [ ] Ядро OCR работает (текст → JSON)
- [ ] Авто-маппинг работает

**День 2 (8-11 часов):**
- [ ] Pipeline полный работает
- [ ] Бот принимает фото → выдаёт JSON
- [ ] Все 17 фото из photo_test распознаны
- [ ] Статистика пишется

---

## ✅ Чеклист перед стартом

- [ ] **OpenAI API Key** получен и добавлен в `.env`
- [ ] **Yandex Cloud аккаунт** создан
- [ ] **Yandex Service Account** создан с ролями `vision.editor`
- [ ] **Yandex API Key** получен
- [ ] **GSheet credentials** есть
- [ ] **Database** подключена
- [ ] **Telegram bot** работает
- [ ] **photo_test** папка с примерами
- [ ] **S3 бакет** создан (опционально)

---

## 🔗 Полезные ссылки

- [Yandex Vision OCR Docs](https://cloud.yandex.ru/docs/vision/concepts/document-text-detection)
- [OpenAI GPT-5.2 Docs](https://platform.openai.com/docs/models/gpt-5-2)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [aiogram 3.x Docs](https://docs.aiogram.dev/)
- [SQLAlchemy 2.0](https://docs.sqlalchemy.org/)

---

## 📝 Заметки для редактирования

**Отредактируй следующие секции перед стартом:**

1. **Модель GPT:** `gpt-5.2-chat-latest` → другая если нужно
2. **Пороги маппинга:** `85%` → другое значение
3. **Лимиты:** `MAX_RETRIES=2` → другое
4. **GSheet ID:** `OCR_MAPPING_SHEET_ID` → свой
5. **Стоимость:** пересчитай под свои объёмы

---

**Готов начать? Отметь чеклист и дай ключи!**
