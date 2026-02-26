# 📝 Списания (Writeoffs)

> **Зависимости:** PROJECT_MAP.md (прочитан).  
> Если задеваешь БД → загрузи секции `pending_writeoff`, `writeoff_history` из DATABASE.md.  
> Если новый handler → загрузи UX_PATTERNS.md.

---

## Контракты домена

- **WO1:** Pending writeoffs → PostgreSQL (переживают рестарт). In-memory хранение запрещено.
- **WO2:** Авто-выбор склада по должности (Бар/Кухня). Админ → ручной выбор.
- **WO3:** Номенклатура фильтруется по разрешённым папкам: GOODS+DISH из `gsheet_export_group` (обычные точки) / `writeoff_request_store_group` (точка-получатель). PREPARED — все, кроме исключённых папок (`writeoff_excluded_prepared_group` / `writeoff_excluded_prepared_request_group`).
- **WO3b:** При поиске GOODS/DISH выводятся перед PREPARED (приоритетная сортировка), чтобы п/ф не вытесняли блюда при лимите.
- **WO4:** Идемпотентный POST: UUID генерируется при создании, повторный POST = обновление (не дубликат).

---

## Поток создания акта (FSM)

```
📝 Списания → [авто-выбор склада] → [выбор счёта] → поиск товаров → ввод количества → причина
  ├─ Без админов: отправка в iiko сразу + save_to_history
  └─ С админами: pending_writeoff → кнопки одобрения → iiko + save_to_history
```

**FSM-состояния:** `WriteoffStates`: `store → account → search_product → enter_qty → reason → confirm`

## Авто-выбор склада

| Должность | Действие |
|-----------|----------|
| Бот-админ | Ручной выбор из списка |
| «*бар*» | Авто → склад типа «Бар» |
| «*кухня*» / «*повар*» | Авто → склад типа «Кухня» |
| Unknown | Ручной выбор |

---

## Pending writeoffs (PostgreSQL)

- Таблица `pending_writeoff`: `doc_id`, `author_chat_id`, `store_id`, `account_id`, `items` (JSONB), `admin_msg_ids` (JSONB), `is_locked` (атомарный лок)
- TTL 24ч — автоочистка expired
- Конкурентность: `UPDATE ... WHERE is_locked = false`
- При рестарте бота — все pending сохранены, админ может одобрить/отклонить

## История списаний

- Таблица `writeoff_history`: JSONB items, `store_type` для ролевой фильтрации
- Автоочистка: >200 записей на пользователя
- Фильтрация: бар → только бар, кухня → только кухня, admin → все
- Дублирование: «🔄 Повторить» → новый акт с позициями из истории

---

## Модули

| Модуль | Роль |
|--------|------|
| `bot/writeoff_handlers.py` | FSM: создание, одобрение, история, редактирование |
| `use_cases/writeoff.py` | Бизнес-логика: preload_products, build_writeoff_xml, search_products |
| `use_cases/pending_writeoffs.py` | CRUD pending в PostgreSQL |
| `use_cases/writeoff_history.py` | save/get/build_summary |
| `use_cases/writeoff_cache.py` | TTL-кеш: stores, accounts, products |

## Таблицы

| Таблица | Назначение | Ключевые поля |
|---------|-----------|---------------|
| `pending_writeoff` | Ожидающие одобрения | doc_id, is_locked, items (JSONB) |
| `writeoff_history` | Архив списаний | telegram_id, department_id, store_type |
| `writeoff_excluded_prepared_group` | Искл. папки PREPARED (обычные точки) | group_id (UUID) |
| `writeoff_excluded_prepared_request_group` | Искл. папки PREPARED (точка-получатель) | group_id (UUID) |

## Частые ошибки

1. **In-memory pending:** Запрещено (WO1) — используй PostgreSQL таблицу
2. **UUID не генерируется:** Каждый документ = `uuid4()` для идемпотентности
3. **is_locked гонка:** Только `UPDATE ... WHERE is_locked = false` (атомарный)
4. **Кеш продуктов:** ~400 КБ RAM, TTL 10 мин, прогрев при входе в «Документы»
