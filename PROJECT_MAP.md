# 🗂 Карта проекта iiko + FinTablo Sync Bot

> Последнее обновление: 2026-02-09
> Язык общения с разработчиком: **русский**

---

## 🎯 О проекте

Telegram-бот для синхронизации справочных данных из **iiko REST API** и **FinTablo REST API** в **PostgreSQL**.
Это **скелет большого проекта** — впереди новые интеграции, источники данных, аналитика.
Текущая фаза: выгрузка всех справочников iiko + FinTablo в БД по кнопкам бота + **авторизация сотрудников через Telegram** + **акты списания с проверкой админами** + **загрузка накладных** + **управление администраторами бота** + **синхронизация остатков по складам** + **проверка минимальных остатков по подразделениям**.

---

## 🧠 Архитектурные принципы (ОБЯЗАТЕЛЬНО соблюдать)

1. **Тонкие хэндлеры** — `bot/handlers.py` только принимает команду, вызывает use_case, отправляет ответ. Никакой бизнес-логики.
2. **Бизнес-логика в use_cases/** — sync, валидация, трансформация данных.
3. **Интеграции в adapters/** — HTTP, XML, внешние API. Use_cases не знают про HTTP/XML.
4. **Никакого хардкода** — все секреты и настройки в `.env`, читаются через `config.py`.
5. **1 кнопка = 1 таблица** — не плодить таблицы без необходимости.
6. **UPSERT-паттерн** — INSERT ON CONFLICT DO UPDATE, батчами по 500 строк.
7. **Mirror-sync** — после UPSERT, DELETE из БД записей, которых больше нет в API. БД = зеркало iiko/FinTablo.
8. **raw_json в каждой таблице** — страховка: полный ответ API хранится как JSONB.
9. **SyncLog — аудит всего** — каждая синхронизация пишется в iiko_sync_log.
10. **Логирование везде** — тайминги API-запросов, прогресс batch, итоги. Verbose, не silent.
11. **Быстрый старт бота** — `SELECT 1` вместо `create_all` при каждом запуске.

---

## 🏗 Конвенции разработки

- **Python 3.12**, async everywhere (asyncio, asyncpg, httpx, aiogram 3)
- **SQLAlchemy 2.0** declarative models, async session
- **Маленькие изменения** — не переписывать всё сразу, инкрементальные правки
- **DRY** — generic `_run_sync()` + `_batch_upsert()` вместо копипасты на каждую таблицу
- **Параллельность** — `asyncio.gather` для независимых API-запросов и sync-операций
- **Persistent HTTP client** — один `httpx.AsyncClient` с connection pool, не создавать новый на каждый запрос

---

## ⚠️ Особенности инфраструктуры (Railway)

- **PostgreSQL на Railway** — удалённая БД, **высокая сетевая задержка ~400мс на round-trip**
- Из-за этого: batch INSERT критичен (500 строк = 1 round-trip вместо 500)
- `pool_recycle=300` — Railway дропает idle-соединения
- `jit=off` — PostgreSQL JIT бесполезен для коротких OLTP-запросов
- Первое подключение может занять ~30 сек (cold start Railway)

---

## ⚠️ Особенности iiko API

- Большинство endpoint'ов возвращают **XML** (suppliers, departments, stores, groups, employees, roles)
- entities/list и products возвращают **JSON**
- XML от iiko содержит **вложенные теги с теми же именами** (например `<employee>` внутри `<employee>` как boolean-флаг) — парсить через `findall()`, не `iter()`!
- Токен авторизации живёт ~15 мин, кешируем на 10, retry при 403
- API endpoint: `https://ip-merzlyakov-e-a-co.iiko.it/resto/api/...`

### Функции iiko_api.py (adapters)

| Функция | Endpoint | Формат | Параметры |
|---------|----------|--------|-----------|
| `fetch_entities(root_type, include_deleted)` | `/resto/api/v2/entities/list` | JSON | `rootType`, `includeDeleted` |
| `fetch_suppliers()` | `/resto/api/suppliers` | XML | — |
| `fetch_departments()` | `/resto/api/corporation/departments` | XML | — |
| `fetch_stores()` | `/resto/api/corporation/stores` | XML | — |
| `fetch_groups()` | `/resto/api/corporation/groups` | XML | — |
| `fetch_products(include_deleted)` | `/resto/api/v2/entities/products/list` | JSON | `includeDeleted` |
| `fetch_employees(include_deleted)` | `/resto/api/employees` | XML | `includeDeleted` |
| `fetch_employee_roles()` | `/resto/api/employees/roles` | XML | — |
| `fetch_stock_balances(timestamp)` | `/resto/api/v2/reports/balance/stores` | JSON | `timestamp` (YYYY-MM-DDThh:mm:ss, дефолт = now) |

---

## ⚠️ Особенности FinTablo API

- **Все endpoints JSON**: `GET /v1/{endpoint}` → `{"status": 200, "items": [...]}`
- **Авторизация:** `Authorization: Bearer <token>` во всех запросах
- **Rate limit:** 300 req/min — при превышении отдаёт 429 Too Many Requests
- **НЕ пагинирует** list-эндпоинты — все записи возвращаются за 1 запрос
- **ID — integer** (BigInteger), не UUID
- **Base URL:** `https://api.fintablo.ru`

### Функции fintablo_api.py (adapters)

| Функция | Endpoint |
|---------|----------|
| `fetch_categories()` | `/v1/category` |
| `fetch_moneybags()` | `/v1/moneybag` |
| `fetch_partners()` | `/v1/partner` |
| `fetch_directions()` | `/v1/direction` |
| `fetch_moneybag_groups()` | `/v1/moneybag-group` |
| `fetch_goods()` | `/v1/goods` |
| `fetch_obtainings()` | `/v1/obtaining` |
| `fetch_jobs()` | `/v1/job` |
| `fetch_deals()` | `/v1/deal` |
| `fetch_obligation_statuses()` | `/v1/obligation-status` |
| `fetch_obligations()` | `/v1/obligation` |
| `fetch_pnl_categories()` | `/v1/pnl-category` |
| `fetch_employees()` | `/v1/employees` |

## ⚙️ Переменные окружения (.env)

| Переменная | Обязат. | Описание |
|------------|---------|-------------------------------------------|
| `DATABASE_URL` | ✅ | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `IIKO_BASE_URL` | ✅ | Базовый URL iiko API (`https://ip-merzlyakov-e-a-co.iiko.it`) |
| `IIKO_LOGIN` | ✅ | Логин iiko API |
| `IIKO_SHA1_PASSWORD` | ✅ | SHA1-хеш пароля iiko |
| `FINTABLO_TOKEN` | ✅ | Bearer-токен FinTablo API |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен Telegram-бота (от @BotFather) |
| `OPENAI_API_KEY` | ✅ | Ключ OpenAI (GPT Vision OCR для накладных) |
| `FINTABLO_BASE_URL` | ❌ | Base URL FinTablo (дефолт `https://api.fintablo.ru`) |
| `LOG_LEVEL` | ❌ | Уровень логирования (дефолт `INFO`) |

Все обязательные переменные читаются через `_require()` в `config.py` — **fail-fast** с понятной ошибкой если пусто.

---

## 📁 Структура файлов

```
test/
├── .env                     # Секреты: БД, iiko API, Telegram-токен, FinTablo токен
├── .gitignore               # Игнор: .env, __pycache__, logs/, venv/
├── config.py                # Чтение .env → переменные (fail-fast если пусто)
│                             #   _require(name) — обязат. переменная, иначе RuntimeError
│                             #   DATABASE_URL, IIKO_BASE_URL, IIKO_LOGIN, IIKO_SHA1_PASSWORD
│                             #   FINTABLO_BASE_URL (дефолт), FINTABLO_TOKEN, TELEGRAM_BOT_TOKEN
│                             #   LOG_LEVEL (дефолт INFO)
├── iiko_auth.py             # Авторизация iiko API (токен, кеш 10 мин, retry×4)
│                             #   get_auth_token() → str — async, кеширует в _token_cache
│                             #   get_base_url() → str — IIKO_BASE_URL из config
│                             #   AUTH_TIMEOUT (connect=10, read=30), AUTH_ATTEMPTS=4, AUTH_RETRY_DELAY=3сек
│                             #   Retry: 403 + таймауты + сетевые ошибки
├── logging_config.py        # Логи: stdout + logs/app.log (ротация 5МБ×3)
│                             #   setup_logging() — вызывается 1 раз в main.py
│                             #   Формат: "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
│                             #   Приглушены: httpx, httpcore, aiogram, sqlalchemy.engine → WARNING
├── main.py                  # Точка входа: логи → проверка БД → запуск бота
│                             #   1. setup_logging()
│                             #   2. SELECT 1 — health check БД
│                             #   3. Bot + Dispatcher + include_router
│                             #   4. dp.start_polling(bot)
│                             #   finally: close_iiko() + close_ft() + dispose_engine()
├── requirements.txt         # Зависимости: python-dotenv, httpx, sqlalchemy[asyncio], asyncpg, aiogram
│
├── adapters/
│   ├── __init__.py
│   ├── iiko_api.py          # HTTP-клиент iiko (persistent httpx, connection pool)
│   │                         #   _get_client() — lazy-init persistent AsyncClient
│   │                         #   close_client() — закрыть при остановке (main.py finally)
│   │                         #   _TIMEOUT (connect=15, read=60), _LIMITS (max=20, keepalive=10)
│   │                         #   8 функций fetch_*() → list[dict]
│   │                         #   XML-парсеры: _parse_employees_xml(), _parse_corporate_items_xml(),
│   │                         #     _parse_roles_xml(), _element_to_dict()
│   └── fintablo_api.py      # HTTP-клиент FinTablo (persistent httpx, Bearer token)
│                             #   _get_client() — lazy-init с base_url + Authorization header
│                             #   close_client() — закрыть при остановке
│                             #   _fetch_list(endpoint, label) — единый GET-fetcher с retry на 429
│                             #   _semaphore = Semaphore(4), _MAX_RETRIES=5, _RETRY_BASE_DELAY=2.0сек
│                             #   13 функций fetch_*() → list[dict]
│
├── bot/
│   ├── __init__.py
│   ├── handlers.py          # Telegram-хэндлеры (тонкие: команда → use_case → ответ)
│   │                         #   FSM-авторизация: фамилия → сотрудник → ресторан
│   │                         #   Главное меню: 🏠 Сменить ресторан | 📂 Команды | 📊 Отчёты | 📄 Документы
│   │                         #   Подменю «Команды»: 9 кнопок iiko + 8 FT + 2 мега-кнопки + 👑 Управление админами + ◀️ Назад
│   │                         #   Подменю «Отчёты»: заглушка + ◀️ Назад
│   │                         #   Подменю «Документы»: 📷 Загрузить накладную | 📝 Создать списание + ◀️ Назад
│   │                         #   Фоновая синхр. номенклатуры + справочников + прогрев кеша writeoff при открытии «Документы»
│   ├── writeoff_handlers.py # Акты списания: FSM сотрудника + проверка админами
│   │                         #   WriteoffStates: store → account → reason → add_items → quantity
│   │                         #   AdminEditStates: choose_field → choose_store/account/item_idx → ...
│   │                         #   Финал: отправка на проверку админам (не в iiko напрямую)
│   │                         #   Админ: ✅ Отправить (iiko) | ✏️ Редактировать | ❌ Отклонить
│   │                         #   Редактирование: склад / счёт / позиции (название/кол-во/удалить)
│   │                         #   Конкурентность: try_lock/unlock — 1 админ за раз
│   │                         #   Защиты: текст в inline-состояниях, double-click, лимиты qty, MAX_ITEMS=50
│   │                         #   Комментарий в iiko: "причина (Автор: ФИО)" — для трекинга
│   ├── admin_handlers.py    # Управление администраторами бота
│   │                         #   /admin_init — bootstrap первого админа (только когда таблица пуста)
│   │                         #   👑 Управление админами (только для админов)
│   │                         #   Показать текущих | Добавить (из сотрудников с tg) | Удалить
│   │                         #   AdminMgmtStates: menu | choosing_employee | confirm_remove
│   └── invoice_handlers.py  # Загрузка накладных (GPT Vision OCR)
│                             #   Фото накладной → GPT-4 Vision → JSON → редактирование → отправка в iiko
│
├── db/
│   ├── __init__.py
│   ├── engine.py            # SQLAlchemy async engine + session factory
│   │                         #   pool_size=5, max_overflow=5, pool_pre_ping=True
│   │                         #   pool_recycle=300, jit=off
│   │                         #   async_session_factory (expire_on_commit=False)
│   │                         #   get_session() — async generator для DI
│   │                         #   dispose_engine() — закрыть пул (main.py finally)
│   ├── init_db.py           # Создание таблиц + безопасная миграция новых столбцов
│   │                         #   create_tables() — create_all + ALTER TABLE IF NOT EXISTS
│   │                         #   drop_tables() — удалить все таблицы (осторожно!)
│   │                         #   _MIGRATIONS: telegram_id, department_id в iiko_employee
│   │                         #   Запуск: python -m db.init_db
│   │                         #   Импортирует и iiko models, и ft_models
│   ├── models.py            # 10 моделей iiko (SyncMixin: synced_at + raw_json) + Base + SyncLog + BotAdmin
│   │                         #   Entity, Supplier, Department, Store, GroupDepartment,
│   │                         #   Product, Employee, EmployeeRole, SyncLog, BotAdmin
│   │                         #   ENTITY_ROOT_TYPES — список 16 допустимых rootType
│   └── ft_models.py         # 13 моделей FinTablo (таблиц) SQLAlchemy (ft_* префикс)
│                             #   FTSyncMixin (synced_at + raw_json)
│                             #   Все PK — BigInteger (ID из FinTablo, autoincrement=False)
│
├── use_cases/
│   ├── __init__.py
│   ├── auth.py              # Бизнес-логика авторизации через Telegram
│   │                         #   find_employees_by_last_name(), bind_telegram_id()
│   │                         #   bind_telegram_id() резолвит role_name из iiko_employee_role
│   │                         #   get_restaurants(), save_department()
│   │                         #   Логирование: тайминги каждой операции
│   ├── user_context.py      # In-memory кеш контекста пользователя
│   │                         #   UserContext (dataclass): employee_id, name, department_id/name, role_name
│   │                         #   get_user_context() — кеш → БД (lazy load), подтягивает role_name из iiko_employee_role
│   │                         #   set_context(), update_department(), invalidate()
│   │                         #   Без Redis/файлов, ~10 КБ RAM на 57 сотрудников
│   ├── writeoff.py          # Бизнес-логика списаний
│   │                         #   classify_role(role_name) — классификация должности → bar/kitchen/unknown
│   │                         #   get_store_keyword_for_role() — ключевое слово для авто-выбора склада
│   │                         #   get_stores_for_department() — склады с фильтром бар/кухня
│   │                         #   get_writeoff_accounts(store_name) — счета с фильтром "списание" + сегмент
│   │                         #   search_products(), get_unit_name(), normalize_unit()
│   │                         #   build_writeoff_document() — comment = "причина (Автор: ФИО)"
│   │                         #   send_writeoff_document()
│   │                         #   preload_for_user() — параллельный прогрев кеша
│   ├── writeoff_cache.py    # TTL-кеш для writeoff-данных (in-memory)
│   │                         #   get/set_stores, get/set_accounts, get/set_unit
│   │                         #   TTL: 600с (склады/счета), 1800с (ед. изм.)
│   │                         #   invalidate(), invalidate_all()
│   ├── pending_writeoffs.py # In-memory хранилище документов на проверке
│   │                         #   PendingWriteoff (dataclass): doc_id, author, store, account, items, admin_msg_ids
│   │                         #   create(), get(), remove()
│   │                         #   try_lock()/unlock() — конкурентность (один админ за раз)
│   │                         #   build_summary_text(), admin_keyboard()
│   │                         #   TTL: 86400с (24ч) автоочистка
│   ├── admin.py             # Управление администраторами бота (CRUD + кеш)
│   │                         #   get_admin_ids() — из БД + in-memory кеш (инвалид. при add/remove)
│   │                         #   is_admin(), list_admins()
│   │                         #   get_employees_with_telegram() — для выбора нового админа
│   │                         #   add_admin(), remove_admin()
│   ├── invoice.py           # Бизнес-логика накладных (GPT Vision OCR)
│   │                         #   Обработка фото накладных → распознавание → документ
│   ├── sync_stock_balances.py # Синхронизация остатков по складам
│   │                         #   sync_stock_balances(triggered_by, timestamp) → int
│   │                         #   Паттерн: full-replace (DELETE + batch INSERT)
│   │                         #   Фильтрация amount ≠ 0, денормализация имён из iiko_store/iiko_product
│   │                         #   get_stock_by_store(), get_stores_with_stock(), get_stock_summary()
│   ├── check_min_stock.py   # Проверка минимальных остатков по подразделениям
│   │                         #   check_min_stock_levels(department_id) → dict
│   │                         #   v2: суммирование残атков по всем складам dept, MAX дедупликация
│   │                         #   format_min_stock_report(data) → str (Telegram Markdown)
│   ├── sync.py              # Бизнес-логика синхронизации iiko
│   │                         #   _run_sync() + _batch_upsert() + _safe_decimal()
│   │                         #   _mirror_delete() — зеркальная очистка (DELETE WHERE NOT IN)
│   │                         #   sync_all_entities() — параллельный asyncio.gather
│   └── sync_fintablo.py     # Бизнес-логика синхронизации FinTablo
│                             #   _run_ft_sync() — единый шаблон
│                             #   _batch_upsert(), _mirror_delete(), _safe_decimal() из sync.py (DRY)
│                             #   13 sync_ft_*() — по одной на каждый справочник
│                             #   sync_all_fintablo() — параллельный asyncio.gather ×13
│
└── logs/
    └── app.log              # Лог-файл (ротация)
```

---

## 🗄 База данных PostgreSQL (Railway)

**Подключение:** `postgresql+asyncpg://...@ballast.proxy.rlwy.net:17027/railway`
**Всего таблиц:** 24 (11 iiko/bot + 13 FinTablo)

---

### 1. `iiko_entity` — Справочники (все 16 типов в одной таблице)

Кнопка бота: **📋 Синхр. справочники**
Источник API: `GET /resto/api/v2/entities/list?rootType=...` (JSON)

| Колонка    | Тип           | Описание                                              |
|------------|---------------|-------------------------------------------------------|
| `pk`       | BigInteger PK | Суррогатный автоинкремент                              |
| `id`       | UUID          | ID сущности из iiko (index)                           |
| `root_type`| String(50)    | Тип справочника: Account, PaymentType, ... (index)    |
| `name`     | String(500)   | Название                                               |
| `code`     | String(200)   | Код                                                    |
| `deleted`  | Boolean       | Удалён в iiko                                          |
| `synced_at`| DateTime      | Время последней синхронизации                          |
| `raw_json` | JSONB         | Полный JSON из API (для дебага)                        |

**Unique constraint:** `uq_entity_id_root_type` на `(id, root_type)`

**16 типов rootType:**
Account, AccountingCategory, AlcoholClass, AllergenGroup, AttendanceType,
Conception, CookingPlaceType, DiscountType, MeasureUnit, OrderType,
PaymentType, ProductCategory, ProductScale, ProductSize, ScheduleType, TaxCategory

---

### 2. `iiko_supplier` — Поставщики

Кнопка бота: **🚚 Синхр. поставщиков**
Источник API: `GET /resto/api/suppliers` (XML)

| Колонка              | Тип          | Описание              |
|----------------------|--------------|-----------------------|
| `id`                 | UUID PK      | ID поставщика         |
| `name`               | String(500)  | Название              |
| `code`               | String(200)  | Код                   |
| `deleted`            | Boolean      | Удалён                |
| `card_number`        | String(100)  | Номер карты           |
| `taxpayer_id_number` | String(100)  | ИНН                   |
| `snils`              | String(50)   | СНИЛС                 |
| `synced_at`          | DateTime     | Время синхронизации   |
| `raw_json`           | JSONB        | Полный ответ API      |

---

### 3. `iiko_department` — Подразделения

Кнопка бота: **🏢 Синхр. подразделения**
Источник API: `GET /resto/api/corporation/departments` (XML)

| Колонка          | Тип          | Описание                                    |
|------------------|--------------|---------------------------------------------|
| `id`             | UUID PK      | ID подразделения                            |
| `parent_id`      | UUID         | Родитель в иерархии (index)                 |
| `name`           | String(500)  | Название                                     |
| `code`           | String(200)  | Код                                          |
| `department_type`| String(50)   | Тип: CORPORATION, JURPERSON, DEPARTMENT...  |
| `deleted`        | Boolean      | Удалён                                       |
| `synced_at`      | DateTime     | Время синхронизации                          |
| `raw_json`       | JSONB        | Полный ответ API                             |

---

### 4. `iiko_store` — Склады

Кнопка бота: **🏪 Синхр. склады**
Источник API: `GET /resto/api/corporation/stores` (XML)

| Колонка          | Тип          | Описание              |
|------------------|--------------|-----------------------|
| `id`             | UUID PK      | ID склада             |
| `parent_id`      | UUID         | Родитель (index)      |
| `name`           | String(500)  | Название              |
| `code`           | String(200)  | Код                   |
| `department_type`| String(50)   | Тип                   |
| `deleted`        | Boolean      | Удалён                |
| `synced_at`      | DateTime     | Время синхронизации   |
| `raw_json`       | JSONB        | Полный ответ API      |

---

### 5. `iiko_group` — Группы и отделения

Кнопка бота: **👥 Синхр. группы**
Источник API: `GET /resto/api/corporation/groups` (XML)

| Колонка          | Тип          | Описание              |
|------------------|--------------|-----------------------|
| `id`             | UUID PK      | ID группы             |
| `parent_id`      | UUID         | Родитель (index)      |
| `name`           | String(500)  | Название              |
| `code`           | String(200)  | Код                   |
| `department_type`| String(50)   | Тип                   |
| `deleted`        | Boolean      | Удалён                |
| `synced_at`      | DateTime     | Время синхронизации   |
| `raw_json`       | JSONB        | Полный ответ API      |

---

### 6. `iiko_product` — Номенклатура

Кнопка бота: **📦 Синхр. номенклатуру**
Источник API: `GET /resto/api/v2/entities/products/list` (JSON)

| Колонка               | Тип           | Описание                         |
|-----------------------|---------------|----------------------------------|
| `id`                  | UUID PK       | ID товара/блюда                  |
| `parent_id`           | UUID          | Родительская группа (index)      |
| `name`                | String(500)   | Название                          |
| `code`                | String(200)   | Код                               |
| `num`                 | String(200)   | Артикул                           |
| `description`         | Text          | Описание                          |
| `product_type`        | String(50)    | GOODS, DISH, PREPARED, SERVICE...|
| `deleted`             | Boolean       | Удалён                            |
| `main_unit`           | UUID          | Единица измерения                 |
| `category`            | UUID          | Категория                         |
| `accounting_category` | UUID          | Бухгалтерская категория          |
| `tax_category`        | UUID          | Налоговая категория              |
| `default_sale_price`  | Numeric(15,4) | Цена продажи                     |
| `unit_weight`         | Numeric(15,6) | Вес единицы                      |
| `unit_capacity`       | Numeric(15,6) | Объём единицы                    |
| `synced_at`           | DateTime      | Время синхронизации              |
| `raw_json`            | JSONB         | Полный ответ API                 |

---

### 7. `iiko_employee` — Сотрудники

Кнопка бота: **👷 Синхр. сотрудников**
Источник API: `GET /resto/api/employees` (XML)

| Колонка      | Тип          | Описание              |
|--------------|--------------|-----------------------|
| `id`         | UUID PK      | ID сотрудника         |
| `name`       | String(500)  | ФИО (объединённое)    |
| `code`       | String(200)  | Код                   |
| `deleted`    | Boolean      | Удалён                |
| `first_name` | String(200)  | Имя                   |
| `middle_name`| String(200)  | Отчество              |
| `last_name`  | String(200)  | Фамилия               |
| `role_id`    | UUID         | Основная должность (index) |
| `telegram_id`| BigInteger   | Telegram user ID (unique, index) |
| `department_id`| UUID       | Выбранный ресторан (iiko_department.id, index) |
| `synced_at`  | DateTime     | Время синхронизации   |
| `raw_json`   | JSONB        | Полный ответ API      |

---

### 8. `iiko_employee_role` — Должности

Кнопка бота: **🎭 Синхр. должности**
Источник API: `GET /resto/api/employees/roles` (XML)

| Колонка          | Тип           | Описание              |
|------------------|---------------|-----------------------|
| `id`             | UUID PK       | ID должности          |
| `name`           | String(500)   | Название              |
| `code`           | String(200)   | Код                   |
| `deleted`        | Boolean       | Удалён                |
| `payment_per_hour`| Numeric(15,4)| Оплата в час          |
| `steady_salary`  | Numeric(15,4) | Оклад                 |
| `schedule_type`  | String(50)    | Тип графика           |
| `synced_at`      | DateTime      | Время синхронизации   |
| `raw_json`       | JSONB         | Полный ответ API      |

---

### 9. `iiko_sync_log` — Лог синхронизаций (аудит)

Автоматически заполняется при каждой синхронизации.

| Колонка          | Тип          | Описание                               |
|------------------|--------------|----------------------------------------|
| `id`             | BigInteger PK| Автоинкремент                          |
| `entity_type`    | String(100)  | Тип синхронизации (index)              |
| `started_at`     | DateTime     | Начало                                  |
| `finished_at`    | DateTime     | Конец                                   |
| `status`         | String(20)   | running / success / error              |
| `records_synced` | Integer      | Кол-во записей                          |
| `error_message`  | Text         | Текст ошибки (если есть)               |
| `triggered_by`   | String(100)  | Кто запустил: tg:user_id / scheduler   |

---

### 10. `bot_admin` — Администраторы бота

Хранит список администраторов бота (CRUD через «👑 Управление админами»).

| Колонка          | Тип          | Описание                                |
|------------------|--------------|-----------------------------------------|
| `id`             | BigInteger PK| Автоинкремент                           |
| `telegram_id`    | BigInteger   | Telegram user ID (unique, index)        |
| `employee_id`    | UUID         | FK → iiko_employee.id                   |
| `employee_name`  | String(500)  | ФИО (для отображения без JOIN)          |
| `added_at`       | DateTime     | Когда добавлен                          |
| `added_by`       | BigInteger   | telegram_id того, кто добавил           |

Bootstrap: `/admin_init` — добавляет текущего пользователя как первого админа (работает только при пустой таблице).

---

### 11. `iiko_stock_balance` — Остатки по складам

Кнопка бота: **📊 Мин. остатки по складам** (в подменю «Отчёты»)
Источник API: `GET /resto/api/v2/reports/balance/stores?timestamp=...` (JSON)
Паттерн: **full-replace** (DELETE all + batch INSERT) при каждой синхронизации

| Колонка        | Тип            | Описание                                         |
|----------------|----------------|--------------------------------------------------|
| `pk`           | BigInteger PK  | Суррогатный автоинкремент                        |
| `store_id`     | UUID           | UUID склада → iiko_store.id (index)              |
| `store_name`   | String(500)    | Название склада (денормализовано)                 |
| `product_id`   | UUID           | UUID товара → iiko_product.id (index)            |
| `product_name` | String(500)    | Название товара (денормализовано)                 |
| `amount`       | Numeric(15,6)  | Конечный остаток (кол-во), может быть < 0        |
| `money`        | Numeric(15,4)  | Конечный денежный остаток (руб)                   |
| `synced_at`    | DateTime       | Время последней синхронизации                     |
| `raw_json`     | JSONB          | Полный JSON из API                               |

**Unique constraint:** `uq_stock_balance_store_product` на `(store_id, product_id)`

> ✅ **Исправлено 2026-02-09:** timestamp теперь включает время (HH:mm:ss), API возвращает актуальные остатки на текущий момент.

---

## 🗄 Таблицы FinTablo (13 таблиц, префикс `ft_`)

Все таблицы FinTablo имеют общие поля от `FTSyncMixin`:
- `synced_at` (DateTime) — время последней синхронизации
- `raw_json` (JSONB) — полный JSON из API (для дебага)

Все PK — `BigInteger` (ID из FinTablo, `autoincrement=False`).

---

### 11. `ft_category` — Статьи ДДС

Кнопка бота: **📊 FT: Статьи**
Источник API: `GET /v1/category`

| Колонка       | Тип           | Описание                              |
|---------------|---------------|---------------------------------------|
| `id`          | BigInteger PK | ID из FinTablo                        |
| `name`        | String(500)   | Название статьи                       |
| `parent_id`   | BigInteger    | Родительская статья (index)           |
| `group`       | String(50)    | income / outcome / transfer           |
| `type`        | String(50)    | operating / financial / investment    |
| `pnl_type`    | String(100)   | Тип дохода/расхода для ОПиУ           |
| `description` | Text          | Описание                              |
| `is_built_in` | Integer       | 1 = системная статья                  |

---

### 12. `ft_moneybag` — Счета

Кнопка бота: **💰 FT: Счета**
Источник API: `GET /v1/moneybag`

| Колонка             | Тип            | Описание                              |
|---------------------|----------------|---------------------------------------|
| `id`                | BigInteger PK  | ID из FinTablo                        |
| `name`              | String(500)    | Название счёта                        |
| `type`              | String(50)     | nal / bank / card / electron / acquiring |
| `number`            | String(200)    | Номер банковского счёта               |
| `currency`          | String(10)     | RUB, USD, EUR...                      |
| `balance`           | Numeric(15,2)  | Текущий остаток                       |
| `surplus`           | Numeric(15,2)  | Зафиксированный остаток               |
| `surplus_timestamp` | BigInteger     | Unix timestamp зафикс. остатка        |
| `group_id`          | BigInteger     | ID группы счетов (index)              |
| `archived`          | Integer        | 1 = архивный                          |
| `hide_in_total`     | Integer        | 1 = не учитывать в итого              |
| `without_nds`       | Integer        | 1 = без НДС                          |

---

### 13. `ft_partner` — Контрагенты

Кнопка бота: **🤝 FT: Контрагенты**
Источник API: `GET /v1/partner`

| Колонка    | Тип           | Описание                   |
|------------|---------------|----------------------------|
| `id`       | BigInteger PK | ID из FinTablo             |
| `name`     | String(500)   | Название                   |
| `inn`      | String(50)    | ИНН                        |
| `group_id` | BigInteger    | ID группы контрагентов (index) |
| `comment`  | Text          | Комментарий                |

---

### 14. `ft_direction` — Направления

Кнопка бота: **🎯 FT: Направления**
Источник API: `GET /v1/direction`

| Колонка       | Тип           | Описание                   |
|---------------|---------------|----------------------------|
| `id`          | BigInteger PK | ID из FinTablo             |
| `name`        | String(500)   | Название                   |
| `parent_id`   | BigInteger    | Родитель (index)           |
| `description` | Text          | Описание                   |
| `archived`    | Integer       | 1 = архивное               |

---

### 15. `ft_moneybag_group` — Группы счетов

Источник API: `GET /v1/moneybag-group`

| Колонка       | Тип           | Описание                   |
|---------------|---------------|----------------------------|
| `id`          | BigInteger PK | ID из FinTablo             |
| `name`        | String(500)   | Название                   |
| `is_built_in` | Integer       | 1 = системная              |

---

### 16. `ft_goods` — Товары

Кнопка бота: **📦 FT: Товары**
Источник API: `GET /v1/goods`

| Колонка          | Тип            | Описание                  |
|------------------|----------------|---------------------------|
| `id`             | BigInteger PK  | ID из FinTablo            |
| `name`           | String(500)    | Название                  |
| `cost`           | Numeric(15,2)  | Стоимость                 |
| `comment`        | Text           | Комментарий               |
| `quantity`       | Numeric(15,4)  | Остаток                   |
| `start_quantity` | Numeric(15,4)  | Начальный остаток         |
| `avg_cost`       | Numeric(15,2)  | Средняя цена закупки      |

---

### 17. `ft_obtaining` — Закупки

Источник API: `GET /v1/obtaining`

| Колонка      | Тип            | Описание                       |
|--------------|----------------|--------------------------------|
| `id`         | BigInteger PK  | ID из FinTablo                 |
| `goods_id`   | BigInteger     | ID товара (index)              |
| `partner_id` | BigInteger     | ID контрагента (index)         |
| `amount`     | Numeric(15,2)  | Сумма закупки                  |
| `cost`       | Numeric(15,2)  | Цена за единицу                |
| `quantity`   | Integer        | Количество                     |
| `currency`   | String(10)     | Валюта                         |
| `comment`    | Text           | Комментарий                    |
| `date`       | String(20)     | Дата закупки (dd.MM.yyyy)      |
| `nds`        | Numeric(15,2)  | Сумма НДС                      |

---

### 18. `ft_job` — Услуги

Источник API: `GET /v1/job`

| Колонка        | Тип            | Описание                  |
|----------------|----------------|---------------------------|
| `id`           | BigInteger PK  | ID из FinTablo            |
| `name`         | String(500)    | Название                  |
| `cost`         | Numeric(15,2)  | Стоимость                 |
| `comment`      | Text           | Комментарий               |
| `direction_id` | BigInteger     | ID направления (index)    |

---

### 19. `ft_deal` — Сделки

Кнопка бота: **📝 FT: Сделки**
Источник API: `GET /v1/deal`

| Колонка              | Тип            | Описание                          |
|----------------------|----------------|-----------------------------------|
| `id`                 | BigInteger PK  | ID из FinTablo                    |
| `name`               | String(500)    | Название                          |
| `direction_id`       | BigInteger     | ID направления (index)            |
| `amount`             | Numeric(15,2)  | Сумма выручки без НДС             |
| `currency`           | String(10)     | Валюта                            |
| `custom_cost_price`  | Numeric(15,2)  | Себестоимость                     |
| `status_id`          | BigInteger     | ID статуса (index)                |
| `partner_id`         | BigInteger     | ID контрагента (index)            |
| `responsible_id`     | BigInteger     | ID ответственного (index)         |
| `comment`            | Text           | Комментарий                       |
| `start_date`         | String(20)     | Дата начала                       |
| `end_date`           | String(20)     | Дата окончания                    |
| `act_date`           | String(20)     | Дата акта                         |
| `nds`                | Numeric(15,2)  | НДС                               |

> jobs / goods / stages — вложенные массивы, хранятся в `raw_json`

---

### 20. `ft_obligation_status` — Статусы обязательств

Источник API: `GET /v1/obligation-status`

| Колонка | Тип           | Описание                   |
|---------|---------------|----------------------------|
| `id`    | BigInteger PK | ID из FinTablo             |
| `name`  | String(500)   | Название                   |

---

### 21. `ft_obligation` — Обязательства

Кнопка бота: **📋 FT: Обязательства**
Источник API: `GET /v1/obligation`

| Колонка        | Тип            | Описание                      |
|----------------|----------------|-------------------------------|
| `id`           | BigInteger PK  | ID из FinTablo                |
| `name`         | String(500)    | Название                      |
| `category_id`  | BigInteger     | ID статьи ДДС (index)        |
| `direction_id` | BigInteger     | ID направления (index)        |
| `deal_id`      | BigInteger     | ID сделки (index)             |
| `amount`       | Numeric(15,2)  | Сумма без НДС                 |
| `currency`     | String(10)     | Валюта                        |
| `status_id`    | BigInteger     | ID статуса (index)            |
| `partner_id`   | BigInteger     | ID контрагента (index)        |
| `comment`      | Text           | Комментарий                   |
| `act_date`     | String(20)     | Дата акта                     |
| `nds`          | Numeric(15,2)  | НДС                           |

---

### 22. `ft_pnl_category` — Статьи Прибылей и Убытков

Источник API: `GET /v1/pnl-category`

| Колонка       | Тип           | Описание                                  |
|---------------|---------------|-------------------------------------------|
| `id`          | BigInteger PK | ID из FinTablo                            |
| `name`        | String(500)   | Название                                  |
| `type`        | String(50)    | income / costprice / outcome / refund     |
| `pnl_type`    | String(100)   | Категория ОПиУ                            |
| `category_id` | BigInteger    | ID связанной статьи ДДС (index)           |
| `comment`     | Text          | Комментарий                               |

---

### 23. `ft_employee` — Сотрудники FinTablo

Кнопка бота: **👤 FT: Сотрудники**
Источник API: `GET /v1/employees`

| Колонка      | Тип            | Описание                                 |
|--------------|----------------|------------------------------------------|
| `id`         | BigInteger PK  | ID из FinTablo                           |
| `name`       | String(500)    | ФИО                                      |
| `date`       | String(20)     | Дата изменения начисления (MM.yyyy)      |
| `currency`   | String(10)     | Валюта                                   |
| `regularfix` | Numeric(15,2)  | Фикс зарплата                            |
| `regularfee` | Numeric(15,2)  | Страховые взносы                         |
| `regulartax` | Numeric(15,2)  | НДФЛ                                     |
| `inn`        | String(50)     | ИНН                                      |
| `hired`      | String(20)     | Дата найма                               |
| `fired`      | String(20)     | Дата увольнения                          |
| `comment`    | Text           | Комментарий                              |

> positions[] — вложенный массив, хранится в `raw_json`

---

## 🤖 Кнопки Telegram-бота

### Главное меню

| Кнопка                       | Действие                                |
|------------------------------|-------------------------------------------|
| 🏠 Сменить ресторан          | Выбор нового ресторана (inline-кнопки) |
| 📂 Команды                  | Открывает подменю синхронизации       |
| 📊 Отчёты                   | Открывает подменю отчётов (в разработке) |
| 📄 Документы                | Открывает подменю документов + фоновая синхр. номенклатуры/справочников + прогрев кеша writeoff |

### Подменю «Команды»

#### iiko

| Кнопка                    | Функция                | Таблица            |
|---------------------------|------------------------|--------------------|
| 📋 Синхр. справочники     | `sync_all_entities()`  | `iiko_entity`      |
| 🏢 Синхр. подразделения   | `sync_departments()`   | `iiko_department`  |
| 🏪 Синхр. склады          | `sync_stores()`        | `iiko_store`       |
| 👥 Синхр. группы          | `sync_groups()`        | `iiko_group`       |
| 📦 Синхр. номенклатуру    | `sync_products()`      | `iiko_product`     |
| 🚚 Синхр. поставщиков     | `sync_suppliers()`     | `iiko_supplier`    |
| 👷 Синхр. сотрудников     | `sync_employees()`     | `iiko_employee`    |

> ℹ️ `sync_employees()` вызывает `fetch_employees(include_deleted=False)` — только активные
> ℹ️ `sync_products()` вызывает `fetch_products(include_deleted=True)` — включая удалённые
| 🎭 Синхр. должности       | `sync_employee_roles()`| `iiko_employee_role`|
| 🔄 Синхр. ВСЁ iiko        | все iiko параллельно   | все iiko таблицы   |

#### FinTablo

| Кнопка                    | Функция                      | Таблица               |
|---------------------------|------------------------------|-----------------------|
| 📊 FT: Статьи             | `sync_ft_categories()`       | `ft_category`         |
| 💰 FT: Счета              | `sync_ft_moneybags()`        | `ft_moneybag`         |
| 🤝 FT: Контрагенты        | `sync_ft_partners()`         | `ft_partner`          |
| 🎯 FT: Направления        | `sync_ft_directions()`       | `ft_direction`        |
| 📦 FT: Товары             | `sync_ft_goods()`            | `ft_goods`            |
| 📝 FT: Сделки             | `sync_ft_deals()`            | `ft_deal`             |
| 📋 FT: Обязательства      | `sync_ft_obligations()`      | `ft_obligation`       |
| 👤 FT: Сотрудники         | `sync_ft_employees()`        | `ft_employee`         |
| 💹 FT: Синхр. ВСЁ         | `sync_all_fintablo()`        | все 13 ft_* таблиц    |
> ℹ️ 5 FT-справочников **без отдельных кнопок** (синхронизируются только через «📈 FT: Синхр. ВСЁ»):
> `ft_moneybag_group`, `ft_obtaining`, `ft_job`, `ft_obligation_status`, `ft_pnl_category`
### Синхронизация ВСЁ iiko (кнопка 🔄)

```
1. sync_all_entities() — 16 rootType параллельно, 1 COMMIT
2. asyncio.gather × 7:
   sync_departments, sync_stores, sync_groups,
   sync_products, sync_suppliers, sync_employees, sync_employee_roles
```

#### Мега-кнопки

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| ⚡ Синхр. ВСЁ (iiko + FT)    | iiko + FinTablo параллельно (все 23 таблицы)  |

#### Администрирование

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| 👑 Управление админами     | Открыть панель управления админами (только для админов) |

#### Навигация подменю

| Кнопка          | Функция                 |
|-----------------|----------------------------|
| ◀️ Назад        | Возврат в главное меню    |

### Подменю «Отчёты»

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| � Мин. остатки по складам   | sync_stock_balances() → check_min_stock_levels(dept) → Telegram-отчёт |
| 🚧 Раздел в разработке (отчёты) | Заглушка (для будущих отчётов)             |
| ◀️ Назад                    | Возврат в главное меню                    |

### Подменю «Документы»

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| � Загрузить накладную          | GPT Vision OCR: фото → JSON → редактирование → iiko |
| 📝 Создать списание          | FSM: склад → счёт → причина → товары → на проверку |
| ◀️ Назад                    | Возврат в главное меню                    |

---

## 🔐 Авторизация сотрудников

### Поток авторизации

```
/start → проверка кеша (get_user_context) → если есть department_id → главное меню
  └── Нет в кеше → БД → кеш
        ├── Авторизован + department_id → «С возвращением, {имя}!» → главное меню
        └── Не авторизован → ввод фамилии
        ├── Не найден → «Не найден, попробуйте ещё раз»
        ├── 1 совпадение → привязка telegram_id → выбор ресторана
        └── >1 совпадений → inline-кнопки выбора сотрудника → выбор ресторана

Выбор ресторана:
  → inline-кнопки из iiko_department (department_type = 'DEPARTMENT')
  → сохранение department_id в iiko_employee
  → главное меню
```

### Данные авторизации (в таблице `iiko_employee`)

| Колонка        | Тип        | Описание                                  |
|----------------|------------|-------------------------------------------|
| `telegram_id`  | BigInteger | Telegram user ID (unique, index)          |
| `department_id`| UUID       | Выбранный ресторан (iiko_department.id)   |

### FSM-состояния (aiogram)

| Состояние                          | Описание                      |
|------------------------------------|-------------------------------|
| `AuthStates.waiting_last_name`     | Ожидание ввода фамилии        |
| `AuthStates.choosing_employee`     | Выбор сотрудника из списка    |
| `AuthStates.choosing_department`   | Выбор ресторана при регистрации |
| `ChangeDeptStates.choosing_department` | Смена ресторана из меню    |

### Логирование авторизации

Все операции auth логируются с таймингами:
- `[auth] Поиск сотрудника по фамилии «...» → N сотрудников за X.XX сек`
- `[auth] Привязка telegram_id=... к сотруднику ... за X.XX сек`
- `[auth] Ресторанов: N за X.XX сек`
- `[auth] Сотрудник «...» → ресторан «...» за X.XX сек`
- `[auth] telegram_id=... не авторизован` (debug)

### Функции auth.py

| Функция | Описание |
|---------|----------|
| `find_employees_by_last_name(last_name)` | Поиск по фамилии (case-insensitive, только `deleted=False`) |
| `bind_telegram_id(employee_id, telegram_id)` | Привязка tg к сотруднику + отвязка от старого + заполнение кеша + резолвинг role_name |
| `get_restaurants()` | Список департаментов с `department_type='DEPARTMENT'` |
| `save_department(telegram_id, department_id)` | Сохранить ресторан, вернуть название |
| `get_employee_by_telegram_id(telegram_id)` | Получить сотрудника по tg_id (dict или None) |

---

## 🧠 In-memory кеш контекста пользователя

**Модуль:** `use_cases/user_context.py`

### Зачем

При каждом отчёте/документе/действии в боте нужно знать `department_id` и `employee_id` сотрудника. Запрос в БД каждый раз = +400мс (Railway latency). Кеш в RAM — 0мс.

### Структура кеша

```python
_cache: dict[int, UserContext] = {}
# telegram_id → UserContext(employee_id, employee_name, first_name, department_id, department_name, role_name)
```

### Жизненный цикл

| Событие | Действие |
|---------|----------|
| Бот запустился | Кеш пустой `{}` |
| Первый запрос от сотрудника | `get_user_context()` → БД → кеш |
| Повторные запросы | Из кеша мгновенно (0мс) |
| Авторизация (bind_telegram_id) | `set_context()` → кеш заполняется |
| Выбор/смена ресторана | `update_department()` → кеш обновляется |
| Перепривязка к другому сотруднику | `invalidate()` → кеш очищается, перезагрузится |
| Рестарт бота | Кеш пуст, загружается лениво |

### API

| Функция | Описание |
|---------|----------|
| `get_user_context(telegram_id)` | Кеш-хит → 0мс; промах → БД → кеш |
| `get_cached(telegram_id)` | Только кеш, без БД (синхронный) |
| `set_context(...)` | Записать полный контекст |
| `update_department(telegram_id, id, name)` | Обновить только ресторан |
| `invalidate(telegram_id)` | Удалить из кеша |
| `clear_all()` | Очистить весь кеш |

---

## 📝 Акты списания (writeoff)

**Модули:** `bot/writeoff_handlers.py`, `use_cases/writeoff.py`, `use_cases/writeoff_cache.py`, `use_cases/pending_writeoffs.py`

### Поток создания (сотрудник)

```
📝 Создать списание → определение склада по должности:
  Бот-админ (bot_admin) → ручной выбор склада
  Бармен/Кассир/Ранер/... → авто-склад «бар»
  Повар/Шеф/Пекарь/... → авто-склад «кухня»
  Нераспознанная должность → ручной выбор склада
  → выбор счёта (фильтр "списание" + сегмент)
  → ввод причины → поиск товаров → указание количества (г/мл/шт)
  → «✅ Отправить на проверку» → pending_writeoffs → рассылка ВСЕМ админам
```

### Классификация должностей (авто-выбор склада)

| Тип | Должности |
|-----|--------|
| **БАР** | Бармен, Старший бармен, Кассир, Кассир-бариста, Кассир-администратор, Ранер |
| **КУХНЯ** | Повар, Шеф-повар, Пекарь-кондитер, Старший кондитер, Заготовщик пицца, Посудомойка |
| **РУЧНОЙ ВЫБОР** | Бот-админы (bot_admin), а также нераспознанные должности (Бухгалтер, Собственник, Управляющий, Техник, Фриланс и т.д.) |

### Проверка (администратор)

```
Админ получает сообщение с summary + 3 кнопки:
  ✅ Отправить в iiko — build_writeoff_document() → iiko API POST
  ✏️ Редактировать — склад / счёт / позиции (наименование, кол-во, удалить)
  ❌ Отклонить — уведомить автора
Конкурентность: try_lock/unlock — если один админ нажал, у остальных кнопки убираются
```

### Фильтрация счетов

142 счёта в iiko → фильтр: `name contains "списание" AND (бар/кухня по имени склада)` → 3–5 счетов.
Пагинация (10/стр) как fallback.

### Комментарий в iiko

Поле `comment` документа = `"причина (Автор: ФИО)"` — для трекинга кто создал акт.

### TTL-кеш (writeoff_cache.py)

| Ключ | TTL | Назначение |
|------|-----|------------|
| stores | 600с (10 мин) | Склады подразделения |
| accounts | 600с | Счета списания |
| units | 1800с (30 мин) | Единицы измерения |

### FSM-состояния

| Состояние | Описание |
|-----------|----------|
| `WriteoffStates.store` | Выбор склада |
| `WriteoffStates.account` | Выбор счёта списания |
| `WriteoffStates.reason` | Ввод причины |
| `WriteoffStates.add_items` | Поиск и добавление товаров |
| `WriteoffStates.quantity` | Ввод количества |
| `AdminEditStates.choose_field` | Админ: что редактировать (склад/счёт/позиции) |
| `AdminEditStates.choose_store` | Админ: выбор нового склада |
| `AdminEditStates.choose_account` | Админ: выбор нового счёта |
| `AdminEditStates.choose_item_idx` | Админ: какую позицию |
| `AdminEditStates.choose_item_action` | Админ: наименование/кол-во/удалить |
| `AdminEditStates.new_product_search` | Админ: поиск замены товара |
| `AdminEditStates.new_quantity` | Админ: новое количество |

### Pending writeoffs (in-memory)

```python
_pending: dict[str, PendingWriteoff] = {}   # doc_id → документ
_lock_set: set[str] = set()                  # залоченные документы
TTL = 86400с (24ч) — автоочистка
```

---

## 👑 Управление администраторами

**Модули:** `bot/admin_handlers.py`, `use_cases/admin.py`, `db/models.py` → `BotAdmin`

### Поток

```
/admin_init → добавить себя как первого админа (только при пустой таблице bot_admin)
📂 Команды → 👑 Управление админами (только для админов):
  📋 Текущие админы — список с ФИО и tg_id
  ➕ Добавить — список сотрудников с telegram_id (не-админов) → выбрать → bot_admin INSERT
  ➖ Удалить — список текущих админов → выбрать → bot_admin DELETE
```

### Кеш admin_ids

```python
_admin_ids_cache: list[int] | None = None  # инвалидируется при add/remove
get_admin_ids() → list[int] — из БД + кеш
is_admin(telegram_id) → bool
```

### Функции admin.py

| Функция | Описание |
|---------|----------|
| `get_admin_ids()` | Все telegram_id админов (с кешем) |
| `is_admin(telegram_id)` | Проверка прав |
| `get_employees_with_telegram()` | Все сотрудники с tg_id (для выбора) |
| `list_admins()` | Текущие админы (для отображения) |
| `add_admin(tg_id, emp_id, name, added_by)` | Добавить админа |
| `remove_admin(tg_id)` | Удалить админа |

---

## ⚡ Оптимизации

- **In-memory UserContext кеш** — dict `{telegram_id: UserContext}` в RAM, ~10 КБ, ленивая загрузка
- **Persistent httpx client (iiko)** — 1 TCP/TLS-соединение, connection pool до 20
- **Persistent httpx client (FinTablo)** — отдельный client с Bearer token, keep-alive pool
- **asyncio.Semaphore(4) для FinTablo** — макс 4 параллельных запроса (rate limit 300 req/min)
- **Retry с exponential backoff** — при 429 Too Many Requests (2с → 4с → 8с → 16с → 32с)
- **Batch INSERT** — до 500 строк в одном INSERT ... ON CONFLICT DO UPDATE
- **asyncio.gather** — параллельные API-запросы (16 iiko справочников, 13 FinTablo)
- **SyncLog в той же сессии** — 0 лишних round-trip
- **pool_recycle=300** — переподключение к Railway каждые 5 мин
- **jit=off** — быстрее планирование batch INSERT в PostgreSQL
- **DRY: общие хелперы в sync.py** — `_batch_upsert()`, `_mirror_delete()` и `_safe_decimal()` переиспользуются в sync_fintablo.py
- **Mirror-sync** — после каждого UPSERT: `DELETE WHERE id NOT IN (ids из API)`. Одна транзакция (upsert + delete + sync_log). Безопасность: пустой набор ID → skip (защита от сбоя API)
- **Токен iiko кешируется** на 10 мин с retry×4
- **TTL-кеш writeoff** — склады/счета 10 мин, ед. измерения 30 мин (writeoff_cache.py)
- **Фоновая синхронизация при открытии Документов** — `sync_products()` + `sync_all_entities()` параллельно через `asyncio.gather` (номенклатура + 16 справочников всегда актуальны)
- **Фоновый прогрев кеша** — `preload_for_user()` через `asyncio.create_task` (склады + счета + admin_ids в RAM)
- **FSM-кеш** — `_stores_cache`, `_accounts_cache` в FSM state.data (0 запросов при пагинации)
- **Фильтрация счетов** — 142 → 3–5 через SQL фильтр ("списание" + бар/кухня)
- **callback.answer() первым** — мгновенный отклик на кнопку, потом логика
- **try_lock/unlock** — конкурентная блокировка документов (один админ за раз)
- **Admin IDs из БД** — `bot_admin` таблица с in-memory кешем, инвалидация при CRUD
- **Параллельный старт списания** — `get_stores_for_department()` + `is_admin()` через `asyncio.gather` (−400мс на холодном старте)
- **Batch-resolve единиц измерения** — `search_products()` резолвит `unit_name`/`unit_norm` для **всех** товаров в одном SQL-запросе (JOIN Entity) вместо N отдельных вызовов `get_unit_name()`. Экономия: 0мс вместо ~400мс при выборе товара
- **Pre-warm admin_ids** — `preload_for_user()` прогревает `get_admin_ids()` параллельно со складами
- **Полное логирование действий** — каждый handler логирует при входе: `[module] действие tg:USER_ID, params`. Модули: `[auth]`, `[nav]`, `[sync]`, `[sync-ft]`, `[writeoff]`, `[writeoff-edit]`, `[admin]`, `[bg]`. Пагинация и guard-хэндлеры — `logger.debug`, остальные — `logger.info`

---

## 🛠 Команды

```bash
# Установка зависимостей
pip install -r requirements.txt

# Создание таблиц
python -m db.init_db

# Запуск бота
python main.py
```

---

## 🐛 Известные грабли (решены, но помнить)

| Проблема | Причина | Решение |
|----------|---------|---------|
| Медленный старт бота (13 сек) | `create_all` на каждый запуск по удалённой БД | `SELECT 1` health check |
| Минута на 200 записей | Каждый INSERT = отдельный round-trip | Batch по 500 строк |
| Половина записей "невалидный UUID" | XML `iter()` находит вложенные одноимённые теги | `findall()` — только прямые дочерние |
| Справочники 40 сек | 16 последовательных fetch + 16 отдельных COMMIT | `asyncio.gather` + 1 COMMIT |
| httpx пересоздаёт TCP | Новый `AsyncClient` на каждый запрос | Persistent client с connection pool |
| FinTablo бесконечная пагинация | `_fetch_list` циклил `?page=N`, но API отдаёт ВСЁ за 1 запрос | Убрана пагинация, 1 GET = все записи |
| FinTablo 429 Too Many Requests | 13 параллельных задач × бесконечный цикл = 500+ req/min | Semaphore(4) + retry с exp. backoff |

---

## 🔮 Планы / следующие шаги

- Это будет **большой проект с различными интеграциями**
- Возможные направления: аналитика, отчёты, другие API-источники
- Архитектура уже готова к масштабированию: adapters/ для новых интеграций, use_cases/ для логики
- FinTablo: scheduler для автоматической периодической синхронизации
- Связки iiko ↔ FinTablo: матчинг контрагентов/сотрудников между системами
- ✅ **Расхождение sync_stock_balances исправлено** — причина: timestamp без времени, iiko отдавал остатки на начало дня

---

## ✅ История изменений

### 2026-02-09 — Исправление точности sync_stock_balances (timestamp)

**Проблема:** Данные в `iiko_stock_balance` после синхронизации расходились с тем, что показывает UI iiko (Склады → Остатки на складах). Пользователи видели в боте другие цифры, чем в iiko.

**Корневая причина:** Параметр `timestamp` в `fetch_stock_balances()` передавался как `date.today().isoformat()` → `"2026-02-09"` (только дата, без времени). iiko API интерпретировал это как `2026-02-09T00:00:00` = **начало дня = конец вчерашнего**. Все сегодняшние проводки (продажи, списания, приходы) НЕ учитывались, а UI iiko показывал остатки на текущий момент.

**Решение:** Изменён дефолтный `timestamp` на `datetime.now().strftime("%Y-%m-%dT%H:%M:%S")` — теперь API возвращает остатки на текущий момент времени, как и UI iiko.

**Документация iiko:** Эндпоинт `reports/balance/stores` принимает `timestamp` в формате `yyyy-MM-dd'T'HH:mm:ss` (учётная дата-время) и возвращает остатки **на этот конкретный момент**. Это рекомендованный API для остатков (быстрее чем OLAP/проводки).

**Ограничение:** Продажи из открытой кассовой смены могут не отражаться до закрытия смены — это особенность iiko (списки складских проводок по продажам создаются при закрытии). Ручные документы (списания, накладные, перемещения) отражаются сразу после проведения.

**Что изменено:**
- `adapters/iiko_api.py` → `fetch_stock_balances()` — дефолт `timestamp` изменён с `date.today().isoformat()` (`"2026-02-09"`) на `datetime.now().strftime("%Y-%m-%dT%H:%M:%S")` (`"2026-02-09T14:30:45"`)
- `adapters/iiko_api.py` — обновлён docstring с описанием поведения timestamp
- `use_cases/sync_stock_balances.py` — обновлён модульный docstring, улучшено логирование (timestamp в логе)

### 2026-02-10 — Проверка минимальных остатков по подразделениям

**Задача:** Присылать в бот сообщение о товарах ниже минимальных остатков для ресторана пользователя.

**Решение (v2 — агрегация по департаменту):**
1. Из `iiko_product.raw_json` извлекаются `storeBalanceLevels` — `{storeId, minBalanceLevel, maxBalanceLevel}`
2. По `storeId` определяется `department` через `iiko_store.parent_id`
3. Фактические остатки из `iiko_stock_balance` **суммируются по всем складам** одного department
4. Если один продукт имеет min на нескольких складах dept — берётся `MAX(minBalanceLevel)` (дедупликация)
5. Сравнение: `total_amount < min_level` → дефицит
6. Результат фильтруется по `department_id` пользователя

**Зачем суммирование по всем складам dept:**
Молоко может приходоваться на кухню, а списываться с бара. minBalanceLevel задан только на баре, а товар лежит на обоих складах. Только суммирование показывает реальную картину.

**Что изменено:**
- Новый файл `use_cases/check_min_stock.py` — `check_min_stock_levels(department_id)` + `format_min_stock_report(data)`
- `bot/handlers.py` → новый handler `btn_check_min_stock` (кнопка «📊 Мін. остатки по складам»): авторизация → sync_stock_balances → check_min_stock_levels(dept) → форматированный Markdown-отчёт
- `bot/handlers.py` → `_reports_keyboard()` — добавлена кнопка «📊 Мін. остатки по складам» в подменю «Отчёты»
- `bot/handlers.py` — исправлены сломанные emoji в кнопках (были `�` вместо корректных символов)

### 2026-02-10 — Исправление: мин. остатки не находились для нового ресторана

**Проблема:** При нажатии «📊 Мін. остатки по складам» для ресторана «Московский» бот возвращал «Все позиции в норме», хотя пользователь установил минимальный остаток на товар «Чоризо» в iiko UI.

**Корневая причина:** Обработчик `btn_check_min_stock` вызывал `sync_stock_balances()` (обновляет фактические остатки), но **не вызывал** `sync_products()`. Минимальные уровни (`storeBalanceLevels`) хранятся в `iiko_product.raw_json` — эти данные обновляются только при синхронизации номенклатуры. Если пользователь добавил/изменил min level в iiko UI, без `sync_products()` БД об этом не знает.

**Решение:** В `btn_check_min_stock` добавлен вызов `sync_products()` **перед** `sync_stock_balances()`:
1. `sync_products()` — обновляет `raw_json` с актуальными `storeBalanceLevels`
2. `sync_stock_balances()` — обновляет фактические остатки
3. `check_min_stock_levels(dept)` — сравнивает min с actual

**Что изменено:**
- `bot/handlers.py` → `btn_check_min_stock` — добавлен `sync_products()` перед `sync_stock_balances()`, обновлено сообщение «⏳ Синхронизирую номенклатуру, остатки и проверяю минимальные уровни...»

**Структура таблицы `iiko_stock_balance`:** уже задокументирована выше (таблица #11)

**✅ Исправлено 2026-02-09:** проблема расхождения данных с UI iiko решена — причина была в параметре `timestamp` без времени (см. changelog ниже).

### 2026-02-10 — Синхронизация остатков по складам (sync_stock_balances)

**Задача:** Загружать текущие остатки товаров по всем складам из iiko API в PostgreSQL.

**Решение:** Full-replace паттерн (DELETE all + batch INSERT) в одной транзакции.

**Что изменено:**
- Новый файл `use_cases/sync_stock_balances.py` — `sync_stock_balances(triggered_by, timestamp)`, паттерн full-replace
- `adapters/iiko_api.py` — новая функция `fetch_stock_balances(timestamp)` → GET `/resto/api/v2/reports/balance/stores`
- `db/models.py` — новая модель `StockBalance` (таблица `iiko_stock_balance`)
- Query helpers: `get_stock_by_store()`, `get_stores_with_stock()`, `get_stock_summary()`

---

### 2026-02-08 — Mirror-sync (зеркальная синхронизация)

**Проблема:** UPSERT обновлял свойства и добавлял новые записи, но если объект удалялся в iiko/FinTablo — его запись оставалась в БД навсегда.

**Решение:** `_mirror_delete()` — после каждого UPSERT выполняется `DELETE FROM table WHERE id NOT IN (ids из API)`. Это превращает БД в точное зеркало источника.

**Что изменено:**
- `sync.py` — добавлен `_mirror_delete()`: generic helper, принимает table + id_column + valid_ids + extra_filters. Поддерживает фильтрацию по root_type для iiko_entity
- `sync.py` → `_run_sync()` — два новых параметра: `pk_column` (какая колонка = ID, по умолчанию `"id"`) и `mirror_scope` (доп. фильтры WHERE для delete, напр. `{"root_type": rt}`)
- `sync.py` → `sync_all_entities()` — mirror-delete для каждого из 16 root_type в рамках общей транзакции
- `sync.py` → `sync_entity_list()` — передаёт `mirror_scope={"root_type": root_type}`
- `sync_fintablo.py` → `_run_ft_sync()` — mirror-delete по колонке `"id"` после UPSERT
- Все операции (upsert + delete + sync_log) в одной транзакции — 1 COMMIT
- Безопасность: если API вернул 0 записей — mirror-delete пропускается с warning в лог (защита от случайной очистки при сбое API)
- Затронуты все 22 таблицы: 9 iiko + 13 FinTablo

### 2026-02-08 — Реструктуризация меню бота

**Проблема:** Все кнопки синхронизации были на главном экране — не масштабируется, нет места для новых разделов.

**Решение:** Главное меню с 3 разделами + подменю.

**Что изменено:**
- `handlers.py` → `_main_keyboard()` — теперь 4 кнопки: 🏠 Сменить ресторан | 📂 Команды | 📊 Отчёты | 📄 Документы
- `handlers.py` → `_commands_keyboard()` — новая клавиатура с всеми кнопками синхронизации (iiko + FT + мега-кнопки) + ◀️ Назад
- `handlers.py` → `_reports_keyboard()` — подменю «Отчёты» (заглушка 🚧) + ◀️ Назад
- `handlers.py` → `_documents_keyboard()` — подменю «Документы» (заглушка 🚧) + ◀️ Назад
- `handlers.py` → хэндлеры `btn_commands_menu`, `btn_reports_menu`, `btn_documents_menu`, `btn_back_to_main`, `btn_stub`
- Все существующие обработчики синхронизации без изменений (они работают по тексту кнопки, который не менялся)

### 2026-02-08 — In-memory кеш контекста пользователя (UserContext)

**Проблема:** При каждом отчёте/документе нужно знать department_id сотрудника. Запрос в БД каждый раз = +400мс (Railway latency).

**Решение:** `use_cases/user_context.py` — dict `{telegram_id: UserContext}` в RAM.

**Что изменено:**
- Новый файл `use_cases/user_context.py` — dataclass `UserContext` (employee_id, employee_name, first_name, department_id, department_name, role_name)
- `get_user_context(telegram_id)` — кеш-хит → 0мс, промах → БД → кеширует (включая role_name из iiko_employee_role)
- `set_context()` — заполняет кеш при авторизации (bind_telegram_id)
- `update_department()` — обновляет ресторан в кеше при смене
- `invalidate()` — очищает кеш при перепривязке
- `handlers.py` — /start использует `get_user_context()` вместо прямого запроса в БД; смена ресторана обновляет кеш
- `auth.py` — bind_telegram_id() заполняет кеш + invalidate() при отвязке старого + резолвит role_name

### 2026-02-09 — Авто-выбор склада по должности при создании акта списания

**Проблема:** При создании акта списания все сотрудники выбирали склад вручную, хотя для барменов/поваров склад очевиден по должности.

**Решение:** Классификация должности → авто-выбор склада, бот-админы всегда выбирают вручную.

**Логика:**
1. Бот-админ (из таблицы `bot_admin`) → ручной выбор склада
2. Должность бара → авто-склад «бар»
3. Должность кухни → авто-склад «кухня»
4. Нераспознанная должность → ручной выбор (fallback)

**Классификация должностей:**
- **БАР:** Бармен, Старший бармен, Кассир, Кассир-бариста, Кассир-администратор, Ранер
- **КУХНЯ:** Повар, Шеф-повар, Пекарь-кондитер, Старший кондитер, Заготовщик пицца, Посудомойка
- **РУЧНОЙ ВЫБОР:** Бот-админы (bot_admin), Бухгалтер, Собственник, Управляющий, Техник, Фриланс и др.

**Что изменено:**
- `user_context.py` — добавлено поле `role_name` в `UserContext`, подтягивается из `iiko_employee_role` по `role_id` сотрудника
- `auth.py` — `bind_telegram_id()` теперь резолвит `role_name` из `iiko_employee_role` и сохраняет в кеш
- `writeoff.py` — новые функции `classify_role()` (бар/кухня/unknown) и `get_store_keyword_for_role()`
- `writeoff_handlers.py` — `start_writeoff()`: сначала `is_admin()` → ручной выбор, иначе `classify_role()` → авто-склад или fallback на выбор

### 2026-02-09 — Фоновая синхронизация при открытии «Документы»

**Проблема:** При создании списания/накладной номенклатура и справочники в БД могли быть устаревшими (новые товары, счета, ед. изм. не подтянуты).

**Решение:** При нажатии «📄 Документы» фоном запускается параллельная синхронизация номенклатуры и 16 справочников через `asyncio.create_task` + `asyncio.gather`. Пользователь не ждёт — меню открывается мгновенно.

**Что изменено:**
- `handlers.py` → `btn_documents_menu()` — добавлен `asyncio.create_task(_bg_sync_for_documents(...))`
- `handlers.py` → `_bg_sync_for_documents()` — новая функция: `asyncio.gather(sync_products, sync_all_entities)` с `return_exceptions=True`
- `triggered_by = "bg:documents:{tg_id}"` — для аудита в `iiko_sync_log`

### 2026-02-09 — Оптимизация скорости создания акта списания

**Проблема:** При создании списания пользователь ловил задержки ~400мс на каждом шаге из-за последовательных DB round-trip (Railway latency).

**Решение:** 3 оптимизации, устраняющие все ощутимые задержки:

| Шаг | Было | Стало | Экономия |
|-----|------|-------|----------|
| Старт списания | `get_stores` → `is_admin` (последовательно, ~800мс) | `asyncio.gather(get_stores, is_admin)` | **−400мс** |
| Выбор товара | `get_unit_name()` отдельный DB-запрос (~400мс) | unit_name/unit_norm уже в product_cache (из batch-resolve) | **−400мс** |
| Поиск товаров | 1 запрос товаров + N запросов единиц | 1 запрос товаров + 1 batch-запрос единиц (в одной DB-сессии) | **−(N−1)×400мс** |
| Прогрев кеша | склады + счета | склады + счета + admin_ids (параллельно) | **−400мс холодный** |

**Что изменено:**
- `writeoff.py` → `search_products()` — batch-resolve `unit_name`/`unit_norm` через JOIN с `iiko_entity` (MeasureUnit) в той же сессии; возвращает `{id, name, main_unit, product_type, unit_name, unit_norm}`
- `writeoff.py` → `preload_for_user()` — добавлен `admin_uc.get_admin_ids()` в `asyncio.gather` со складами
- `writeoff_handlers.py` → `start_writeoff()` — `asyncio.gather(get_stores, is_admin)` вместо последовательных вызовов
- `writeoff_handlers.py` → `select_product()` — использует `product.get("unit_name")` из кеша, fallback на DB если нет

### 2026-02-09 — Полное логирование всех действий пользователя и фоновых процессов

**Проблема:** 94% хэндлеров (62 из 68) не имели entry-логирования. При нажатии «Назад», навигации по меню, синхронизации, редактировании документов — в логах пусто. Невозможно отследить, где именно пошло не так.

**Решение:** Каждый handler теперь логирует при входе с единым форматом: `[module] действие tg:USER_ID, params`.

**Модули логирования:**

| Префикс | Область | Примеры |
|----------|---------|---------|
| `[auth]` | Авторизация | /start, ввод фамилии, выбор сотрудника/ресторана |
| `[nav]` | Навигация | Меню Команды/Отчёты/Документы, Назад, Сменить ресторан, заглушки |
| `[sync]` | Синхронизация iiko | Все кнопки синхр. (справочники, подразделения, склады, ВСЁ iiko) |
| `[sync-ft]` | Синхронизация FT | Все FT-кнопки (статьи, счета, контрагенты, ВСЁ FT) |
| `[bg]` | Фоновые задачи | Фоновая синхронизация при открытии Документов |
| `[writeoff]` | Создание списания | Старт, выбор склада/счёта, причина, поиск/выбор товара, количество, отправка, отмена |
| `[writeoff-edit]` | Редактирование админом | Начало/отмена редактирования, выбор поля, смена склада/счёта/позиции, завершение |
| `[admin]` | Управление админами | Панель, список, добавление, удаление, /admin_init, навигация |

**Уровни:**
- `logger.info` — все значимые действия (нажатия кнопок, ввод данных, выбор элементов)
- `logger.debug` — пагинация, guard-хэндлеры (текст в inline-состояниях)

**Что изменено:**
- `handlers.py` — добавлено 25 entry-логов (auth: 4, nav: 6, sync: 8, sync-ft: 2, bg: 1, mega: 4)
- `writeoff_handlers.py` — добавлено 23 entry-лога (создание: 10, admin approve/reject/edit: 13)
- `admin_handlers.py` — добавлено 10 entry-логов (все 10 хэндлеров + 1 debug для пагинации)

### 2026-02-09 — Аудит и чистка кодовой базы

**Задача:** Ревизия всего проекта — удаление мусора, неиспользуемых импортов, исправление багов, оптимизация.

**Удалены файлы:**
- `f.json` — отладочный дамп продукта «Чоризо», не используется кодом
- `FinTablo-v1-swagger (1).yaml` — справочная Swagger-спецификация, не используется кодом (имя с пробелом и `(1)`)
- Все `__pycache__/` директории

**Очищены неиспользуемые импорты (9 файлов):**
- `main.py` — убран `sys`
- `adapters/iiko_api.py` — убран `date as _date` (после timestamp-фикса используется только `datetime`)
- `bot/handlers.py` — убраны `Bot`, `Dispatcher` (используются только в `main.py`)
- `db/ft_models.py` — убран `Boolean`
- `db/models.py` — убран `uuid as _uuid`
- `use_cases/admin.py` — убран `AsyncSession`
- `use_cases/auth.py` — убран `AsyncSession`
- `use_cases/user_context.py` — убраны `UUID`, `AsyncSession`
- `use_cases/writeoff.py` — убран `AsyncSession`

**Исправлены баги:**
- `bot/writeoff_handlers.py` → `_sending_lock`: добавлен `_sending_lock.add(user_id)` + `finally: _sending_lock.discard(user_id)` — раньше lock проверялся, но никогда не устанавливался (защита от двойной отправки не работала)
- `iiko_auth.py` → unreachable `raise Exception(...)` после for-loop заменён на `raise RuntimeError(...)` с комментарием для static analysis

**Обновлён `.gitignore`:** добавлены `.idea/`, `.vscode/`, `raw_stock_*.json`, `stock_named_*.json`

**Обновлён `PROJECT_MAP.md`:** убраны ссылки на удалённые файлы из дерева и документации

---

## 📋 Как работать с этим проектом (для AI-ассистента)

1. **Всегда читай этот файл первым** при начале нового диалога
2. Соблюдай архитектурные принципы выше — не лепи логику в handlers
3. **Порядок изменений (строго):** анализ существующего кода → use_case (бизнес-логика) → handler (тонкий) → обновить PROJECT_MAP.md. Никогда не начинай с handler'а.
4. При оптимизации: помни про Railway latency — каждый лишний round-trip = +400мс
5. **Логирование — ОБЯЗАТЕЛЬНО для любого нового кода:**
   - Каждый новый handler **ОБЯЗАН** иметь `logger.info()`/`logger.debug()` на входе
   - Формат: `[module] действие tg:%d, params` — где module один из: `[auth]`, `[nav]`, `[sync]`, `[sync-ft]`, `[bg]`, `[writeoff]`, `[writeoff-edit]`, `[admin]` (или новый по аналогии)
   - Значимые действия (кнопки, ввод, выбор) → `logger.info`
   - Пагинация, guard-хэндлеры, noop → `logger.debug`
   - Фоновые задачи: лог на старте и на завершении (с таймингом)
   - Бизнес-логика в use_cases: лог с таймингом (`time.monotonic()`)
   - Ошибки: `logger.warning`/`logger.exception` с контекстом (tg_id, doc_id и т.д.)
   - **Никогда не создавай handler без entry-лога** — это главное правило
6. **Кеширование — 3 уровня, не изобретай четвёртый:**
   - **In-memory dict** (user_context, admin_ids) — для данных, которые живут всю сессию бота. Инвалидация ручная.
   - **TTL-кеш** (writeoff_cache) — для данных из БД, которые могут устареть (склады, счета, единицы). TTL 10–30 мин.
   - **FSM state.data** — для данных внутри одного FSM-флоу (`_stores_cache`, `_accounts_cache`). Живёт до `state.clear()`. Используй для избежания повторных запросов при пагинации/навигации внутри флоу.
   - Перед созданием нового кеша — проверь, подходит ли один из существующих.
7. **Параллелизация — правило, а не оптимизация:**
   - Если 2+ независимых async-вызова идут подряд → `asyncio.gather()` сразу, не «потом оптимизируем»
   - Фоновые задачи: `asyncio.create_task()` + `try/except` + лог старта и завершения. НИКОГДА не `await` для того что не должно блокировать пользователя.
8. **Fallback — всегда graceful degradation:**
   - Нет данных в кеше → запрос в БД (а не ошибка)
   - Нет админов → прямая отправка (а не «невозможно»)
   - Авто-выбор не сработал → ручной выбор (а не сбой)
   - Нет unit_name → «шт» (а не пустая строка)
9. **Telegram UX-паттерны (не нарушай):**
   - `callback.answer()` — ПЕРВЫМ в каждом callback-хэндлере (мгновенная реакция)
   - Эмодзи в начале сообщений (✅ ❌ ⏳ ⚠️ 📄 🏬 📂 🔍 📏)
   - Guard-хэндлеры для текста в inline-состояниях (delete + подсказка «нажмите кнопку»)
   - Пагинация: кнопки ◀️ ▶️ + счётчик «N/M» с callback `noop`
   - `try: await message.delete()` для пользовательских текстовых вводов (чистота чата)
10. **Обновление PROJECT_MAP.md — обязательный финальный шаг:**
    - После КАЖДОГО изменения функциональности — обнови соответствующие секции карты
    - Добавь запись в changelog с датой, описанием проблемы, решения и списком изменённых файлов
    - Если добавил новый файл — добавь его в структуру проекта
11. Не создавай лишних файлов (markdown-отчёты, скрипты) — только то что просят
12. Общайся на русском, кратко, по существу
