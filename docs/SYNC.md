# 🔄 Синхронизация данных

> **Зависимости:** PROJECT_MAP.md (прочитан).  
> Если работа с таблицами → загрузи DATABASE.md.  
> Если API → загрузи API_INTEGRATIONS.md.

---

## Контракты домена

- **S1:** UPSERT-паттерн — INSERT ON CONFLICT DO UPDATE, батчами по 500. Пересмотреть когда: >100k записей за sync.
- **S2:** Mirror-sync — после UPSERT, DELETE записей, которых нет в API. БД = зеркало.
- **S3:** Mirror-delete sanity: не более 50% удалений за раз, иначе skip + warning.
- **S4:** SyncLog — каждая синхронизация записывается (entity, status, count, timing).
- **S5:** Sync-lock — `asyncio.Lock` per entity. Одна и та же sync не параллельно.

---

## Автосинхронизация (APScheduler)

| Время | Что | Модуль |
|-------|-----|--------|
| **07:00** | Полная sync: iiko справочники + FinTablo + остатки + GSheet min/max + ref-лист маппинга | `scheduler.py` |
| **22:00** | Ежевечерний отчёт стоп-листа | `stoplist_report.py` |
| **23:00** | Авто-перемещение расходных материалов (отрицательные остатки) | `negative_transfer.py` |

`misfire_grace_time = 3600с` (1 час) — если бот был недоступен, задача выполнится.

## 07:00 Sync — 6 шагов

1. iiko → БД (справочники, подразделения, номенклатура)
2. FinTablo → БД (13 таблиц)
3. Остатки по складам → БД
4. GSheet мин/макс → БД
5. БД номенклатура → GSheet «Мин остатки»
6. БД GOODS display-имена → GSheet «Маппинг Справочник»

## 23:00 Авто-перемещение расходных материалов

```
OLAP v1 остатки → фильтр (TopParent = «Расходные материалы», amount < 0)
  → internalTransfer (Хоз.товары → Бар/Кухня) по всем ресторанам
```

Авто-масштабирование: новый ресторан → новые склады «Хоз. товары (НОВЫЙ)» + «Бар (НОВЫЙ)» → перемещения автоматически.

### Пайплайн (7 шагов)

| Шаг | Что делается |
|-----|-------------|
| 1 | `_load_stores()` → `_build_restaurant_map()` — паттерн `TYPE (РЕСТОРАН)`, строится карта source→targets |
| 2 | `fetch_olap_transactions_v1(today, today)` — один OLAP-запрос для всех ресторанов |
| 3 | `_collect_negative_items()` — фильтр по целевым складам + TopParent + amount < 0. Сохраняет `measure_unit_name` из OLAP |
| 4 | `_load_products_by_name()` — **двухпроходный поиск**: проход 1 = точный `name.in_()`, проход 2 = `func.trim(name).in_()` для ненайденных (trailing spaces из REST API) |
| 5 | `_load_unit_id_by_name()` — **fallback единиц измерения**: для товаров с `main_unit=NULL` ищет UUID в `iiko_entity (MeasureUnit)` по имени из OLAP |
| 6 | POST `/v2/documents/internalTransfer` per restaurant×target_store |
| 7 | SyncLog запись |

### Ключевые функции

| Функция | Файл | Назначение |
|---------|------|------------|
| `run_negative_transfer_once()` | `negative_transfer.py` | Публичный API с Lock (вызывается scheduler и ручным триггером) |
| `_build_restaurant_map()` | `negative_transfer.py` | Паттерн `TYPE (РЕСТОРАН)` → карта source+targets |
| `_collect_negative_items()` | `negative_transfer.py` | Фильтрация OLAP-строк, сохраняет `measure_unit_name` |
| `_load_products_by_name()` | `negative_transfer.py` | Двухпроходный поиск по имени (exact + trim) |
| `_load_unit_id_by_name()` | `negative_transfer.py` | Fallback UUID единицы по имени из `iiko_entity` |
| `send_internal_transfer()` | `iiko_api.py` | POST перемещения в iiko |

### Конфигурация (env)

| Переменная | Дефолт | Назначение |
|-----------|--------|------------|
| `NEGATIVE_TRANSFER_SOURCE_PREFIX` | `Хоз. товары` | Имя склада-источника |
| `NEGATIVE_TRANSFER_TARGET_PREFIXES` | `Бар,Кухня` | CSV целевых складов |
| `NEGATIVE_TRANSFER_PRODUCT_GROUP` | `Расходные материалы` | TopParent фильтр |

### Когда нечего перемещать

- `nothing_to_transfer` — OLAP не нашёл отрицательных остатков на целевых складах в текущий день. Это нормально если перемещение уже было сделано ранее или расходников достаточно.
- Если **сам Хоз.товары в минусе** — авто-перемещение не поможет. Нужна накладная прихода в iiko на склад-источник.

---

## Generic sync-паттерн

```python
# use_cases/sync.py — шаблон для любой sync-операции
async def _run_sync(entity_name, fetch_fn, Model, mapping_fn, session):
    """
    1. fetch_fn() → raw data из API
    2. mapping_fn(item) → dict для UPSERT  
    3. _batch_upsert(session, Model, items, batch_size=500)
    4. _mirror_delete(session, Model, api_ids)
    5. Запись в SyncLog
    """
```

## Модули

| Модуль | Роль |
|--------|------|
| `use_cases/sync.py` | Generic _run_sync + _batch_upsert + _mirror_delete для iiko |
| `use_cases/sync_fintablo.py` | Sync FinTablo: 13 таблиц ft_* |
| `use_cases/sync_stock_balances.py` | Full-replace (DELETE + INSERT) остатков |
| `use_cases/sync_min_stock.py` | GSheet ↔ БД min_stock_level + номенклатура → GSheet |
| `use_cases/sync_lock.py` | asyncio.Lock per entity (acquire_nowait) |
| `use_cases/scheduler.py` | APScheduler: start/stop, misfire_grace_time |
| `use_cases/negative_transfer.py` | OLAP → отрицательные → internalTransfer |
| `adapters/iiko_api.py` | 11 fetch_* + send_writeoff + fetch_olap_* + send_internal_transfer |
| `adapters/fintablo_api.py` | FinTablo sync (persistent httpx) |

## Таблицы (ключевые для sync)

| Таблица | Тип sync | Источник |
|---------|----------|---------|
| `iiko_entity` | UPSERT + mirror | iiko REST (16 rootTypes) |
| `iiko_product` | UPSERT + mirror | iiko REST |
| `iiko_supplier` | UPSERT + mirror | iiko REST (XML) |
| `iiko_department` | UPSERT + mirror | iiko REST (XML) |
| `iiko_store` | UPSERT + mirror | iiko REST (XML) |
| `stock_balance` | Full-replace | iiko REST |
| `ft_*` (13 таблиц) | UPSERT + mirror | FinTablo REST |
| `iiko_sync_log` | INSERT only | Аудит |

## Частые ошибки

1. **`Server disconnected without sending a response`:** retry с _get_with_retry (3 попытки, backoff 1→3→7)
2. **Mirror-delete всех записей:** sanity check — не более 50% (S3)
3. **Параллельный sync:** sync-lock на `acquire_nowait()`, не `locked()` + `async with` (TOCTOU гонка, K2)
4. **datetime.now() в sync:** ВСЕГДА `now_kgd()` — серверное UTC ≠ Калининградское
5. **Авто-перемещение: «пропущено N товаров»** — две причины:
   - iiko REST API добавляет **trailing spaces** в `iiko_product.name`, OLAP возвращает имена без пробелов → `name.in_()` не находит. Решение: двухпроходный поиск с `func.trim()`.
   - `iiko_product.main_unit = NULL` — iiko не вернул `mainUnit` в REST ответе. Решение: fallback UUID через `_load_unit_id_by_name()` по имени из OLAP.
6. **Авто-перемещение: `nothing_to_transfer` после 07:00-синка** — OLAP `report=TRANSACTIONS from=today` не видит вчерашних транзакций как «сегодняшних». Это нормально — минусы на целевых складах исчезают после перемещений. Если остатки вновь уходят в минус днём, перемещение сработает в 23:00.
