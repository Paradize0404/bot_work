# 🗺️ Roadmap: аудит и улучшения бота

> Последнее обновление: 2026-02-26

---

## ✅ Завершённые сессии

### Сессия 1 — Безопасность
- [x] middleware.py: `auth_required`, `admin_required` декораторы
- [x] Валидация `callback_data` (`_parse_uuid`, `_parse_int`)
- [x] Webhook secret (`WEBHOOK_SECRET`)
- [x] Защита sync/admin кнопок проверкой прав

### Сессия 2 — UX-паттерны
- [x] edit-not-answer для inline-кнопок (не спамить в чат)
- [x] `ChatAction.TYPING` перед длинными операциями
- [x] Guard handlers (удаление текста в inline-состояниях)
- [x] `reply_menu()` — единая точка отправки ReplyKeyboard
- [x] Валидация → edit prompt (не новое сообщение)

### Сессия 3 — Архитектура (thin handlers)
- [x] handlers.py: 708 → 458 строк (auth, sync, reports → use_cases)
- [x] writeoff_handlers.py: 1245 → 1027 строк (prepare, approve, finalize → use_cases)
- [x] admin_handlers.py: 385 → 296 строк (format_admin_list, get_available_for_promotion)
- [x] min_stock_handlers.py: 313 → 238 строк (apply_min_level validation)
- [x] Новые use_case модули: auth (dataclasses), reports.py, sync (orchestration), writeoff (dataclasses)
- [x] 0 прямых DB-импортов в bot/ (кроме middleware)

---

## ⬜ Запланированные сессии

### Сессия N — K5: Персистентный кеш pending invoices
**Приоритет: HIGH** | Источник: аудит 2026-02-22 (K5)

- [x] `_pending_invoices` в `use_cases/incoming_invoice.py` хранится в RAM → при рестарте/деплое теряется
- [x] Создать таблицу `pending_incoming_invoice` (аналогично `pending_writeoff`)
- [x] Перевести всю логику на PostgreSQL (CRUD через async_session_factory)
- [x] Добавить TTL-чистку (`_cleanup_expired`) для зависших документов
- [x] Проверить, что FSM-состояния корректно сбрасываются при потере документа

> ✅ Реализовано: таблица `pending_incoming_invoice` в models.py, CHANGELOG 2026-02-25

### Сессия N+1 — A1-A2: use_cases не должен импортировать из bot/
**Приоритет: MEDIUM** | Источник: аудит 2026-02-22 (A1-A2)

- [ ] Найти все `from bot.` импорты внутри `use_cases/` (permission_map, keyboards и т.д.)
- [ ] Перенести общие константы (PERM_*, NAV_BUTTONS) в нейтральный модуль (e.g. `config.py` или `use_cases/constants.py`)
- [ ] Обновить все импорты в `bot/` и `use_cases/`
- [ ] Проверить, что циклических зависимостей больше нет
- [ ] ⚠️ Рискованный рефактор — тестировать все FSM-флоу после изменений

### Сессия 4 — Rate Limiting + Sync Lock
**Приоритет: HIGH** | Источник: `docs/SECURITY_AND_RELIABILITY.md` §4, §8

- [ ] `use_cases/cooldown.py` — in-memory cooldown с auto-cleanup
  - sync: 10 сек, finalize_writeoff: 5 сек, search: 1 сек, navigation: 0.3 сек, admin: 3 сек
- [ ] `use_cases/sync_lock.py` — asyncio.Lock per entity (iiko_all, fintablo, everything, documents)
- [ ] Интеграция в handlers.py, writeoff_handlers.py, admin_handlers.py, min_stock_handlers.py
- [ ] Паттерн: `if not check_cooldown(...)` → edit_text "⏳ Подождите..."
- [ ] Паттерн: `run_sync_with_lock(...)` → None = "⏳ Уже запущено"

### Сессия 5 — Retry writeoff POST + Error Classification
**Приоритет: HIGH** | Источник: `docs/SECURITY_AND_RELIABILITY.md` §9, §10

- [x] `send_writeoff_with_retry()` — retry на ConnectError/ReadTimeout (2 попытки, backoff 2s/5s)
- [x] iiko writeoff idempotent by design (тот же doc ID → обновление, не дубликат)
- [x] Классификация ошибок: transient (retry) vs permanent (log + alert) vs unknown (1 retry)
- [x] `is_transient(exc)` helper — ConnectError, ReadTimeout, 429/502/503
- [x] FinTablo retry на ConnectError (если не реализован)

> ✅ Реализовано: `use_cases/errors.py` (is_transient), CHANGELOG 2026-02-22

### Сессия 6 — Инфраструктура (можно объединить с 7)
**Приоритет: MEDIUM** | Источник: `docs/SECURITY_AND_RELIABILITY.md` §6, §7, §12

- [ ] Health endpoint `/health` (aiohttp, проверка DB)
- [ ] Graceful shutdown: отмена background tasks, лог потерянных pending writeoffs
- [ ] SIGTERM handler для polling-режима
- [ ] `alert_admins(bot, message)` — критические ошибки → Telegram всем админам

### Сессия 7 — Config Hardening + Log Safety
**Приоритет: MEDIUM** | Источник: `docs/SECURITY_AND_RELIABILITY.md` §5, §11

- [ ] `_require_url()`, `_require_int()` — валидация формата env vars при старте
- [ ] `mask_secrets(text)` — маскировка key/token/bearer в логах
- [ ] httpx/httpcore логгеры → WARNING в production
- [ ] WEBHOOK_PATH нормализация (всегда с `/`)

---

## 📊 Сводка из SECURITY_AND_RELIABILITY.md

| Защита | Статус | Сессия |
|--------|--------|--------|
| callback_data валидация | ✅ | 1 |
| Webhook secret | ✅ | 1 |
| Auth на sync/admin кнопках | ✅ | 1 |
| UX: edit-not-answer, TYPING | ✅ | 2 |
| Thin handlers → use_cases | ✅ | 3 |
| Rate limiting / cooldown | ✅ | 4 (реализовано) |
| Sync lock (конкурентность) | ✅ | 4 (реализовано) |
| `== False` → `.is_(False)` | ✅ | аудит 22.02 |
| `safe_page()` пагинация | ✅ | аудит 22.02 |
| `_pending_invoices` → DB | ✅ | K5 (реализовано) |
| use_cases → bot/ зависимость | ⬜ | A1-A2 |
| Retry iiko POST (writeoff) | ✅ | 5 (реализовано) |
| Error classification | ✅ | 5 (реализовано) |
| Health endpoint | ✅ | 6 (реализовано) |
| Graceful shutdown | ✅ | 6 (реализовано) |
| Alerting (Telegram) | ✅ | 6 (реализовано) |
| Config URL validation | ⬜ | 7 |
| Token masking в логах | ✅ | 7 (реализовано) |

---

## 💡 Заметки

- Сессии 4 и 5 — **обязательные** (защита API от спама, защита финансовых документов от потери)
- Сессии 6 и 7 — **рекомендуемые**, можно объединить в одну
- Промпт для сессии 4 уже готов (сгенерирован в конце сессии 3)
