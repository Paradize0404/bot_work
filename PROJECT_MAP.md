# 🗂 Карта проекта iiko + FinTablo Sync Bot

> Версия MAP: **2.1** | Обновлено: 2026-02-24  
> Язык: **русский** | Python 3.12 | asyncio + aiogram 3 + SQLAlchemy 2.0 + asyncpg + Redis  
> Deploy: Railway (PostgreSQL ~400ms RTT, Redis FSM)

---

## 🎯 Идентичность проекта

Telegram-бот для **PizzaYolo**: синхронизация iiko REST API + FinTablo REST API → PostgreSQL,
управление складскими остатками, списаниями, заявками, OCR-накладными, отчётами.

**Три слоя архитектуры:** `bot/handler` ← `use_cases/` ← `adapters/`  
**Часовой пояс:** Europe/Kaliningrad (UTC+2) — `now_kgd()` всегда, `datetime.now()` запрещён.  
**Авторасписание:** 07:00 full-sync | 22:00 стоп-лист отчёт | 23:00 авто-перемещение расходников.

---

## 📜 Контракты (K1–K5)

> Контракты — фундаментальные правила, нарушение которых = регрессия.  
> Каждый контракт содержит тестируемый критерий.

### K1: Тонкие хэндлеры
Handler получает ввод → вызывает use_case → отправляет ответ.  
**Тест:** в `bot/*.py` нет SQL-запросов, нет `httpx`, нет бизнес-вычислений.

### K2: Идемпотентность и конкурентность
- Все POST в iiko — с UUID (повторный клик = тот же результат).
- Sync-lock: `asyncio.Lock` per entity, через `acquire_nowait()` (**не** `locked()` — TOCTOU).
- Mirror-delete: sanity ≤50%, иначе skip + warning.
- Double-click: `if user_id in _lock → answer("⏳"); return`.

**Тест:** два `asyncio.gather(sync(), sync())` не дают дублей и не падают.

### K3: Наблюдаемость
- Каждый handler: `logger.info("[module] action tg:%d ...")` на входе.
- Каждый use_case: тайминг `time.monotonic()` + лог итогов.
- Каждая sync: запись в `iiko_sync_log`.
- Секреты: `mask_secrets()` перед логированием. httpx/httpcore loggers ≥ WARNING.

**Тест:** grep по handler'ам — нет handler без `logger.info`/`logger.debug` на первых 5 строках.

### K4: Валидация (Trust Nothing)
- `callback_data` → `split(":", 1)[1]` → `try UUID()/int() except → answer("⚠️"); return`.
- Текст: длина ≤ 100/500/2000. Числа: min/max.
- FSM: проверка авторизации на **каждом** шаге, не только на входе.

**Тест:** callback с невалидным UUID → ответ "⚠️ Ошибка", не 500.

### K5: UX — одно окно, нет мусора
- `callback.answer()` — **первая строка** callback-handler (снимает спиннер).
- Inline → `edit_text()`, **никогда** `answer()`. Reply → delete old + send new.
- Текст пользователя → `message.delete()` всегда.
- Операция >1с → `"⏳ ..."` → `edit_text("✅ результат")`.

**Тест:** бот не создаёт >1 сообщения на 1 действие пользователя (кроме ошибок валидации → edit).

---

## 🗺 Навигация (dependency graph)

> Читай MAP всегда. Дополнительные файлы — по задаче. Формат: `файл (~Nk токенов)`.

```
PROJECT_MAP.md (ЭТОТ ФАЙЛ, ~10k) — ВСЕГДА загружать
│
├─ docs/FILE_STRUCTURE.md (~19k) ←── новая фича, поиск модуля, FSM-потоки
├─ docs/DATABASE.md (~17k)       ←── новая таблица, миграция, данные
├─ docs/API_INTEGRATIONS.md (~8k) ←── внешние API, производительность, Railway
├─ docs/UX_PATTERNS.md (~13k)    ←── Telegram UX, кнопки, пагинация, кеширование
├─ docs/SECURITY.md (~6k)        ←── безопасность, валидация, конкурентность
├─ docs/KNOWN_ISSUES.md (~6k)    ←── грабли, решённые баги
│
├─── Домены (загружать по задаче, самодостаточны) ───
│
├─ docs/SYNC.md (~5k)         ←── scheduler, sync-паттерн, mirror-delete
├─ docs/WEBHOOKS.md (~5k)     ←── iikoCloud hooks, стоп-лист, остатки
├─ docs/REQUESTS.md (~5k)     ←── заявки, авто-склады, авто-контрагент
├─ docs/OCR.md (~6k)          ←── OCR pipeline, маппинг GSheet, iiko XML
├─ docs/WRITEOFFS.md (~4k)    ←── акты списания, pending, история
├─ docs/ENV_VARS.md (~3k)     ←── все env vars, деплой-чеклист
│
└─── Архив / Песочница ───
   ├─ docs/CHANGELOG.md (~10k) ←── последние изменения (2026)
   ├─ docs/archive/             ←── старый changelog, deprecated файлы
   └─ sandbox/                  ←── изолированные эксперименты (.gitignored)
```

**Правило загрузки:** MAP (~10k) + 1–2 домена (~5–10k) = 15–25k → остаётся 100k+ для кода.

---

## 🏗 Конвенции разработки (компактные)

| Правило | Реализация |
|---------|------------|
| Python 3.12 async | asyncio, asyncpg, httpx, aiogram 3 |
| SQLAlchemy 2.0 | Declarative, async session, `expire_on_commit=False` |
| Часовой пояс | `now_kgd()` из `_helpers`. `datetime.now()` = БАГ |
| Shared helpers | `use_cases/_helpers.py`: `now_kgd`, `compute_hash`, `bfs_allowed_groups`, `load_stores_for_department`, `safe_uuid`, `mask_secrets` |
| UPSERT | INSERT ON CONFLICT DO UPDATE, batch=500 |
| Mirror-sync | После UPSERT → DELETE отсутствующих. БД = зеркало API |
| raw_json | Каждая таблица хранит полный ответ API как JSONB |
| DRY | `_run_sync()` + `_batch_upsert()` — один generic, не копипаста |
| Persistent HTTP | Один `httpx.AsyncClient` с pool, не per-request |
| Маленькие PR | Инкрементальные правки, не переписывать всё |
| Параллельность | `asyncio.gather` для независимых вызовов — правило, не оптимизация |

---

## 🚫 Антипаттерны (компактные)

| Запрещено | Правильно |
|-----------|-----------|
| `asyncio.get_event_loop()` | `asyncio.run()` / `get_running_loop()` |
| `time.sleep`, `requests` | `asyncio.sleep`, `httpx.AsyncClient` |
| `print()` | `logging` (через `logging_config.py`) |
| `Optional[X]` | `X \| None` |
| `except Exception: pass` | `logger.exception()` |
| `datetime.now()` / `utcnow()` | `now_kgd()` из `_helpers` |
| `_KGD_TZ = ZoneInfo(...)` заново | `from use_cases._helpers import KGD_TZ` |
| `default=[]` / `default={}` в Column | `default=list` / `default=dict` |
| `REAL` / `FLOAT` для денег | `Numeric(precision, scale)` |
| `JSON` в PostgreSQL | `JSONB` |
| `lock.locked()` + `async with` | `lock.acquire_nowait()` |
| BFS / hash / stores вручную | `bfs_allowed_groups` / `compute_hash` / `load_stores_for_department` |

---

## 📐 Шаблоны масштабирования

> Новый домен = копируй паттерн, не изобретай.

### Шаблон 1: Новый sync (скопируй `use_cases/sync.py`)
```python
# 1. В adapters/: fetch_new_entity() → list[dict]
# 2. В use_cases/: _run_sync("entity", fetch_fn, Model, mapping_fn, session)
# 3. В bot/handlers.py: кнопка → вызов sync → лог → ответ
# 4. Модель: db/models.py — new table с raw_json, synced_at
# 5. SyncLog записывается автоматически
```
**Эталон:** `sync.py::sync_products()` — UPSERT + mirror-delete + SyncLog.

### Шаблон 2: Новый FSM-flow (скопируй `bot/writeoff_handlers.py`)
```python
# 1. States: class NewFlowStates(StatesGroup): step1, step2, confirm
# 2. Entry: кнопка → permission check → prefetch → set state
# 3. Steps: каждый шаг — валидация + edit prompt (не новое сообщение)
# 4. Confirm: use_case → результат → state.clear() → restore_menu_kb()
# 5. Cancel: /cancel → edit prompt → state.clear() → restore_menu_kb()
```
**Эталон:** `writeoff_handlers.py` — полный цикл с cancel, guard, pagination.

### Шаблон 3: Новый домен-документ (скопируй `docs/WRITEOFFS.md`)
```markdown
# Заголовок
> Зависимости: ...
## Контракты домена (XX1–XX4)
## Поток / архитектура
## Модули (таблица)
## Таблицы БД (компактная таблица)
## Частые ошибки
```

---

## 🔄 Протокол эволюции правил

> Правила устаревают. Вместо молчаливого игнорирования — формальный процесс.

### Жизненный цикл правила

```
ACTIVE → REVIEW → MIGRATING → DEPRECATED → REMOVED
```

| Статус | Значение |
|--------|----------|
| `ACTIVE` | Действует, обязательно к соблюдению |
| `REVIEW` | Под вопросом — собираем аргументы за/против |
| `MIGRATING` | Решение принято, идёт переход на новое правило |
| `DEPRECATED` | Старое правило ещё в коде, но новый код пишется по-новому |
| `REMOVED` | Полностью удалено из кода и документации |

### Классификация по устойчивости

| Тип | Пример | Когда меняется |
|-----|--------|----------------|
| 🏛 Фундаментальное | K1 (тонкие хэндлеры), K3 (наблюдаемость) | Никогда (смена = новый проект) |
| 🏗 Архитектурное | UPSERT-паттерн, mirror-sync | При кратном масштабировании |
| 🔧 Техническое | Python 3.12, httpx, aiogram 3 | При смене инструмента |
| 📏 Стилистическое | `X \| None` вместо `Optional[X]` | При смене конвенции |

### Процедура изменения правила

1. **Формулировка:** «Правило X устарело потому что Y. Альтернатива: Z»
2. **Документирование:** в `docs/CHANGELOG.md` запись `[RULE]` с обоснованием
3. **Миграция:** план перехода (какие файлы затронуты, порядок)
4. **Переходный период:** старый и новый подход сосуществуют (DEPRECATED + ACTIVE)
5. **Очистка:** удаление deprecated-кода, обновление документации

### Реестр правил (текущее состояние)

| ID | Правило | Тип | Статус | Версия |
|----|---------|-----|--------|--------|
| K1 | Тонкие хэндлеры | 🏛 | ACTIVE | 1.0 |
| K2 | Идемпотентность + конкурентность | 🏛 | ACTIVE | 1.0 |
| K3 | Наблюдаемость (логи + SyncLog) | 🏛 | ACTIVE | 1.0 |
| K4 | Валидация (Trust Nothing) | 🏛 | ACTIVE | 1.0 |
| K5 | UX — одно окно, нет мусора | 🏗 | ACTIVE | 1.0 |
| R1 | `now_kgd()` вместо `datetime.now()` | 🔧 | ACTIVE | 1.0 |
| R2 | `X \| None` вместо `Optional[X]` | 📏 | ACTIVE | 1.0 |
| R3 | UPSERT batch=500 + mirror-delete | 🏗 | ACTIVE | 1.0 |
| R4 | Persistent httpx.AsyncClient | 🏗 | ACTIVE | 1.0 |
| R5 | Миграции через `_MIGRATIONS` + IF NOT EXISTS | 🔧 | ACTIVE | 1.0 |
| R6 | GSheet как источник прав (не БД) | 🏗 | ACTIVE | 1.0 |
| R7 | OCR: GPT-5.2 Vision (Yandex Vision / Gemini — abandoned) | 🔧 | ACTIVE | 2.0 |

---

## 🤝 Протокол единства (для AI-ассистента)

### Порядок работы
1. **Загрузи MAP** (этот файл) — всегда.
2. **Определи домен** задачи → загрузи 1–2 файла из графа навигации.
3. **Анализ** существующего кода (grep/read) → план.
4. **Реализация:** use_case → handler → тест/проверка.
5. **Документация:** обнови docs/ + CHANGELOG.md.

### Правило 99%+1%
Документация покрывает 99% действий. Для оставшегося 1%:
- Определи ближайший домен-документ.
- Найди шаблон масштабирования (§ Шаблоны выше).
- Скопируй паттерн из эталонного модуля.
- Если нет подходящего шаблона — создай новый домен-документ по Шаблону 3.

### Песочница (sandbox/) — автоматический режим

AI **сам** решает использовать sandbox, когда задача = новая фича / переработка / исследование API / изоляция бага.
Протокол: `sandbox/<feature>_test.py` → итерации → интеграция в основной проект → **зачистка sandbox/**.
AI **не** использует sandbox для простых правок, шаблонных хэндлеров, документации, аудита.

Папка в `.gitignore` (кроме README). Полная инструкция: `sandbox/README.md`.

### Аудит проекта (по запросу пользователя)

Пользователь запрашивает аудит фразой вроде «проведи аудит проекта».
AI выполняет **полный** чеклист ниже, последовательно. Чеклист автоматически расширяется при появлении новых контрактов, правил, модулей — AI генерирует пункты **динамически** на основе текущего состояния MAP.

#### Фаза 1: Контракты (K1–Kn)
Для **каждого** контракта из таблицы «Реестр правил» со статусом ACTIVE:
- Выполнить тест из описания контракта
- Зафиксировать нарушения (файл + строка + суть)

**Пример K1:** `grep` по `bot/*.py` — ни одного SQL/httpx/бизнес-вычисления.
**Пример K3:** `grep` по handler'ам — нет handler без `logger.info` на первых 5 строках.
**Пример K4:** `grep` по callback-handler'ам — каждый содержит `try UUID()/int()`.

#### Фаза 2: Правила (R1–Rn)
Для **каждого** правила из реестра со статусом ACTIVE:
- Проверить соблюдение в коде
- `R1`: нет `datetime.now()` / `datetime.utcnow()` (кроме `_utcnow` в models)
- `R3`: все sync-функции используют `_run_sync` или UPSERT-паттерн
- Новые правила — AI формулирует проверку по описанию

#### Фаза 3: Документация ↔ Код
| Проверка | Как |
|----------|-----|
| Таблицы БД | Кол-во моделей в `db/models.py` + `db/ft_models.py` = кол-во в `docs/DATABASE.md` |
| Модули | Файлы в `use_cases/`, `adapters/`, `bot/` = записи в `docs/FILE_STRUCTURE.md` |
| Env vars | Переменные в `config.py` = записи в `docs/ENV_VARS.md` |
| Кнопки бота | `NAV_BUTTONS` в `global_commands.py` = описание в `docs/UX_PATTERNS.md` |
| Роутеры | `dp.include_router()` в `main.py` = описание в `docs/FILE_STRUCTURE.md` |

#### Фаза 4: Расхождения
Для каждого найденного расхождения:
1. Классифицировать: HIGH (поведение) / MEDIUM (документация) / LOW (стиль)
2. Исправить код или документацию
3. Записать в `docs/CHANGELOG.md` с тегом `[AUDIT]`

#### Фаза 5: Отчёт
Сообщить пользователю сводку:
```
Аудит завершён. Проверено: N контрактов, M правил, X модулей.
Найдено расхождений: A HIGH, B MEDIUM, C LOW.
Исправлено: ... Осталось: ...
```

> **Расширяемость:** AI не ограничен этим списком. Если в реестре появился новый
> контракт K6 или правило R8 — аудит автоматически включает их проверку.
> Если появился новый домен-документ — AI добавляет проверку соответствия.

### DoD (Definition of Done) для любого изменения
- [ ] Контракты K1–K5 не нарушены
- [ ] Логи: entry-лог в handler, тайминг в use_case
- [ ] Валидация: callback_data, текст, FSM-авторизация
- [ ] UX: edit (не answer), delete user text, placeholder для >1с
- [ ] Документация обновлена (docs/ + CHANGELOG)
- [ ] Нет утечек секретов в логах
- [ ] Нет лишних round-trips к БД (Railway latency!)

### Кеширование — 4 уровня
| Уровень | Пример | TTL |
|---------|--------|-----|
| In-memory dict | user_context, admin_ids | 30 мин |
| TTL-кеш | permissions, writeoff_cache | 5–30 мин |
| FSM state.data | `_stores_cache` в flow | до `state.clear()` |
| GSheet → memory | permissions, cloud_org_mapping | 5 мин |

### Fallback — всегда graceful
- Нет кеша → запрос БД (не ошибка)
- Нет админов → отправка всем (не «невозможно»)
- Авто-выбор не сработал → ручной (не сбой)
- Нет unit_name → «шт» (не пустая строка)

---

## ✅ Быстрый чеклист (копируй в промпт)

```
Прочитай PROJECT_MAP.md. Соблюдай контракты K1–K5:

- [ ] handler тонкий → логика в use_cases/ → адаптеры в adapters/
- [ ] callback_data: try UUID()/int() except → answer("⚠️"); return
- [ ] logger.info("[module] action tg:%d") в каждом handler
- [ ] callback.answer() — первая строка callback-handler
- [ ] inline → edit_text(), текст → message.delete()
- [ ] >1с → ⏳ placeholder → edit результат
- [ ] docs/ обновлены + запись в CHANGELOG.md
```
