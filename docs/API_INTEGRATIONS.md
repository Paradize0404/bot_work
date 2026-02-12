# 🔌 API-интеграции, инфраструктура и оптимизации

> Читай этот файл при: новый sync, работа с внешним API, проблемы производительности, инфраструктура Railway.

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
| `fetch_product_groups()` | `/resto/api/v2/entities/products/group/list` | JSON | — |
| `send_writeoff(xml_body)` | `/resto/api/documents/writeoff/outgoing` | XML POST | — (без retry) |
| `fetch_incoming_invoices(from, to)` | `/resto/api/documents/export/incomingInvoice` | XML | `from`, `to` (YYYY-MM-DD) |
| `fetch_assembly_charts(from, to)` | `/resto/api/v2/assemblyCharts/getAll` | JSON | `dateFrom`, `dateTo`, `includePreparedCharts` |

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

---

## ⚠️ Особенности инфраструктуры (Railway)

- **PostgreSQL на Railway** — удалённая БД, **высокая сетевая задержка ~400мс на round-trip**
- Из-за этого: batch INSERT критичен (500 строк = 1 round-trip вместо 500)
- `pool_recycle=300` — Railway дропает idle-соединения
- `jit=off` — PostgreSQL JIT бесполезен для коротких OLTP-запросов
- Первое подключение может занять ~30 сек (cold start Railway)

---

## ⚡ Оптимизации производительности

Все оптимизации направлены на минимизацию round-trip к Railway PostgreSQL (~400мс каждый).

### Принципы
1. **Параллельные независимые запросы** — `asyncio.gather()` для любых 2+ async-вызовов без зависимости
2. **JOIN вместо N+1** — одним SQL-запросом вместо последовательных SELECT
3. **Параллельный API + DB** — внешний HTTP-запрос одновременно с чтением справочников из БД
4. **Фоновая синхронизация** — `asyncio.create_task()` для операций, которые не блокируют пользователя

### Применённые оптимизации

| Файл | Было | Стало | Экономия |
|------|------|-------|----------|
| `user_context.py` | 3 последовательных SELECT (Employee → Department → EmployeeRole) | 1 JOIN-запрос (outerjoin + aliased) | **−800мс** (2 round-trip) |
| `handlers.py` → `btn_check_min_stock` | `sync_products` → `sync_stock_balances` последовательно | `asyncio.gather(sync_products, sync_stock_balances)` | **−3-5 сек** (параллельно) |
| `check_min_stock.py` | stores → departments → остатки последовательно | `asyncio.gather(stores_task, dept_task)` → остатки | **−400мс** (1 round-trip) |
| `sync_stock_balances.py` | API fetch → `_load_name_maps` (DB) последовательно | `asyncio.gather(API, name_maps)` параллельно | **−300-500мс** (перекрытие I/O) |
| `writeoff.py` → `start_writeoff` | `get_stores` → `is_admin` последовательно | `asyncio.gather(get_stores, is_admin)` | **−400мс** |
| `writeoff.py` → `search_products` | N запросов единиц измерения | batch JOIN с `iiko_entity` (MeasureUnit) | **−(N−1)×400мс** |
| `handlers.py` → `btn_documents_menu` | синхронизация блокирует UI | `asyncio.create_task()` — фоновая | **0мс для пользователя** |

### Полный список оптимизаций

- **In-memory UserContext кеш** — dict `{telegram_id: UserContext}` в RAM, ~10 КБ, ленивая загрузка
- **Persistent httpx client (iiko)** — 1 TCP/TLS-соединение, connection pool до 20
- **Persistent httpx client (FinTablo)** — отдельный client с Bearer token, keep-alive pool
- **Retry iiko GET с backoff** — `_get_with_retry()`: 3 попытки, задержки 1→3→7 сек. Ловит `RemoteProtocolError`, `ConnectError`, `ReadTimeout`, `ConnectTimeout`, `PoolTimeout`. POST (send_writeoff) без retry намеренно.
- **asyncio.Semaphore(4) для FinTablo** — макс 4 параллельных запроса (rate limit 300 req/min)
- **Retry с exponential backoff (FT)** — при 429 Too Many Requests (2с → 4с → 8с → 16с → 32с)
- **Batch INSERT** — до 500 строк в одном INSERT ... ON CONFLICT DO UPDATE
- **asyncio.gather** — параллельные API-запросы (16 iiko справочников, 13 FinTablo)
- **SyncLog в той же сессии** — 0 лишних round-trip
- **pool_recycle=300** — переподключение к Railway каждые 5 мин
- **jit=off** — быстрее планирование batch INSERT в PostgreSQL
- **DRY: общие хелперы в sync.py** — `_batch_upsert()`, `_mirror_delete()` и `_safe_decimal()` переиспользуются в sync_fintablo.py
- **Mirror-sync** — после каждого UPSERT: `DELETE WHERE id NOT IN (ids из API)`. Одна транзакция (upsert + delete + sync_log). Безопасность: пустой набор ID → skip (защита от сбоя API)
- **Токен iiko кешируется** на 10 мин с retry×4
- **TTL-кеш writeoff** — склады/счета 10 мин, ед. измерения 30 мин (writeoff_cache.py)
- **Фоновая синхронизация при открытии Документов** — `sync_products()` + `sync_all_entities()` параллельно через `asyncio.gather`
- **Фоновый прогрев кеша** — `preload_for_user()` через `asyncio.create_task` (склады + счета + admin_ids в RAM)
- **FSM-кеш** — `_stores_cache`, `_accounts_cache` в FSM state.data (0 запросов при пагинации)
- **Фильтрация счетов** — 142 → 3–5 через SQL фильтр ("списание" + бар/кухня)
- **callback.answer() первым** — мгновенный отклик на кнопку, потом логика
- **try_lock/unlock** — конкурентная блокировка документов (один админ за раз)
- **Admin IDs из БД** — `bot_admin` таблица с in-memory кешем, инвалидация при CRUD
- **Параллельный старт списания** — `get_stores_for_department()` + `is_admin()` через `asyncio.gather` (−400мс)
- **Batch-resolve единиц измерения** — `search_products()` резолвит через JOIN Entity в одном SQL
- **Pre-warm admin_ids** — `preload_for_user()` прогревает `get_admin_ids()` параллельно со складами
- **Полное логирование действий** — каждый handler логирует при входе: `[module] действие tg:USER_ID, params`

---

## 🐛 Известные грабли (решены, но помнить)

| Проблема | Причина | Решение |
|----------|---------|---------|
| Медленный старт бота (13 сек) | `create_all` на каждый запуск по удалённой БД | `SELECT 1` health check |
| Минута на 200 записей | Каждый INSERT = отдельный round-trip | Batch по 500 строк |
| Половина записей "невалидный UUID" | XML `iter()` находит вложенные одноимённые теги | `findall()` — только прямые дочерние |
| Справочники 40 сек | 16 последовательных fetch + 16 отдельных COMMIT | `asyncio.gather` + 1 COMMIT |
| httpx пересоздаёт TCP | Новый `AsyncClient` на каждый запрос | Persistent client с connection pool |
| iiko `Server disconnected` | Транзиентные сетевые ошибки при GET | `_get_with_retry()` — 3 попытки, backoff 1→3→7 сек |
| FinTablo бесконечная пагинация | `_fetch_list` циклил `?page=N`, но API отдаёт ВСЁ за 1 запрос | Убрана пагинация, 1 GET = все записи |
| FinTablo 429 Too Many Requests | 13 параллельных задач × бесконечный цикл = 500+ req/min | Semaphore(4) + retry с exp. backoff |
