# 🗄 База данных PostgreSQL (Railway)

> Читай этот файл при: миграция, новая таблица, sync-задача, работа с данными, запросы.

**Подключение:** `postgresql+asyncpg://...@ballast.proxy.rlwy.net:17027/railway`
**Всего таблиц:** 46 (30 iiko/bot + 13 FinTablo + 1 служебная бота + 1 внешняя + 1 pending)

---

## 📇 Компактный индекс (все таблицы)

> Найди нужную таблицу → перейди к детальному описанию ниже.

| # | Таблица | Категория | Ключевые колонки | Sync |
|---|---------|-----------|-----------------|------|
| 1 | `iiko_entity` | iiko справочники | id (UUID), root_type (16 типов), name | UPSERT+mirror |
| 2 | `iiko_supplier` | iiko справочники | id (UUID PK), name, ИНН | UPSERT+mirror |
| 3 | `iiko_department` | iiko структура | id (UUID PK), parent_id, type | UPSERT+mirror |
| 4 | `iiko_store` | iiko структура | id (UUID PK), parent_id, name | UPSERT+mirror |
| 5 | `iiko_group` | iiko структура | id (UUID PK), name, parent_id | UPSERT+mirror |
| 6 | `iiko_product_group` | iiko номенклатура | id (UUID PK), name, parent_id, num_chd | UPSERT+mirror |
| 7 | `iiko_product` | iiko номенклатура | id (UUID PK), parent_id, type, num, unit | UPSERT+mirror |
| 8 | `iiko_employee` | iiko кадры | id (UUID PK), name, role_id | UPSERT+mirror |
| 9 | `iiko_employee_role` | iiko кадры | id (UUID PK), name, code | UPSERT+mirror |
| 10 | `iiko_sync_log` | аудит | entity, status, started_at, count | INSERT only |
| 11 | `bot_admin` | бот (legacy) | telegram_id (PK), name | ручной (deprecated→GSheet) |
| 12 | `iiko_stock_balance` | остатки | product_id, store_id, amount | full-replace |
| 13 | `min_stock_level` | остатки | product_id, department_id, min/max_qty | GSheet sync |
| 14 | `gsheet_export_group` | настройки | group_id (UUID PK), group_name | GSheet sync |
| 15 | `ft_category` | FinTablo | ext_id (PK), name, parent_id | UPSERT+mirror |
| 16 | `ft_moneybag` | FinTablo | ext_id (PK), name, currency | UPSERT+mirror |
| 17 | `ft_partner` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 18 | `ft_direction` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 19 | `ft_moneybag_group` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 20 | `ft_goods` | FinTablo | ext_id (PK), name, unit | UPSERT+mirror |
| 21 | `ft_obtaining` | FinTablo | ext_id (PK), partner, amount, date | UPSERT+mirror |
| 22 | `ft_job` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 23 | `ft_deal` | FinTablo | ext_id (PK), partner, amount, date | UPSERT+mirror |
| 24 | `ft_obligation_status` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 25 | `ft_obligation` | FinTablo | ext_id (PK), partner, amount | UPSERT+mirror |
| 26 | `ft_pnl_category` | FinTablo | ext_id (PK), name, parent_id | UPSERT+mirror |
| 27 | `ft_employee` | FinTablo | ext_id (PK), name | UPSERT+mirror |
| 28 | `writeoff_history` | списания | doc_id (UUID PK), dept, items (JSONB) | INSERT |
| 29 | `invoice_template` | накладные | id (PK), name, dept_id, items (JSONB) | INSERT |
| 30 | `request_receiver` | заявки (legacy) | telegram_id (PK), name | ручной (deprecated→GSheet) |
| 31 | `product_request` | заявки | id (PK), dept_id, status, items (JSONB) | INSERT |
| 32 | `active_stoplist` | стоп-лист | product_id+dept_id (PK), name | full-replace |
| 33 | `stoplist_message` | стоп-лист | chat_id+dept_id (PK), message_id | UPDATE |
| 34 | `stoplist_history` | стоп-лист | id (PK), product_id, entered_at, exited_at | INSERT/UPDATE |
| 35 | `price_product` | прайс-лист | id (PK), product_id, store_id, name | GSheet sync |
| 36 | `price_supplier_column` | прайс-лист | id (PK), supplier_name, col_index | GSheet sync |
| 37 | `price_supplier_price` | прайс-лист | product_id+supplier_id (PK), price | GSheet sync |
| 38 | `stock_alert_message` | остатки | chat_id+dept_id (PK), message_id | UPDATE |
| 42 | `iiko_access_tokens` | внешний | org_id (PK), token, expires_at | INSERT/UPDATE |
| 43 | `pending_writeoff` | списания | id (UUID PK), dept, items, is_locked, TTL 24h | INSERT/UPDATE |
| 44 | `pastry_nomenclature_group` | кондитерка | id (UUID PK), name | INSERT/DELETE |
| 45 | `writeoff_request_store_group` | списания | id (UUID PK), name | INSERT/DELETE |
| 46 | `salary_history` | ФОТ | id (PK), employee_name, sal_type, rate, mot_pct, mot_base, valid_from, valid_to, iiko_id | GSheet sync |
| 47 | `salary_exclusions` | ФОТ | employee_id (PK), excluded_by, excluded_at | INSERT/DELETE |
| 48 | `pending_incoming_invoice` | накладные | id (PK), owner_tg_id, invoices (JSONB), created_at | INSERT/UPDATE |

---

## Таблицы iiko / бота (25)

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

### 6. `iiko_product_group` — Номенклатурные группы (иерархия товаров)

Кнопка бота: **📁 Ном.группы** (в составе «🔄 ВСЁ iiko»)
Источник API: `GET /resto/api/v2/entities/products/group/list` (JSON)

| Колонка               | Тип          | Описание                            |
|-----------------------|--------------|-------------------------------------|
| `id`                  | UUID PK      | ID группы                           |
| `parent_id`           | UUID         | Родительская группа (index)         |
| `name`                | String(500)  | Название                             |
| `code`                | String(200)  | Код                                  |
| `num`                 | String(200)  | Артикул                              |
| `description`         | Text         | Описание                             |
| `deleted`             | Boolean      | Удалена                              |
| `category`            | UUID         | Категория                            |
| `accounting_category` | UUID         | Бухгалтерская категория             |
| `tax_category`        | UUID         | Налоговая категория                 |
| `synced_at`           | DateTime     | Время синхронизации                 |
| `raw_json`            | JSONB        | Полный ответ API                    |

---

### 7. `iiko_product` — Номенклатура

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

### 8. `iiko_employee` — Сотрудники

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

### 9. `iiko_employee_role` — Должности

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

### 10. `iiko_sync_log` — Лог синхронизаций (аудит)

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

### 11. `bot_admin` — Администраторы бота

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

### 12. `iiko_stock_balance` — Остатки по складам

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

---

### 13. `min_stock_level` — Мин/макс остатки (из Google Таблицы)

Источник истины: **Google Таблица** (синхронизируется кнопкой «📥 Мин. остатки GSheet → БД»).

| Колонка           | Тип            | Описание                                     |
|-------------------|----------------|----------------------------------------------|
| `pk`              | BigInteger PK  | Суррогатный автоинкремент                    |
| `product_id`      | UUID           | UUID товара → iiko_product.id (index)        |
| `product_name`    | String(500)    | Название товара (денормализовано)             |
| `department_id`   | UUID           | UUID ресторана → iiko_department.id (index)  |
| `department_name` | String(500)    | Название ресторана (денормализовано)          |
| `min_level`       | Numeric(15,4)  | Минимальный остаток                          |
| `max_level`       | Numeric(15,4)  | Максимальный остаток                         |
| `updated_at`      | DateTime       | Время последнего обновления                  |

**Unique constraint:** `uq_min_stock_product_dept` на `(product_id, department_id)`

---

### 14. `gsheet_export_group` — Корневые группы для экспорта в GSheet

Определяет, какие ветки дерева номенклатуры попадают в Google Таблицу.
При синхронизации: BFS-обход потомков всех корневых групп → фильтр товаров.

| Колонка       | Тип            | Описание                                      |
|---------------|----------------|-----------------------------------------------|
| `pk`          | BigInteger PK  | Суррогатный автоинкремент                     |
| `group_id`    | UUID           | UUID группы из iiko_product_group (unique)    |
| `group_name`  | String(500)    | Название группы (денормализовано)              |
| `added_at`    | DateTime       | Когда добавлена (server_default=now())        |

> Инициализация: INSERT INTO gsheet_export_group (group_id, group_name) VALUES ('54e7c5ab-...', 'Товары');

---

### 14b. `writeoff_request_store_group` — Разрешённые папки для списания на точке-получателе

Аналог `gsheet_export_group`, применяется **только** к подразделению, выбранному как точка-получатель заявок.
При поиске товаров для списания: BFS-обход потомков → GOODS из этих папок + все PREPARED.
Для всех остальных точек используется `gsheet_export_group`.
Если таблица пуста → отображаются только PREPARED.

| Колонка       | Тип            | Описание                                      |
|---------------|----------------|-----------------------------------------------|
| `pk`          | BigInteger PK  | Суррогатный автоинкремент                     |
| `group_id`    | UUID           | UUID группы из iiko_product_group (unique)    |
| `group_name`  | String(500)    | Название группы (денормализовано)              |
| `added_at`    | DateTime       | Когда добавлена (server_default=now())        |

> Инициализация: INSERT INTO writeoff_request_store_group (group_id, group_name) VALUES ('uuid-здесь', 'Товары ЦК');

---

## Таблицы FinTablo (13 таблиц, префикс `ft_`)

Все таблицы FinTablo имеют общие поля от `FTSyncMixin`:
- `synced_at` (DateTime) — время последней синхронизации
- `raw_json` (JSONB) — полный JSON из API (для дебага)

Все PK — `BigInteger` (ID из FinTablo, `autoincrement=False`).

---

### 15. `ft_category` — Статьи ДДС

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

### 16. `ft_moneybag` — Счета

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

### 17. `ft_partner` — Контрагенты

Источник API: `GET /v1/partner`

| Колонка    | Тип           | Описание                   |
|------------|---------------|----------------------------|
| `id`       | BigInteger PK | ID из FinTablo             |
| `name`     | String(500)   | Название                   |
| `inn`      | String(50)    | ИНН                        |
| `group_id` | BigInteger    | ID группы контрагентов (index) |
| `comment`  | Text          | Комментарий                |

---

### 18. `ft_direction` — Направления

Источник API: `GET /v1/direction`

| Колонка       | Тип           | Описание                   |
|---------------|---------------|----------------------------|
| `id`          | BigInteger PK | ID из FinTablo             |
| `name`        | String(500)   | Название                   |
| `parent_id`   | BigInteger    | Родитель (index)           |
| `description` | Text          | Описание                   |
| `archived`    | Integer       | 1 = архивное               |

---

### 19. `ft_moneybag_group` — Группы счетов

Источник API: `GET /v1/moneybag-group`

| Колонка       | Тип           | Описание                   |
|---------------|---------------|----------------------------|
| `id`          | BigInteger PK | ID из FinTablo             |
| `name`        | String(500)   | Название                   |
| `is_built_in` | Integer       | 1 = системная              |

---

### 20. `ft_goods` — Товары

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

### 21. `ft_obtaining` — Закупки

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

### 22. `ft_job` — Услуги

Источник API: `GET /v1/job`

| Колонка        | Тип            | Описание                  |
|----------------|----------------|---------------------------|
| `id`           | BigInteger PK  | ID из FinTablo            |
| `name`         | String(500)    | Название                  |
| `cost`         | Numeric(15,2)  | Стоимость                 |
| `comment`      | Text           | Комментарий               |
| `direction_id` | BigInteger     | ID направления (index)    |

---

### 23. `ft_deal` — Сделки

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

### 24. `ft_obligation_status` — Статусы обязательств

Источник API: `GET /v1/obligation-status`

| Колонка | Тип           | Описание                   |
|---------|---------------|----------------------------|
| `id`    | BigInteger PK | ID из FinTablo             |
| `name`  | String(500)   | Название                   |

---

### 25. `ft_obligation` — Обязательства

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

### 26. `ft_pnl_category` — Статьи Прибылей и Убытков

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

### 27. `ft_employee` — Сотрудники FinTablo

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

### 28. `writeoff_history` — История списаний

Источник: бот (сохраняется при одобрении акта админом или прямой отправке)

| Колонка           | Тип           | Описание                                                |
|-------------------|---------------|---------------------------------------------------------|
| `pk`              | BigInteger PK | Автоинкремент                                           |
| `telegram_id`     | BigInteger    | Telegram ID автора (index)                              |
| `employee_name`   | String(500)   | ФИО автора (денормализовано)                             |
| `department_id`   | UUID          | Подразделение (index)                                   |
| `store_id`        | UUID          | Склад                                                   |
| `store_name`      | String(500)   | Название склада (денормализовано)                        |
| `account_id`      | UUID          | Счёт списания                                           |
| `account_name`    | String(500)   | Название счёта (денормализовано)                         |
| `reason`          | String(500)   | Причина списания                                        |
| `items`           | JSONB         | Позиции: [{id, name, quantity, user_quantity, ...}]     |
| `store_type`      | String(20)    | Тип склада: 'bar' / 'kitchen' / NULL (index)            |
| `approved_by_name`| String(500)   | ФИО одобрившего админа                                  |
| `created_at`      | DateTime      | Дата создания (index)                                   |

**Фильтрация по ролям:** бар видит только bar, кухня — kitchen, админ — всё по подразделению.
**Лимит:** до 200 записей на пользователя (старые удаляются автоматически).

---

### 29. `invoice_template` — Шаблоны расходных накладных

Источник: бот (создаётся пользователем через FSM)

| Колонка            | Тип           | Описание                                                |
|--------------------|---------------|---------------------------------------------------------|
| `pk`               | BigInteger PK | Автоинкремент                                           |
| `name`             | String(200)   | Название шаблона                                        |
| `created_by`       | BigInteger    | Telegram ID автора (index)                              |
| `department_id`    | UUID          | Подразделение (index)                                   |
| `counteragent_id`  | UUID          | ID контрагента (поставщика)                             |
| `counteragent_name`| String(500)   | Название контрагента (денормализовано)                   |
| `account_id`       | UUID          | ID счёта реализации                                     |
| `account_name`     | String(500)   | Название счёта (денормализовано)                         |
| `store_id`         | UUID          | ID склада                                               |
| `store_name`       | String(500)   | Название склада (денормализовано)                        |
| `items`            | JSONB         | Позиции: [{product_id, name, unit_name}]                |
| `created_at`       | DateTime      | Дата создания                                           |

**Индексы:** `ix_invoice_template_created_by`, `ix_invoice_template_dept`
**Использование:** при создании накладной по шаблону — подставляются все поля, пользователь вводит только количества.

---

### 30. `request_receiver` — Получатели заявок на товары

Источник: бот (назначается админом из авторизованных сотрудников)

| Колонка          | Тип           | Описание                                                |
|--------------------|---------------|----------------------------------------------------------|
| `id`               | BigInteger PK | Автоинкремент                                          |
| `telegram_id`      | BigInteger    | Telegram user ID получателя (unique, index)           |
| `employee_id`      | UUID          | FK → iiko_employee.id                                   |
| `employee_name`    | String(500)   | ФИО (денормализовано)                                  |
| `added_at`         | DateTime      | Дата добавления                                         |
| `added_by`         | BigInteger    | telegram_id того, кто добавил                          |

**Индексы:** `ix_request_receiver_telegram_id`
**Использование:** аналог BotAdmin — получатели уведомлений о новых заявках. Кешируется в `_receiver_ids_cache`.

---

### 31. `product_request` — Заявки на товары

Источник: бот (создаётся сотрудником через FSM)

| Колонка            | Тип           | Описание                                                |
|----------------------|---------------|----------------------------------------------------------|
| `pk`                 | BigInteger PK | Автоинкремент                                          |
| `status`             | String(20)    | pending / approved / cancelled (index)                   |
| `requester_tg`       | BigInteger    | Telegram ID создателя (index)                        |
| `requester_name`     | String(500)   | ФИО создателя (денормализовано)                     |
| `department_id`      | UUID          | Подразделение (index)                                 |
| `department_name`    | String(500)   | Название подразделения                                 |
| `store_id`           | UUID          | Склад-источник (бар/кухня)                            |
| `store_name`         | String(500)   | Название склада                                          |
| `counteragent_id`    | UUID          | UUID контрагента / поставщика                       |
| `counteragent_name`  | String(500)   | Название контрагента                                   |
| `account_id`         | UUID          | Счёт реализации                                        |
| `account_name`       | String(500)   | Название счёта                                            |
| `items`              | JSONB         | Позиции: [{product_id, name, amount, price, unit_name, ...}] |
| `total_sum`          | Numeric(15,2) | Общая сумма заявки                                     |
| `comment`            | Text          | Комментарий                                              |
| `approved_by`        | BigInteger    | Telegram ID одобрившего/отклонившего               |
| `created_at`         | DateTime      | Дата создания                                          |
| `approved_at`        | DateTime      | Дата одобрения                                         |

**Индексы:** `ix_product_request_requester`, `ix_product_request_dept`, `ix_product_request_status`
**Жизненный цикл:** pending → approved (получатель отправил накладную) / cancelled.

---

### 32. `active_stoplist` — Текущий стоп-лист (зеркало iikoCloud)

Источник: iikoCloud API `/api/1/stop_lists` + StopListUpdate вебхук

| Колонка              | Тип            | Описание                                                |
|----------------------|----------------|----------------------------------------------------------|
| `pk`                 | BigInteger PK  | Автоинкремент                                           |
| `product_id`         | String(36)     | UUID товара из iikoCloud (index)                        |
| `name`               | String(500)    | Название товара (из iiko_product)                      |
| `balance`            | Numeric(15,4)  | Остаток (0 = полный стоп)                               |
| `terminal_group_id`  | String(36)     | UUID терминальной группы (index)                        |
| `organization_id`    | String(36)     | UUID организации iikoCloud                              |
| `updated_at`         | DateTime       | Время последнего обновления                             |

**Unique constraint:** `uq_active_stoplist_product_tg` на `(product_id, terminal_group_id)`
**Индексы:** `ix_active_stoplist_product_id`, `ix_active_stoplist_tg_id`, `ix_active_stoplist_org_id`

---

### 33. `stoplist_message` — Закреплённые сообщения со стоп-листом

Источник: бот (аналог stock_alert_message, но для стоп-листа)

| Колонка          | Тип           | Описание                                                |
|------------------|---------------|----------------------------------------------------------|
| `pk`             | BigInteger PK | Автоинкремент                                           |
| `chat_id`        | BigInteger    | Telegram chat_id = user_id (unique, index)             |
| `message_id`     | BigInteger    | ID закреплённого сообщения                              |
| `snapshot_hash`  | String(64)    | SHA-256 хеш данных (дедупликация обновлений)          |
| `updated_at`     | DateTime      | Время последнего обновления                             |

**Unique constraint:** `uq_stoplist_message_chat` на `(chat_id)`
**Индексы:** `ix_stoplist_message_chat_id`

---

### 34. `stoplist_history` — История стоп-листа (вход/выход из стопа)

Источник: бот (заполняется при diff_and_update)

| Колонка              | Тип           | Описание                                                |
|----------------------|---------------|----------------------------------------------------------|
| `pk`                 | BigInteger PK | Автоинкремент                                           |
| `product_id`         | String(36)    | UUID товара (index)                                     |
| `name`               | String(500)   | Название товара (денормализовано)                       |
| `terminal_group_id`  | String(36)    | UUID терминальной группы (index)                        |
| `started_at`         | DateTime      | Время входа в стоп                                      |
| `ended_at`           | DateTime      | Время выхода из стопа (NULL = ещё в стопе)             |
| `duration_seconds`   | Integer       | Длительность в стопе (сек), заполняется при ended_at   |
| `date`               | DateTime      | Дата дня (для фильтрации отчётов, index)               |

**Индексы:** `ix_stoplist_history_product_id`, `ix_stoplist_history_tg_id`, `ix_stoplist_history_date`
**Использование:** ежевечерний отчёт (22:00) — суммарное время в стопе за день по каждому товару.

---

### 35. `price_product` — Товар в прайс-листе

Источник: Google Таблица → синхронизация в БД

| Колонка        | Тип           | Описание                                              |
|----------------|---------------|-------------------------------------------------------|
| `pk`           | BigInteger PK | Автоинкремент                                         |
| `product_id`   | UUID          | UUID товара (→ iiko_product.id), unique, index        |
| `product_name` | String(500)   | Название товара                                       |
| `product_type` | String(50)    | GOODS / DISH                                          |
| `cost_price`   | Numeric(15,4) | Себестоимость (авто, из приходов/техкарт)             |
| `main_unit`    | UUID          | UUID единицы измерения (→ iiko_entity MeasureUnit)    |
| `unit_name`    | String(100)   | Название единицы (денормализовано, дефолт «шт»)       |
| `synced_at`    | DateTime      | Время последней синхронизации (auto)                  |

**Индексы:** `uq_price_product_product_id` (unique)
**Использование:** прайс-лист расходных накладных, формирование PDF, подбор цен.

---

### 36. `price_supplier_column` — Поставщик в прайс-листе (столбец GSheet)

Источник: Google Таблица → синхронизация в БД

| Колонка         | Тип           | Описание                                           |
|-----------------|---------------|-----------------------------------------------------|
| `pk`            | BigInteger PK | Автоинкремент                                       |
| `supplier_id`   | UUID          | UUID поставщика (→ iiko_supplier.id), unique, index |
| `supplier_name` | String(500)   | Название поставщика                                 |
| `column_index`  | Integer       | Индекс столбца в GSheet (0–9)                      |
| `synced_at`     | DateTime      | Время последней синхронизации (auto)                |

**Индексы:** `uq_price_supplier_column_supplier_id` (unique)
**Использование:** маппинг поставщиков ↔ столбцов прайс-листа в Google Таблице.

---

### 37. `price_supplier_price` — Цена товара у поставщика

Источник: Google Таблица (ручной ввод) → синхронизация в БД

| Колонка       | Тип           | Описание                                              |
|---------------|---------------|-------------------------------------------------------|
| `pk`          | BigInteger PK | Автоинкремент                                         |
| `product_id`  | UUID          | UUID товара (→ price_product.product_id), index       |
| `supplier_id` | UUID          | UUID поставщика (→ price_supplier_column.supplier_id) |
| `price`       | Numeric(15,4) | Цена отгрузки (ручная, из GSheet), дефолт 0           |
| `synced_at`   | DateTime      | Время последней синхронизации (auto)                  |

**Unique constraint:** `uq_price_product_supplier` (product_id + supplier_id)
**Использование:** расходные накладные — подстановка цены по товару и поставщику.

---

### 38. `stock_alert_message` — Закреплённые сообщения с остатками

Источник: бот (трекинг pinned-сообщений, аналог stoplist_message)

| Колонка         | Тип           | Описание                                             |
|-----------------|---------------|-------------------------------------------------------|
| `pk`            | BigInteger PK | Автоинкремент                                         |
| `chat_id`       | BigInteger    | Telegram chat_id (= user_id для личных чатов), index  |
| `message_id`    | BigInteger    | ID закреплённого сообщения с остатками                |
| `snapshot_hash` | String(64)    | SHA-256 хеш последних данных (дельта-сравнение)       |
| `updated_at`    | DateTime      | Время последнего обновления (auto)                    |

**Unique constraint:** `uq_stock_alert_chat` (chat_id)
**Использование:** edit-first паттерн — обновляем существующее сообщение, если hash изменился.

---

## Служебные таблицы бота (1)

### 43. `pending_writeoff` — Акты списания, ожидающие проверки админом

Источник: бот (writeoff flow). Хранятся в PostgreSQL, чтобы пережить рестарт бота.

| Колонка          | Тип           | Описание                                              |
|------------------|---------------|-------------------------------------------------------|
| `doc_id`         | String(16) PK | Уникальный hex-ID документа                          |
| `created_at`     | DateTime      | Время создания (Калининград)                          |
| `author_chat_id` | BigInteger    | Telegram chat_id автора (index)                       |
| `author_name`    | String(500)   | ФИО автора                                             |
| `store_id`       | String(36)    | UUID склада                                            |
| `store_name`     | String(500)   | Название склада                                        |
| `account_id`     | String(36)    | UUID счёта списания                                    |
| `account_name`   | String(500)   | Название счёта                                         |
| `reason`         | Text          | Причина списания                                       |
| `department_id`  | String(36)    | UUID подразделения                                     |
| `items`          | JSONB         | Позиции: [{id, name, quantity, user_quantity, unit_label, main_unit}] |
| `admin_msg_ids`  | JSONB         | {admin_chat_id: message_id} для обновления кнопок     |
| `is_locked`      | Boolean       | True = документ обрабатывается админом (атомарный лок) |

**Индексы:** `ix_pending_writeoff_author`, `ix_pending_writeoff_created`
**TTL:** автоочистка документов старше 24 часов (при каждом `create()` / `all_pending()`).
**Конкурентность:** `UPDATE ... WHERE is_locked = false` — атомарный захват документа.
**Использование:** `use_cases/pending_writeoffs.py` — CRUD + lock/unlock.

### 42. `iiko_access_tokens` — Токены iikoCloud API (внешний cron)

Источник: **внешний cron-скрипт** (обновляет токен каждые 5 мин).
Таблица **не управляется** этим проектом (no ORM model, raw SQL query).
Используется в `adapters/iiko_cloud_api.py` → `get_cloud_token()`.

| Колонка | Тип | Описание |
|------------|-------------|----------------------------------------------------|
| `token` | String | Access token iikoCloud API |
| `created_at` | DateTime | Время создания токена |

**Запрос:** `SELECT token FROM iiko_access_tokens ORDER BY created_at DESC LIMIT 1`
**Важно:** если таблица пуста — `RuntimeError` при любом обращении к iikoCloud API.

---

### 44. `pastry_nomenclature_group` — Группы кондитерки

| Колонка      | Тип         | Описание                               |
|--------------|-------------|----------------------------------------|
| `id`         | UUID PK     | ID группы (из iiko)                    |
| `name`       | String(500) | Название группы                        |
| `created_at` | DateTime    | Время добавления                       |

### 45. `writeoff_request_store_group` — Группы складов для заявок на списание

| Колонка      | Тип         | Описание                               |
|--------------|-------------|----------------------------------------|
| `id`         | UUID PK     | ID группы (из iiko)                    |
| `name`       | String(500) | Название группы                        |
| `created_at` | DateTime    | Время добавления                       |

---

### 46. `salary_history` — История ставок сотрудников

GSheet-лист: **"История ставок"** (8 колонок)

| Колонка         | Тип           | Описание                                               |
|-----------------|---------------|--------------------------------------------------------|
| `id`            | Integer PK    | Суррогатный автоинкремент                              |
| `employee_name` | String(500)   | ФИО сотрудника (index)                                 |
| `sal_type`      | String(100)   | Тип расчёта: "Оклад" / "Почасовая" / "Сдельная"       |
| `rate`          | Numeric(15,4) | Ставка                                                 |
| `mot_pct`       | Numeric(6,2)  | Процент мотивации, nullable                            |
| `mot_base`      | String(200)   | База мотивации, nullable                               |
| `valid_from`    | Date          | Дата начала действия ставки                            |
| `valid_to`      | Date          | Дата окончания (NULL = актуальная), nullable           |
| `iiko_id`       | String(36)    | UUID сотрудника в iiko, nullable                       |

**GSheet-колонки:** A=Сотрудник, B=Тип расчёта, C=Ставка, D=Мотивация %, E=База мотивации, F=Дата с, G=Дата по (скрыта), H=iiko_id (скрыт)

**Логика:** при исключении через бот — строки удаляются (`delete_history_for_employee`); при удалении в iiko — `valid_to` закрывается `date.today()`.

**Использование:** `use_cases/salary_history.py`, `adapters/google_sheets.py`

---

### 47. `salary_exclusions` — Исключения из ФОТ

Кнопка бота: **👥 Список ФОТ** | Управление: `use_cases/salary.py`

| Колонка         | Тип           | Описание                                |
|-----------------|---------------|-----------------------------------------|
| `employee_id` | String(36) PK | UUID сотрудника в iiko                  |
| `excluded_by` | String(200)   | Telegram-ник/имя исключившего           |
| `excluded_at` | DateTime      | Время исключения                        |

**Логика:** сотрудник в таблице → не попадает в лист "Зарплаты". При добавлении → история **удаляется**. При снятии → история не восстанавливается.

**Использование:** `use_cases/salary.py`, `bot/salary_handlers.py`