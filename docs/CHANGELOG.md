# ✅ История изменений (Changelog)

> Читай этот файл при: понимание истории решений, проверка что уже было сделано, контекст прошлых багов.
> Архив записей до 2026-02-18: [archive/CHANGELOG_OLD.md](archive/CHANGELOG_OLD.md)

---

### 2026-02-24 — [FEAT] Система ФОТ: исключения, мотивация, удаление истории, защита листа

**Файлы:** `db/models.py`, `db/init_db.py`, `adapters/google_sheets.py`, `use_cases/salary.py`, `use_cases/salary_history.py`, `bot/salary_handlers.py` (NEW), `main.py`, `bot/global_commands.py`, `bot/permission_map.py`, `bot/handlers.py`

#### Модель SalaryExclusion (новая таблица `salary_exclusions`)

- Новая ORM-модель: `employee_id` (VARCHAR 36 PK), `excluded_by` (String), `excluded_at` (DateTime)
- Миграция в `init_db.py`: `CREATE TABLE IF NOT EXISTS salary_exclusions (...)`
- Сотрудник в исключениях → не попадает в таблицу "Зарплаты" в GSheet

#### Новые колонки SalaryHistory (mot_pct, mot_base)

- `mot_pct = Column(Numeric(6,2), nullable=True)` — процент мотивации
- `mot_base = Column(String(200), nullable=True)` — база мотивации
- Миграции: `ALTER TABLE salary_history ADD COLUMN IF NOT EXISTS mot_pct NUMERIC(6,2)` и `mot_base VARCHAR(200)`
- "История ставок" в GSheet расширена до 8 колонок: `[Сотрудник, Тип расчёта, Ставка, Мотивация %, База мотивации, Дата с, Дата по (скрыта), iiko_id (скрыт)]`

#### Удаление истории при исключении (toggle_salary_exclusion)

- **Раньше:** history закрывалась с датой
- **Теперь:** при исключении сотрудника → `delete_history_for_employee(id)` → полное удаление строк из DB + GSheet
- При возврате (снятие исключения) — история не восстанавливается, добавляется вручную
- `delete_salary_history_rows(row_numbers)` — физическое удаление строк через GSheet `deleteDimension` API

#### close_history_for_deleted_employees (для iiko-удалённых)

- Сотрудники с `deleted=True` в iiko — history закрывается датой `date.today()` (не удаляется)
- Вызывается автоматически в конце `sync_salary_history()`

#### Защита листов и белый фон

- `warningOnly=True` protection добавлена к листам "Зарплаты" и "История ставок"
- Старые защиты удаляются через `_get_protection_delete_requests()` перед добавлением новых
- Явный белый фон (`backgroundColor: {r:1,g:1,b:1}`) для data rows — фикс серых ячеек в "База мотивации"

#### Новый роутер «👥 Список ФОТ» (bot/salary_handlers.py)

- Кнопка в главном меню (`PERM_SETTINGS`)
- Пагинация по 12 сотрудников: ✅ включён / ❌ исключён
- Callback-префиксы: `sal_excl_pg:`, `sal_excl_tog:{emp_id}:{page}`, `sal_excl_close`
- Зарегистрирован в `main.py`, права в `permission_map.py`, нав кнопка в `global_commands.py`

---

### 2026-02-23 — [FIX] + [AUDIT] bot/pastry_handlers.py — соответствие контрактам K1/K4/K5

**Файл:** `bot/pastry_handlers.py`

**Проблема 1 (исходная, предыдущий сеанс):** Внутренние переходы после действий (`pastry_select_group`, `pastry_remove_group`, `pastry_cancel`) вызывали `pastry_groups_menu(callback.message, state)` напрямую. `callback.message.from_user` — это бот, а не пользователь, поэтому `@permission_required` проверял права бота → получал отказ. Пользователь видел "⛔ У вас нет доступа" после успешного добавления/удаления группы.

**Решение (предыдущий сеанс):** Извлечена внутренняя функция `_show_pastry_groups_menu()` без декоратора; все внутренние редиректы используют её.

**Аудит [AUDIT] — найдено и исправлено:**

| # | Контракт | Нарушение | Исправление |
|---|----------|-----------|-------------|
| 1 | K1 | `async_session_factory`, `ProductGroup`, `select`, `func`, `Bot` импортированы в `bot/` файл, но не используются | Удалены |
| 2 | K5 | `pastry_remove_list`: `callback.answer()` не первая строка — при непустом списке спиннер на кнопке не снимался | `await callback.answer()` перенесён в начало |
| 3 | K5 | `pastry_search_group`: `message.delete()` не вызывался — текст поиска оставался в чате | Добавлен `await message.delete()` первой строкой |
| 4 | K4 | `pastry_search_group`: длина текста не валидировалась | Добавлена проверка `len(query) > MAX_TEXT_SEARCH` |
| 5 | K4 | `split(":")[1]` вместо `split(":", 1)[1]` в `pastry_sel` и `pastry_rm` | Исправлено на `split(":", 1)[1]` |
| 6 | LOW | Дублирование `from uuid import UUID` (топ-уровень + внутри функций) | Удалены локальные повторные импорты |

---

### 2026-02-23 — [FIX] Аудит защиты от двойного нажатия: invoice + request handlers

**Файлы:** `bot/invoice_handlers.py`, `bot/request_handlers.py`

**Проблема:** `writeoff_handlers.py` имел `_sending_lock` для всех 3 send-handler'ов, но аналогичные handlers в других модулях были не защищены:
- `invoice_handlers.py::confirm_send` — двойной клик создавал 2 расходных накладных в iiko
- `request_handlers.py::confirm_send_request` — двойной клик создавал 2 заявки в БД + 2 набора уведомлений
- `request_handlers.py::dup_confirm_send` — то же для дублирования заявки
- `request_handlers.py::reject_request` — два администратора одновременно могли оба отклонить → 2 уведомления создателю

**Решение:**
- `invoice_handlers.py` — добавлен `_sending_lock: set[int]`, `confirm_send` делегирует в `_do_confirm_send`
- `request_handlers.py` — добавлен `_sending_lock: set[int]`, `confirm_send_request` → `_do_confirm_send_request`, `dup_confirm_send` → `_do_dup_confirm_send`
- `reject_request` — добавлен `_try_lock_request` + `finally: _unlock_request`

| Модуль | Handler | Защита |
|--------|---------|--------|
| `writeoff_handlers` | 3× send | `_sending_lock` ✅ |
| `writeoff_handlers` | approve/reject/edit pending | DB `is_locked` ✅ |
| `invoice_handlers` | `confirm_send` | `_sending_lock` ✅ |
| `request_handlers` | `confirm_send_request`, `dup_confirm_send` | `_sending_lock` ✅ |
| `request_handlers` | `approve_request`, `reject_request` | `_try_lock_request` ✅ |
| `document_handlers` | `cb_iiko_invoice_send` | атомарный `.pop()` ✅ |

---

### 2026-02-23 — [FIX] 409 при отправке списания + datetime в Redis FSM

**Файлы:** `adapters/iiko_api.py`, `use_cases/product_request.py`

**Проблема 1:** `409 Conflict` от iiko при POST списания — body `Cannot find WriteoffDocument document by id <UUID>`. Документ не отправлялся, пользователь получал ошибку.

**Решение 1:** В `send_writeoff()` при 409 генерируем новый UUID (`uuid4()`), подставляем в `document["id"]`, ждём backoff, продолжаем retry-цикл. До 2 попыток с новым UUID.

**Проблема 2:** `Object of type datetime is not JSON serializable` в `view_request_history` при `state.update_data(_history_cache=requests)` — Redis FSM не может сериализовать `datetime`.

**Решение 2:** `get_user_requests()` теперь возвращает `created_at` как ISO-строку (`.isoformat()`). `format_request_text()` обрабатывает оба типа: `str` → `fromisoformat()`, `datetime` → `.strftime()` (обратная совместимость).

---

### 2026-02-23 — [FIX] Авто-перемещение: товары с `main_unit = NULL` теперь обрабатываются

**Файл:** `use_cases/negative_transfer.py`

**Проблема:** Ежедневное авто-перемещение расх.мат. (23:00) пропускало ряд товаров
с сообщением `пропущено N товаров` в Telegram-уведомлении. Причина — `iiko_product.main_unit = NULL`
для части товаров (iiko API не возвращает `mainUnit` для некоторых позиций).

**Изменения:**
1. `_collect_negative_items` — добавлено поле `measure_unit_name` из OLAP `Product.MeasureUnit` в каждый item.
2. Новая функция `_load_unit_id_by_name(names)` — ищет UUID единиц в `iiko_entity (root_type=MeasureUnit)` по имени.
3. `run_negative_transfer_all_restaurants` — шаг 5b: для товаров с `main_unit = NULL` собираем имена единиц из OLAP и резолвим их UUID через `_load_unit_id_by_name`.
4. В transfer-loop: `unit_id = main_unit OR fallback_from_olap`; пропуск только если оба отсутствуют.
5. `_load_products_by_name` — **двухпроходный поиск**: проход 1 = точное `name.in_()`, проход 2 = `func.trim(name).in_()` для ненайденных — iiko REST добавляет trailing spaces, OLAP нет. Это была **главная причина** пропуска.
6. `skipped_products` дедуплицируется per-restaurant (один товар на Бар + Кухня = 1 запись).

---

### 2026-02-22 — [AUDIT] Аудит проекта по PROJECT_MAP.md

**Цель:** Приведение кода в соответствие с контрактами K1-K5 и документацией.

**Фаза 1: Контракты (K1-K5)**
- **K1 (Тонкие хэндлеры):** Найдены и исправлены SQL-запросы в `bot/document_handlers.py`, `bot/handlers.py`, `bot/pastry_handlers.py`. Логика перенесена в `use_cases/ocr_pipeline.py`, `use_cases/cloud_org_mapping.py`, `use_cases/product_request.py`.
- **K3 (Наблюдаемость):** Написан AST-скрипт `check_loggers.py`. Добавлены пропущенные `logger.info` в первые 5 строк всех хэндлеров (более 50 исправлений в `day_report_handlers.py`, `document_handlers.py`, `handlers.py`, `invoice_handlers.py`, `middleware.py`, `min_stock_handlers.py`, `pastry_handlers.py`, `request_handlers.py`, `writeoff_handlers.py`).
- **K4 (Валидация):** Добавлены проверки `try UUID() except ValueError` в `bot/pastry_handlers.py` для `pastry_sel:` и `pastry_rm:`.
- **K5 (UX):** Добавлены вызовы `await callback.answer()` первой строкой в хэндлерах `bot/document_handlers.py`, `bot/invoice_handlers.py`, `bot/pastry_handlers.py`, `bot/request_handlers.py`, `bot/writeoff_handlers.py`. Исправлена синтаксическая ошибка (IndentationError) в `document_handlers.py`, возникшая при автоматическом добавлении.

**Фаза 2: Правила (R1-R2)**
- **R1 (Время):** Заменены вызовы `datetime.now()` на `now_kgd()` в `use_cases/pending_writeoffs.py` и `use_cases/sync_stock_balances.py`.
- **R2 (Типизация):** Заменены устаревшие аннотации `Optional[X]` на `X | None` в `use_cases/ocr_pipeline.py`.

**Фаза 3: Документация ↔ Код**
- **Таблицы БД:** Написан скрипт `check_db.py` для сверки `__tablename__` с `docs/DATABASE.md`.
- Добавлены пропущенные в документации таблицы: `pastry_nomenclature_group` (44), `writeoff_request_store_group` (45).
- Выявлены таблицы, описанные в документации, но отсутствующие в коде (legacy/внешние): `bot_admin`, `iiko_access_tokens`, `request_receiver`.

---

### 2026-02-22 — Реструктуризация документации: 3-уровневая архитектура + архив

**Цель:** Снижение контекстного окна с ~123k до ~81k токенов. Документация разделена на 3 уровня: Core → Domains → References/Archive.

**Новые доменные файлы (6):**
- docs/ENV_VARS.md — все переменные окружения
- docs/WEBHOOKS.md — iikoCloud вебхуки + обработка событий
- docs/REQUESTS.md — заявки на товары
- docs/OCR.md — OCR-пайплайн для приходных накладных
- docs/WRITEOFFS.md — акты списания
- docs/SYNC.md — синхронизация iiko/FinTablo/GSheet

**Изменения существующих:**
- PROJECT_MAP.md — переписан 729→276 строк (контракты K1-K5, граф зависимостей, шаблоны масштабирования)
- docs/DATABASE.md — добавлен компактный индекс 40 таблиц
- docs/FILE_STRUCTURE.md — добавлен компактный индекс ~60 модулей
- docs/SECURITY_AND_RELIABILITY.md → docs/SECURITY.md (переименован)

**[ARCHIVE] Перенесены в docs/archive/:**
- OCR_IMPLEMENTATION_PLAN.md — поглощен docs/OCR.md
- OCR_REPORT.md — поглощен docs/OCR.md
- PROMPT_FOR_NEW_PROJECT.md — поглощен PROJECT_MAP.md (шаблоны)

**[ARCHIVE] CHANGELOG разделён:**
- Записи до 2026-02-18 → docs/archive/CHANGELOG_OLD.md (~780 строк)
- Актуальный CHANGELOG сокращён до ~430 строк

**Sandbox (песочница):**
- Создана `sandbox/` с README — AI сам решает когда использовать
- Добавлена в `.gitignore` (кроме README)
- Сценарии: новая фича / переработка / исследование API / изоляция бага

**Протокол аудита проекта:**
- 5-фазный чеклист в PROJECT_MAP.md (§ Аудит проекта)
- Динамически расширяется: новые контракты/правила → автоматически в аудит
- Фазы: контракты K → правила R → документация↔код → расхождения → отчёт

---

### 2026-02-21 — Отчёт дня (смены): плюсы/минусы + продажи/себестоимость из iiko

**Файлы:** `use_cases/day_report.py` (новый), `bot/day_report_handlers.py` (новый), `adapters/iiko_api.py`, `bot/permission_map.py`, `bot/_utils.py`, `bot/global_commands.py`, `main.py`

#### Что сделано

1. **Новый адаптер `fetch_olap_by_preset(preset_id, date_from, date_to)`** в `adapters/iiko_api.py`:
   - GET `/resto/api/v2/reports/olap/byPresetId/{preset_id}` с retry-логикой
   - Универсальный метод для получения данных из любого сохранённого OLAP-пресета iiko

2. **Use-case `use_cases/day_report.py`:**
   - `fetch_day_report_data()` — запрос пресета «Выручка себестоимость бот» (96df1c31-...)
   - Парсинг результатов: продажи по PayTypes, себестоимость по CookingPlaceType
   - Средневзвешенный расчёт % себестоимости (по сумме продаж)
   - `format_day_report()` — HTML-форматирование итогового текста

3. **Handler `bot/day_report_handlers.py`:**
   - FSM: `DayReportStates.positives → negatives`
   - Кнопка «📋 Отчёт дня» в подменю «📊 Отчёты»
   - Плюсы → Минусы → ⏳ placeholder → данные iiko → отправка всем 👑 Админ
   - Guard-хэндлеры для нетекстового ввода
   - `set_cancel_kb` / `restore_menu_kb` (UX-паттерн)

4. **Права доступа:**
   - Новый perm_key `📋 Отчёт дня` (`PERM_DAY_REPORT`) в `permission_map.py`
   - Добавлен в `TEXT_PERMISSIONS`, `MENU_BUTTON_GROUPS`, `PERMISSION_KEYS`
   - Кнопка в `NAV_BUTTONS` для `NavResetMiddleware`

5. **Регистрация:**
   - Router `day_report_router` подключён в `main.py`
   - Кнопка добавлена в `reports_keyboard()` (`bot/_utils.py`)

---

### 2026-02-22 — Глобальные улучшения стабильности, производительности и UX

**Файлы:** `bot/middleware.py`, `use_cases/admin.py`, `use_cases/_helpers.py`, `use_cases/cooldown.py`, `use_cases/sync_lock.py`, `bot/handlers.py`, `use_cases/errors.py`, `use_cases/writeoff.py`, `adapters/iiko_api.py`, `main.py`, `use_cases/redis_cache.py`, `use_cases/user_context.py`, `use_cases/cloud_org_mapping.py`, `use_cases/permissions.py`, `bot/_utils.py`, `bot/writeoff_handlers.py`, `bot/invoice_handlers.py`, `bot/request_handlers.py`, `.github/workflows/ci.yml`

#### Что сделано

Реализован комплексный пакет улучшений на основе аудита документации:

1. **Глобальный обработчик ошибок и алерты:**
   - Добавлена функция `mask_secrets` в `_helpers.py` для скрытия токенов и паролей в логах.
   - Добавлена функция `alert_admins` в `admin.py` для отправки критических ошибок пользователям с ролью «🔧 Сис.Админ» (или обычным админам как fallback).
   - Интегрировано в `main.py` (глобальный `try...except` вокруг `dp.start_polling`).

2. **Rate Limiting и Sync Locks:**
   - Создан `use_cases/cooldown.py` для in-memory ограничения частоты запросов (защита от спама кнопками).
   - Создан `use_cases/sync_lock.py` с `asyncio.Lock` для предотвращения параллельного запуска одинаковых синхронизаций.
   - Декоратор `@with_cooldown` и блокировки внедрены в `bot/middleware.py` и `bot/handlers.py`.

3. **Идемпотентные POST-запросы и классификация ошибок:**
   - Создан `use_cases/errors.py` с функцией `is_transient` для определения временных сетевых ошибок (502, 503, Timeout).
   - В `use_cases/writeoff.py` добавлена генерация `uuid4()` для поля `id` документа списания.
   - В `adapters/iiko_api.py` (`send_writeoff`) добавлена логика повторных попыток (retry) с экспоненциальной задержкой при транзитных ошибках.

4. **Health-check и Graceful Shutdown:**
   - Эндпоинт `/health` в `main.py` теперь выполняет реальный запрос к БД (`SELECT 1`) для проверки живости пула соединений.

5. **Redis Caching Utility:**
   - Создан `use_cases/redis_cache.py` для унифицированной работы с Redis.
   - In-memory кеши в `use_cases/user_context.py`, `use_cases/cloud_org_mapping.py` и `use_cases/permissions.py` переведены на Redis для обеспечения консистентности при масштабировании (несколько воркеров).

6. **UX: Пагинация для длинных списков:**
   - В `bot/_utils.py` функция `items_inline_kb` обновлена для поддержки пагинации.
   - Добавлены обработчики страниц (`*_page`) и обновлены клавиатуры в `bot/writeoff_handlers.py`, `bot/invoice_handlers.py` и `bot/request_handlers.py` (склады, товары, контрагенты, шаблоны, история).

7. **CI/CD Pipeline:**
   - Создан файл `.github/workflows/ci.yml` для автоматического запуска `pytest`, `flake8` и `black` при пуше в ветку `main`.

---

### 2026-02-21 — Гранулярные права + PermissionMiddleware + auto-sync GSheet

**Файлы:** `bot/permission_map.py` (новый), `bot/global_commands.py`, `use_cases/permissions.py`, `adapters/google_sheets.py`, `bot/handlers.py`, `bot/document_handlers.py`, `main.py`

#### Что сделано

Полная переработка системы прав доступа: вместо бинарных ✅ на целые разделы — гранулярные операции.

**Архитектурные изменения:**
1. **`bot/permission_map.py`** — единственный источник истины: 6 ролей, 13 гранулярных perm_key, маппинги text→perm и callback→perm
2. **`PermissionMiddleware`** (outer-middleware) — автоматическая проверка прав ДО вызова хэндлера:
   - Reply-кнопки: `TEXT_PERMISSIONS[message.text]` → `has_permission()`
   - Inline-кнопки: `CALLBACK_PERMISSIONS[prefix]` → `has_permission()`, `CALLBACK_ADMIN_ONLY` → `is_admin()`
   - Inline-кнопки заявок: `CALLBACK_RECEIVER_OR_ADMIN` → `is_receiver() OR is_admin()`
3. **Auto-sync GSheet столбцов**: при добавлении/удалении perm_key в `permission_map.py` → столбец автоматически появляется/удаляется из GSheet «Права доступа» при следующей синхронизации
4. **Удалён `@permission_required`** из всех хэндлеров — middleware делает это централизованно

**Новые гранулярные perm_key (13 вместо 5):**
- Списания: Создать, История, Одобрение
- Накладные: Создать шаблон, Создать накладную
- Заявки: Создать, История, Одобрение
- Отчёты: Просмотр, Изменение мин.остатков
- Документы OCR: Загрузка, Отправка в iiko
- Настройки

**Callback-защита middleware (4 категории):**
- `CALLBACK_PERMISSIONS` — проверка `has_permission(perm_key)`: OCR отправка/отмена, маппинг
- `CALLBACK_ADMIN_ONLY` — проверка `is_admin()`: одобрение/отклонение/редактирование списаний
- `CALLBACK_RECEIVER_OR_ADMIN` — проверка `is_receiver() OR is_admin()`: одобрение/отклонение/редактирование заявок

**Полная карта ролей и уведомлений (6 ролей):**
- 👑 Админ — bypass всех прав + получает: списания на проверку, заявки с кнопками, отчёты sync, алерты (fallback)
- 🔧 Сис.Админ — получает ТОЛЬКО системные ошибки (ERROR/CRITICAL). Fallback → админы
- 📬 Получатель — получает заявки (text-only, без кнопок). Может одобрять/редактировать/отклонять
- 📦 Остатки — получает уведомления об остатках ниже минимума (pinned). Fallback → все авторизованные
- 🚫 Стоп-лист — получает стоп-лист + ежевечерний отчёт (22:00). Fallback → все авторизованные
- 📑 Бухгалтер — получает OCR накладные на подтверждение/отправку в iiko

**Принцип для разработчика:** добавляешь кнопку → добавь строку в `permission_map.py` → GSheet получит столбец автоматически. Middleware заблокирует доступ без прав.

---

### 2026-02-20 — Авто-перемещение расходных материалов (23:00)

**Файлы:** `use_cases/negative_transfer.py` (новый), `use_cases/scheduler.py`, `adapters/iiko_api.py`, `config.py`

#### Что сделано

Ежедневное автоматическое перемещение товаров с отрицательными остатками из группы
«Расходные материалы» с хозяйственного склада на барные/кухонные.

**Принцип:**
1. В 23:00 по Калининграду APScheduler запускает `use_cases/negative_transfer.py`
2. Из БД загружаются все активные склады (`iiko_store`)
3. Паттерн имени `"TYPE (РЕСТОРАН)"` → авто-сборка карты ресторанов без хардкода
4. Один OLAP v1 GET-запрос: `/resto/api/reports/olap?report=TRANSACTIONS&from=...&groupRow=Account.Name&groupRow=Product.TopParent&...`
5. Фильтр: `Product.TopParent == "Расходные материалы"` + `FinalBalance.Amount < 0`
6. Для каждой пары склад-источник → склад-цель: POST `/resto/api/v2/documents/internalTransfer`
7. Запись в `iiko_sync_log`, уведомление админам в Telegram

**Авто-масштабирование:** при добавлении нового ресторана (нового склада «Хоз. товары (НОВЫЙ)»
и «Бар (НОВЫЙ)»/«Кухня (НОВЫЙ)» в iiko) — перемещения начинаются автоматически без изменений кода.

#### Новые env-переменные (все с дефолтами, менять не нужно при стандартных именах складов)
- `NEGATIVE_TRANSFER_SOURCE_PREFIX` — дефолт `"Хоз. товары"`
- `NEGATIVE_TRANSFER_TARGET_PREFIXES` — дефолт `"Бар,Кухня"`
- `NEGATIVE_TRANSFER_PRODUCT_GROUP` — дефолт `"Расходные материалы"`

#### Новые функции в `adapters/iiko_api.py`
- `fetch_olap_transactions_v1(date_from, date_to)` — GET OLAP v1, поддержка JSON и XML ответов
- `send_internal_transfer(document)` — POST `/resto/api/v2/documents/internalTransfer`

#### Результаты теста (20.02.2026)
- Рестораны обнаружены: Клиническая, Московский
- OLAP-запрос: 14 340 строк
- Отрицательных позиций: 59 по 4 складам (Бар/Кухня × 2 ресторана)
- Найдено в БД: 25/29 товаров (4 не найдены — разное написание в iiko OLAP и iiko номенклатуре)

#### Баг найден и исправлен в ходе code-review
`_collect_negative_items`: `safe_float()` может вернуть `None` если поле отсутствует.
`if amount >= 0` при `amount=None` → `TypeError`. Исправлено на `if amount is None or amount >= 0`.

#### Диагностика
```bash
python test_negative_transfer.py           # preview без отправки
python test_negative_transfer.py --execute # реальный запуск
```

---

### 2026-02-20 — Расчёт себестоимости: 3 критических бага + фильтр складов подразделения

**Файлы:** `use_cases/outgoing_invoice.py`

#### 1. Фильтр складов подразделения для СЦС

**Было:** СЦС считалась по остаткам **всех** складов системы. Если одинаковый товар
хранится на нескольких складах разных подразделений по разным ценам — цена размывалась
между ними и переставала отражать реальную закупочную цену для конкретной точки.

**Стало:** `calculate_goods_cost_prices(store_ids)` принимает множество складов
выбранного подразделения из GSheet «Настройки». СЦС считается только по этим складам.
**Двухуровневый fallback:** dept-склады → all-stores (если товар не нашёлся в dept) →
последняя накладная (если нет ни в каких остатках).

До фикса: 89 товаров (в т.ч. п/ф Чиабатта, Шу меренга, Страчателла) получали цену 0,
т.к. хранятся на производственных складах другого отдела.

#### 2. Баг: неправильное поле `"amount"` для preparedCharts

**Было:** `_pick_effective(prepared_charts, "amount")` — поле `amount` **не существует**
в ответе API для preparedCharts.

**Стало:** `_pick_effective(prepared_charts, "amountOut")` — в обоих типах техкарт
(`assemblyCharts` и `preparedCharts`) правильное поле = `amountOut` (количество ингредиента
после потерь при обработке).

#### 3. Баг: не делилось на `assembledAmount`

**Было:** `all_costs[dish_id] = round(total_cost, 2)` — суммарная стоимость ингредиентов
всей партии записывалась как цена **одной единицы**.

**Стало:** `cost_per_unit = total_cost / assembledAmount`. Поле `assembledAmount` на уровне
техкарты указывает, сколько единиц блюда/п/ф производит рецепт. Пример: «п/ф Гренки на Цезарь»
имеет `assembledAmount = 0.165` (производит 165 г), а не 1 порцию.

**Эффект на цены (пример):**

| Блюдо | До | После |
|---|---|---|
| Гренки на цезарь цех | 39.75 | 250.74 |
| Соус фиш майо цех | 73.76 | 253.81 |
| Шу шоколадные цех | 211.73 | 32.89 |
| Пр_Шу шоколадный | 423.47 | 65.78 |
| Говяжья вырезка на тар тар цех | 1499.80 | 1499.80 ✓ (не изменилась) |

---

### 2026-02-20 — Redis FSM + Persistent Pending Writeoffs

**Цель:** Бот переживает рестарт без потери пользовательских сессий и ожидающих актов списания.

#### 1. FSM Storage → Redis (`RedisStorage`)

**Было:** `Dispatcher()` — `MemoryStorage` по умолчанию. При рестарте все FSM-состояния
(шаг диалога, накопленные данные) терялись. Пользователь «зависал» без ответа.

**Стало:** `Dispatcher(storage=RedisStorage.from_url(REDIS_URL))`. FSM-состояния хранятся
в Redis на Railway → переживают рестарт. Пользователь продолжает flow с того же шага.

**Файлы:** `config.py` (+`REDIS_URL`), `main.py` (RedisStorage), `requirements.txt` (+`redis>=5.0.0`)

#### 2. Pending Writeoffs → PostgreSQL

**Было:** `_pending: dict[str, PendingWriteoff] = {}` в RAM. При рестарте акты,
ожидающие проверки админом, терялись безвозвратно (был warning в логах).

**Стало:** Таблица `pending_writeoff` в PostgreSQL с полями: `doc_id`, `author_chat_id`,
`store_id`, `account_id`, `items` (JSONB), `admin_msg_ids` (JSONB), `is_locked` (атомарный лок).
TTL 24ч — автоочистка expired документов. Конкурентность: `UPDATE ... WHERE is_locked = false`.

**Файлы:** `db/models.py` (+`PendingWriteoffDoc`), `use_cases/pending_writeoffs.py` (полный рерайт sync→async),
`bot/writeoff_handlers.py` (+`await` на ~40 вызовах), `db/init_db.py` (+индексы)

#### 3. Документация

Обновлены: `PROJECT_MAP.md` (секции 5, 10), `SECURITY_AND_RELIABILITY.md` (секция 7, чеклист), `DATABASE.md`

---

### 2026-02-20 — OCR маппинг: дропдаун, дедупликация, UX-исправления

**Коммиты:** `4892cc3` → `b7ce77d`

#### Dropdown «Маппинг Импорт»: все товары видимы по поиску

**Проблема:** Google Sheets поиск в dropdown — **по префиксу**. Товары `т_бутылка`,
`п/ф сироп` и прочие с техническими префиксами были недостижимы — они лежали в
конце списка (lower-case сортируется после upper-case в русском), а поиск «бут»
находил только «Бутылка».

**Решение: скрытый справочный лист + display_name**
- `adapters/google_sheets.py` — добавлен лист `«Маппинг Справочник»` (скрытый):
  - `_write_ref_column()` — пишет имена в колонку A
  - `refresh_import_sheet_dropdown()` — перезаписывает ref-лист + ставит `ONE_OF_RANGE` валидацию на все товарные строки (диапазон `prd_start_row` + 1000)
- `use_cases/ocr_mapping.py`:
  - `_strip_tech_prefix()` — убирает `т_`, `п/ф `, `п/ф_`, `п/ф` из имени
  - `_load_iiko_goods_products()` — возвращает только GOODS; каждая запись содержит `name` (реальное в iiko) и `display_name` (без префикса); сортировка по `display_name.lower()`
  - В ref-лист пишутся `display_name` → «бутылка 2л ПЭТ с крышкой» вместо «т_бутылка 2л ПЭТ с крышкой»
  - `finalize_transfer()` — при поиске по iiko_name добавляются reverse-алиасы: «бутылка 2л ПЭТ с крышкой» → находит «т_бутылка 2л ПЭТ с крышкой» в БД
  - `refresh_ref_sheet()` — обновляет ref-лист актуальными GOODS display-именами
- Кнопка **«🔄 Обновить список товаров»** в уведомлении бухгалтеру → `cb_refresh_mapping_ref` handler
- Ежедневная синхронизация (шаг 6) — обновляет ref-лист в 07:00
- Результат: поиск «бут» находит все «бутылка*» включая «т_бутылка*»

#### iiko XML: статус накладной
- `adapters/iiko_api.py` — `documentStatus` изменён `CLOSED` → `PROCESSED` (правильный статус по iiko XML-схеме)

#### Дедупликация OcrDocument
- `_save_ocr_document()` — перед созданием нового документа удаляет существующие `recognized`/`pending_mapping` с тем же `doc_number + doc_type` (сначала `ocr_item`, затем `ocr_document` — FK без ON DELETE CASCADE)
- `get_pending_ocr_documents(doc_ids=…)` — при запросе по ID статус не фильтруется (документы уже в `pending_mapping`); добавлена Python-дедупликация по `(doc_number, doc_type)` как страховка
- Удалено 20 накопившихся дублей из БД вручную

#### После маппинга — только документы текущей сессии
- `_handle_mapping_done()` теперь берёт `_pending_doc_ids.pop(tg_id)` и грузит документы **этого пользователя из этой загрузки** через `get_pending_ocr_documents(doc_ids=current_doc_ids)` — ранее грузились все `pending_mapping` всех пользователей за всё время

#### UX-исправления
- `_repush()` — теперь редактирует сообщение на месте через `bot.edit_message_text(chat_id, message_id, …)`. Fallback: delete + send новым, если edit не удалось
- Предупреждение «⚠️ Не все позиции заполнены» содержит кнопку **«✅ Маппинг готов»** для повторной отправки
- **Критический баг:** `async def _handle_mapping_done(placeholder, tg_id)` — строка с определением функции была утеряна, тело функции висело внутри `cb_refresh_mapping_ref`. Это вызывало `NameError` при нажатии «Маппинг готов» — бот зависал на «⏳ Проверяю…» навсегда
- `_save_ocr_document()` — исправлен `NameError: name 'select' is not defined` (импортирован как `_sa_select`)

#### Ежедневная синхронизация: 6 шагов
`use_cases/scheduler.py` — расширены шаги daily sync:
1. iiko → БД (все справочники)
2. FinTablo → БД
3. Остатки по складам → БД
4. GSheet мин/макс → БД
5. БД номенклатура → GSheet «Мин остатки»
6. **NEW** БД GOODS display-имена → GSheet «Маппинг Справочник»

---

### 2026-02-19 — OCR Pipeline: исправления НДС и группировки многостраничных накладных

**Коммит:** `31537f9`  
**Файл:** `use_cases/ocr_pipeline.py`

**Баг 1 — ложное предупреждение «сумма не совпадает с расчётной»:**
- **Симптом:** Для позиции с НДС 22% предупреждение `сумма 850.0 не совпадает с расчётной 696.7`
- **Причина:** `22%` отсутствовала в `_VAT_RATE_MAP` → `_parse_vat("22%") = None` → `vat_dec = 0` → expected = qty × price без НДС
- **Исправление 1:** Добавлено `"22%": Decimal("0.22")` в `_VAT_RATE_MAP`
- **Исправление 2:** Введён флаг `vat_unknown = True` когда ставка есть в документе но отсутствует в карте — в этом случае предупреждение о несовпадении суммы **не генерируется**: сумма из документа (столбец «Стоимость с налогом») принимается как авторитетная

**Баг 2 — вторая страница накладной не объединяется с первой:**
- **Симптом:** Двухстраничная накладная (Лист 1 / Лист 2) появлялась как два отдельных документа
- **Причина:** «Лист 2» не содержит шапку с doc_number → `group_key` не строился → страница уходила в `single_*`
- **Исправление:** Двухпроходная группировка:
  - Проход 1: все страницы с явным `group_key` + страница 1 без ключа (как раньше)
  - Проход 2: «осиротевшие» страницы 2+ без `group_key` → сопоставление по `supplier_inn + date`
  - Если совпадение найдено → присоединяем к существующей группе
  - Если нет совпадения → создаём новый одиночный документ + `logger.warning`

**Тесты:** 67 ✅

---

### 2026-02-19 — OCR → iiko: автоматическая отправка приходных накладных

**Цель:** После завершения маппинга бухгалтер видит превью приходных накладных и одной кнопкой отправляет их в iiko (POST XML).

**Новые файлы:**
- `use_cases/incoming_invoice.py` — полная бизнес-логика подготовки и отправки накладных:
  `get_pending_ocr_documents`, `build_iiko_invoices`, `send_invoices_to_iiko`,
  `format_invoice_preview`, `format_send_result`, `mark_documents_imported`, `mark_documents_cancelled`,
  `_load_product_units` (main_unit UUID для amountUnit), `_load_supplier_ids_from_db`
- `tests/test_store_type.py` — 26 тестов для `_normalize_store_type` + `extract_store_type`
- `tests/test_incoming_invoice.py` — 23 теста для XML, превью, результатов
- `tests/test_build_invoices.py` — 9 async-тестов `build_iiko_invoices` (mocked DB)
- `pytest.ini` — `asyncio_mode = auto`, `testpaths = tests`

**Изменённые файлы:**
- `use_cases/product_request.py` — добавлены `_STORE_TYPE_ALIASES` + `_normalize_store_type()`:
  «Хоз. товары (Московский)»→«хозы», «ТМЦ Сельма»→«тмц», маппинг по prefix
- `bot/document_handlers.py`:
  - `_save_ocr_document` теперь сохраняет `supplier_id`
  - `_handle_mapping_done` показывает превью накладных + кнопки «📤 Отправить в iiko» / «❌ Отменить»
  - Добавлены callback-хендлеры `cb_iiko_invoice_send` и `cb_iiko_invoice_cancel`
  - `_pending_invoices: dict[int, list[dict]]` для in-memory хранения до подтверждения

**Ключевые решения:**
- Один OcrDocument → N iiko-накладных (по одной на каждый store_type)
- Номер накладной: «УПД-1-КУХ», «УПД-1-БАР» при нескольких складах
- `main_unit` (UUID из таблицы iiko_product) передаётся как `amountUnit` в XML
- Fallback: если supplier_id не найден в документе — ищем в DB по имени

**Тесты:** 67 тестов, все ✅

---

### 2026-02-18 — OCR накладных: двухтабличный маппинг OCR→iiko

**Цель:** Бухгалтер загружает фото накладных → бот распознаёт → классифицирует → кладёт незамапленные позиции в «Маппинг Импорт» → бухгалтер выбирает из dropdown → «✅ Маппинг готов» → данные уходят в «Маппинг» (база).

**Новые файлы:**
- `use_cases/ocr_mapping.py` — полная логика двухтабличного маппинга:
  `get_base_mapping`, `apply_mapping`, `write_transfer`, `check_transfer_ready`, `finalize_transfer`, `notify_accountants`
- `bot/document_handlers.py` — новый FSM-поток OCR:
  `OcrStates.waiting_photos` → debounce → classify → apply_mapping → write_transfer → summary
  `btn_mapping_done` → check_transfer_ready → finalize_transfer → clear import

**Изменённые файлы:**
- `adapters/google_sheets.py` — добавлены функции маппинговых вкладок:
  `read_base_mapping_sheet`, `write_mapping_import_sheet` (с ONE_OF_LIST dropdown),
  `read_mapping_import_sheet`, `upsert_base_mapping`, `clear_mapping_import_sheet`
- `bot/_utils.py` — `ocr_keyboard()`: кнопки «📤 Загрузить накладные» + «✅ Маппинг готов»
- `bot/global_commands.py` — `NAV_BUTTONS` обновлён (убрано старое, добавлены 2 новые кнопки)
- `models/ocr.py` — `OcrItem` получил поля `iiko_id` и `iiko_name`
- `db/init_db.py` — миграция `ADD COLUMN IF NOT EXISTS iiko_id / iiko_name` в `ocr_item`

**Удалены (очистка):**
- 16 файлов: `test_incoming_service.py`, `test_ocr_*.py`, `test_openai_ocr.py`,
  `get_iam_token.py`, `run_new_photo_ocr.py`, `build_final_report.py`,
  `photo_test_ocr_results.json`, `photo_test_ocr_merged.json`, `photo_test_final_report.json`,
  `ocr_results.json`, `ocr_results_merged.json`, `16_02_2026_*.json`, `docs (4).json`,
  папки `tests/` (частичная очистка), `photo_test/`

**Архитектура GSheet:**
- `MIN_STOCK_SHEET_ID`: две вкладки «Маппинг» и «Маппинг Импорт»
- «Маппинг» (база): A=тип, B=OCR-имя, C=iiko-имя, D=iiko-id — постоянная, только UPSERT
- «Маппинг Импорт» (трансфер): те же столбцы, col C = dropdown ONE_OF_LIST из БД (макс 500)
  — записывается при загрузке, очищается после finalize_transfer

**Классификация doc_type:**
- `rejected_qr` → пропускаем
- `cash_order` / `act` без суммы → услуга → notify_accountants
- `upd` / `act` с суммой → накладная → apply_mapping → write_transfer если незамапленные

---

> **Архив:** записи до 2026-02-18 → [docs/archive/CHANGELOG_OLD.md](archive/CHANGELOG_OLD.md)
