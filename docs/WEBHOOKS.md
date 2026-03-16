# 🔔 Вебхуки iikoCloud и уведомления

> **Зависимости:** PROJECT_MAP.md (прочитан).  
> Если задеваешь БД → загрузи секции `stock_alert_message`, `stoplist_*`, `active_stoplist` из DATABASE.md.  
> Если новый handler → загрузи UX_PATTERNS.md.

---

## Контракты домена

- **W1:** Вебхук всегда debounce (60 сек стоп-лист, антиспам остатки). Пересмотреть когда: real-time требования.
- **W2:** UX всегда delete → send → pin (не edit). Причина: свежее сообщение всплывает наверх в чате.
- **W3:** Получатели из GSheet ролей. Нет отмеченных → все авторизованные (bootstrap).
- **W4:** Покомпонентное сравнение остатков — каждая позиция проверяется отдельно (не суммарное).

---

## Архитектура обработки

```
iikoCloud POST /iiko-webhook
  ├─ StopListUpdate → debounce 60 сек → fetch → diff → delete old msg → send new → pin
  │   └─ Получатели: роль «🚫 Стоп-лист» (fallback → все авторизованные)
  │   └─ Debounce: серия вебхуков (10 позиций = 10 вебхуков) → 60 сек тишины → один flush
  │
  └─ DeliveryOrderUpdate / TableOrderUpdate (status=Closed)
      → только логирование (кол-во закрытых заказов)
      → остатки НЕ синхронизируются и НЕ отправляются автоматически
      → отправка остатков — только по кнопке «🔄 Обновить остатки сейчас» (force_stock_check)
```

---

## Стоп-лист: формат сообщения

```
Новые блюда в стоп-листе 🚫
▫️ Блю манки — стоп
▫️ Тартар из говядины — стоп

Удалены из стоп-листа ✅
▫️ —

Остались в стоп-листе
▫️ Кокосовое молоко — стоп

#стоплист
```

- Позиции отсортированы по алфавиту внутри блока
- При авторизации / смене подразделения — полный стоп-лист (без дифа)

---

## Остатки: логика обновления

**Автоматическая отправка остатков при закрытии заказа — ОТКЛЮЧЕНА.**

Остатки теперь отправляются **только по кнопке** «🔄 Обновить остатки сейчас» (вызов `force_stock_check`).

При нажатии кнопки:
1. Синхронизируются остатки из iiko REST API (`sync_stock_balances`)
2. Проверяются минимальные уровни (`check_min_stock_levels`)
3. Обновляются закреплённые сообщения (`update_all_stock_alerts`) per-department
4. Сообщение: delete old → send new → pin

**UX:** Per-department: каждый пользователь видит остатки **своего** подразделения.

---

## Маппинг подразделений → Cloud-организаций

- iiko Server `department_id` ≠ iikoCloud `organization_id` (разные системы)
- Маппинг хранится в GSheet «Настройки» (лист создаётся автоматически)
- Колонка C — выпадающий список Cloud-организаций, UUID через VLOOKUP
- Кеш: in-memory dict с TTL 5 мин (`use_cases/cloud_org_mapping.py`)
- Fallback: env `IIKO_CLOUD_ORG_ID` → env `ORG_ID` → None
- Кнопка «🔗 Привязать организации» — синхронизация в GSheet

---

## Подписки на уведомления (роли в GSheet)

| Роль / Право | Столбец GSheet | Получает |
|------|----------------|----------|
| 🔧 Сис.Админ | «🔧 Сис.Админ» | Только технические алерты ERROR/CRITICAL |
| ⚙️ Настройки | «⚙️ Настройки» | Синхронизация, права, GSheet-операции |
| 📬 Получатель | «📬 Получатель» | Заявки на товары |
| 📦 Остатки | «📦 Остатки» | Pinned остатки ниже минимума |
| 🚫 Стоп-лист | «🚫 Стоп-лист» | Pinned стоп-лист + отчёт 22:00 |
| 📑 Бухгалтер | «📑 Бухгалтер» | OCR-услуги + запрос маппинга |
| 📋 Отчёт дня | «📋 Отчёт дня» | Получает отчёт смены от сотрудника |

Если **никто** не отмечен → рассылка **всем** авторизованным.

---

## Модули

| Модуль | Роль | Зависимости |
|--------|------|-------------|
| `use_cases/iiko_webhook_handler.py` | Обработка вебхуков, дельта-сравнение | `cloud_org_mapping`, `sync_stock_balances`, `check_min_stock` |
| `use_cases/pinned_stock_message.py` | Pinned остатки: delete → send → pin | `check_min_stock`, `_helpers.compute_hash` |
| `use_cases/pinned_stoplist_message.py` | Pinned стоп-лист: delete → send → pin | `stoplist`, `_helpers.compute_hash` |
| `use_cases/stoplist.py` | Fetch + diff стоп-листа | `iiko_cloud_api`, `active_stoplist` таблица |
| `use_cases/stoplist_report.py` | Ежевечерний отчёт 22:00 | `stoplist_history`, `permissions` |
| `use_cases/cloud_org_mapping.py` | dept_id → cloud_org_id, TTL 5 мин | `google_sheets`, `iiko_cloud_api` |
| `adapters/iiko_cloud_api.py` | Токен, register_webhook, fetch_stop_lists | `iiko_access_tokens` таблица |

## Таблицы (компактно)

| Таблица | Назначение | Ключ |
|---------|-----------|------|
| `stock_alert_message` | Pinned остатки (chat_id, msg_id, hash) | chat_id |
| `stoplist_message` | Pinned стоп-лист | chat_id |
| `active_stoplist` | Зеркало iikoCloud стоп-листа | product_id |
| `stoplist_history` | Вход/выход из стопа | product_id + started_at |
| `iiko_access_tokens` | Cloud-токены (внешний cron) | org_id |

## Частые ошибки в этом домене

1. **Стоп-лист пуст:** `ORG_ID` ≠ `IIKO_CLOUD_ORG_ID` → fallback в config.py
2. **Вебхук не приходит:** Проверь регистрацию через кнопку «ℹ️ Статус вебхука»
3. **Edit вместо delete+send:** Нарушение W2 — сообщение не всплывает наверх
4. **Глобальные переменные без Lock:** Гонка данных в webhook handler (см. K6 в CHANGELOG 2026-02-22)
