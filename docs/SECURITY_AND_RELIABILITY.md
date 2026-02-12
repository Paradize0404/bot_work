# 🔒 Безопасность, надёжность и защита от регрессий

> Читай этот файл при: добавлении нового handler'а, работе с авторизацией, деплое, оптимизации конкурентности.
> Принципы здесь — **обязательные**, не рекомендации. Нарушение = потенциальный production-инцидент.

---

## 1. Валидация callback_data (ОБЯЗАТЕЛЬНО в каждом handler)

### Проблема
Telegram не гарантирует, что callback_data от пользователя совпадает с тем, что было отправлено в кнопке.
Модифицированный клиент может прислать `auth_emp:'; DROP TABLE users--` или `wo_store:NOT_A_UUID`.

### Паттерн: безопасный парсинг

```python
# ❌ ПЛОХО — crash на невалидном UUID
async def choose_store(callback: CallbackQuery, state: FSMContext):
    store_id = callback.data.split(":", 1)[1]
    # если store_id = "GARBAGE" → UUID() → ValueError → 500
    store_uuid = UUID(store_id)

# ✅ ХОРОШО — validate + early return
from uuid import UUID

def _parse_uuid(raw: str) -> UUID | None:
    """Безопасный парсинг UUID из callback_data."""
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None

def _parse_int(raw: str) -> int | None:
    """Безопасный парсинг int из callback_data."""
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None

async def choose_store(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    store_uuid = _parse_uuid(raw)
    if store_uuid is None:
        await callback.answer("⚠️ Ошибка данных, попробуйте ещё раз")
        logger.warning("[writeoff] invalid callback_data: %s, tg:%d", callback.data, callback.from_user.id)
        return
    # ... safe to use store_uuid
```

### Чеклист для каждого callback-handler'а:
1. `callback.answer()` — первым
2. Парсинг `callback.data` — через `_parse_uuid()` / `_parse_int()`
3. Проверка `None` → early return с предупреждением
4. Лог невалидных данных на `logger.warning` (для детекции атак)

---

## 2. Авторизация и разграничение доступа

### Матрица доступа

| Операция | Неавторизованный | Авторизованный | Админ |
|----------|:---:|:---:|:---:|
| `/start`, авторизация | ✅ | ✅ | ✅ |
| Меню, навигация | ❌ | ✅ | ✅ |
| Просмотр остатков / мин. остатков | ❌ | ✅ | ✅ |
| Создание акта списания | ❌ | ✅ | ✅ |
| Одобрение/отклонение актов | ❌ | ❌ | ✅ |
| **Синхронизация iiko / FinTablo** | ❌ | ❌ | ✅ |
| Управление админами | ❌ | ❌ | ✅ |
| Редактирование мин. остатков | ❌ | ❌ | ✅ |

### Паттерн: middleware авторизации

```python
# Вместо проверки в каждом handler — middleware (или decorator)
from functools import wraps

def admin_required(handler):
    """Decorator: проверяет admin-права перед выполнением handler'а."""
    @wraps(handler)
    async def wrapper(event, *args, **kwargs):
        tg_id = event.from_user.id
        if not await is_admin(tg_id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Только для администраторов")
            else:
                await event.answer("⛔ У вас нет прав администратора")
            logger.warning("[auth] unauthorized access attempt: tg:%d, handler:%s", tg_id, handler.__name__)
            return
        return await handler(event, *args, **kwargs)
    return wrapper

# Использование:
@router.callback_query(F.data == "sync_all_iiko")
@admin_required
async def sync_all_iiko(callback: CallbackQuery):
    ...
```

### Защита от admin escalation
```python
async def remove_admin(session, admin_tg_id: int, target_tg_id: int) -> str:
    """Удаление админа с защитой от удаления последнего."""
    count = await session.scalar(select(func.count()).select_from(Admin))
    if count <= 1 and admin_tg_id == target_tg_id:
        return "⚠️ Нельзя удалить единственного администратора"
    # ... proceed with deletion
```

---

## 3. Webhook Security

### Обязательная конфигурация

```python
import secrets

# В config.py:
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or secrets.token_hex(32)

# В main.py при set_webhook:
await bot.set_webhook(
    url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
    secret_token=WEBHOOK_SECRET,
    drop_pending_updates=True,
)

# В setup aiohttp:
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
```

### Без секрета → любой POST на `/webhook` = fake update
Это не теория — сканеры автоматически пробуют типичные пути (`/webhook`, `/bot`, `/telegram`).

---

## 4. Rate Limiting

### Проблема
Без ограничений один пользователь может:
- Запустить 100 sync-операций в минуту (каждая = 10+ API-запросов)
- Спамить поиск продуктов (каждый запрос = SELECT по БД)
- Создать 50 актов списания одновременно

### Паттерн: простой cooldown (без зависимостей)

```python
import time
from collections import defaultdict

_last_action: dict[int, float] = defaultdict(float)  # tg_id → timestamp

def check_cooldown(tg_id: int, action: str, seconds: float = 1.0) -> bool:
    """Возвращает True если действие разрешено, False если слишком рано."""
    key = (tg_id, action)
    now = time.monotonic()
    if now - _last_action[key] < seconds:
        return False
    _last_action[key] = now
    return True

# В handler:
async def sync_all_iiko(callback: CallbackQuery):
    await callback.answer()
    if not check_cooldown(callback.from_user.id, "sync", seconds=5.0):
        await callback.answer("⏳ Подождите 5 сек между синхронизациями")
        return
    ...
```

### Рекомендованные cooldown'ы:
| Операция | Cooldown |
|----------|----------|
| Sync (iiko/FinTablo) | 10 сек |
| Создание акта списания (finalize) | 5 сек |
| Поиск продуктов | 1 сек |
| Обычные кнопки навигации | 0.3 сек |
| Admin operations | 3 сек |

---

## 5. Токены и секреты в логах

### Правила
1. **httpx/httpcore** логгеры = `WARNING` минимум. Никогда `DEBUG` в production.
2. **iiko API key** передаётся в URL query — при логировании URL маскировать: `key=abc...xyz` → `key=***`
3. **FinTablo Bearer** — не логировать заголовки запросов.
4. **Traceback** — httpx включает URL в сообщение об ошибке. При логировании исключений проверять.

### Паттерн: маскировка в логах

```python
import re

_SECRET_RE = re.compile(r'(key|token|password|secret|bearer)=([^\s&"\']+)', re.IGNORECASE)

def mask_secrets(text: str) -> str:
    """Маскирует секреты в строке для безопасного логирования."""
    return _SECRET_RE.sub(r'\1=***', text)

# Использование:
logger.error("[iiko] request failed: %s", mask_secrets(str(exc)))
```

---

## 6. Health Endpoint

### Реализация для aiohttp (webhook-режим)

```python
from aiohttp import web

async def health_check(request: web.Request) -> web.Response:
    """Health endpoint для Railway / мониторинга."""
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        return web.json_response({"status": "ok", "db": "connected"})
    except Exception as e:
        logger.error("[health] DB check failed: %s", e)
        return web.json_response(
            {"status": "error", "db": str(e)},
            status=503,
        )

# В main.py setup:
app.router.add_get("/health", health_check)
```

---

## 7. Graceful Shutdown

### Текущая проблема
- `_pending` writeoffs хранятся в RAM → потеря при рестарте
- Background tasks не отслеживаются → могут держать connections
- Railway шлёт SIGTERM, polling-режим может не поймать

### Паттерн: tracked tasks + shutdown

```python
import signal
import asyncio

_background_tasks: set[asyncio.Task] = set()

def track_task(coro) -> asyncio.Task:
    """Создать background task с отслеживанием для graceful shutdown."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def graceful_shutdown():
    """Завершение с очисткой всех ресурсов."""
    logger.info("[shutdown] Stopping... %d background tasks pending", len(_background_tasks))
    
    # 1. Отменить background tasks
    for task in _background_tasks:
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    
    # 2. Логировать потерянные pending writeoffs
    from use_cases.pending_writeoffs import get_all_pending
    pending = get_all_pending()
    if pending:
        logger.warning("[shutdown] LOSING %d pending writeoffs: %s",
                       len(pending), [d.doc_id for d in pending])
    
    # 3. Cleanup connections
    await _cleanup()
    logger.info("[shutdown] Clean exit")

# Для polling-режима:
loop = asyncio.get_running_loop()
loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(graceful_shutdown()))
```

---

## 8. Sync Lock (предотвращение параллельных синхронизаций)

### Проблема
Два пользователя нажимают «Синхр. ВСЁ» одновременно → двойная нагрузка на API, потенциальные deadlocks в БД.

### Паттерн

```python
import asyncio

_sync_locks: dict[str, asyncio.Lock] = {}

def get_sync_lock(entity: str) -> asyncio.Lock:
    """Получить lock для конкретного типа синхронизации."""
    if entity not in _sync_locks:
        _sync_locks[entity] = asyncio.Lock()
    return _sync_locks[entity]

async def run_sync_with_lock(entity: str, sync_coro):
    """Запуск синхронизации с гарантией единственного выполнения."""
    lock = get_sync_lock(entity)
    if lock.locked():
        return None  # уже запущено
    async with lock:
        return await sync_coro

# В handler:
async def sync_products(callback: CallbackQuery):
    await callback.answer()
    result = await run_sync_with_lock("products", sync.sync_products())
    if result is None:
        await callback.message.answer("⏳ Синхронизация продуктов уже запущена, подождите")
        return
    await callback.message.answer(f"✅ {result}")
```

---

## 9. Retry для iiko POST (writeoff)

### Проблема
`send_writeoff()` — единственный POST к iiko. Нет retry. Если сеть моргнула после одобрения админом — документ потерян.

### Решение: idempotency key + retry

```python
async def send_writeoff_with_retry(
    xml_body: str, 
    doc_id: str, 
    max_retries: int = 2, 
    backoff: tuple = (2, 5)
) -> httpx.Response:
    """POST writeoff с retry на транзиентные ошибки.
    
    iiko writeoff создаёт документ с ID из XML — повторный POST 
    с тем же ID = обновление, не дубликат (idempotent by design).
    """
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, content=xml_body, ...)
            resp.raise_for_status()
            return resp
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            if attempt == max_retries:
                raise
            delay = backoff[attempt] if attempt < len(backoff) else backoff[-1]
            logger.warning("[iiko] writeoff POST retry %d/%d for doc %s: %s", 
                          attempt + 1, max_retries, doc_id, e)
            await asyncio.sleep(delay)
```

---

## 10. Ошибки: классификация и стратегия

### Три типа ошибок

| Тип | Примеры | Стратегия |
|-----|---------|-----------|
| **Transient** (пройдёт) | `ConnectError`, `ReadTimeout`, HTTP 429/502/503 | Retry с backoff, потом warning |
| **Permanent** (не пройдёт) | HTTP 400/401/404, `ValueError`, невалидный XML | Не retry. Error log. Уведомление. |
| **Unknown** | Любой другой `Exception` | 1 retry, потом error log + alert |

### Паттерн: классификация

```python
_TRANSIENT = (
    httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
    httpx.RemoteProtocolError, httpx.PoolTimeout,
)

def is_transient(exc: Exception) -> bool:
    """Определяет, является ли ошибка транзиентной (стоит retry)."""
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False
```

---

## 11. Config Validation (fail-fast при старте)

### Текущее: `_require()` — проверяет только «не пусто»
### Нужное: валидация формата

```python
def _require_url(name: str) -> str:
    """Требует env var с валидным URL."""
    val = _require(name)
    if not val.startswith(("http://", "https://")):
        raise RuntimeError(f"{name} must be a valid URL, got: {val[:20]}...")
    return val.rstrip("/")

def _require_int(name: str, min_val: int = 0, max_val: int = 65535) -> int:
    """Требует env var с int в допустимых границах."""
    raw = _require(name)
    try:
        val = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw}")
    if not (min_val <= val <= max_val):
        raise RuntimeError(f"{name} must be {min_val}–{max_val}, got: {val}")
    return val

# Использование:
IIKO_BASE_URL = _require_url("IIKO_BASE_URL")
FINTABLO_BASE_URL = _require_url("FINTABLO_BASE_URL") if os.getenv("FINTABLO_BASE_URL") else "https://api.fintablo.ru"
WEBAPP_PORT = _require_int("PORT", 1024, 65535) if os.getenv("PORT") else 8080
```

### Валидация WEBHOOK_PATH
```python
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH
```

---

## 12. Алертинг в Telegram (бесплатный мониторинг)

### Паттерн: отправка критических ошибок админам

```python
async def alert_admins(bot: Bot, message: str):
    """Отправить алерт всем админам. Fire-and-forget."""
    admin_ids = await get_admin_ids()
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, f"🚨 ALERT\n\n{message[:4000]}")
        except Exception:
            pass  # если не можем алертнуть — не падаем

# Использование в обработке ошибок:
try:
    await sync_all()
except Exception as e:
    logger.exception("[sync] critical failure")
    await alert_admins(bot, f"Sync failed: {e}")
```

---

## Сводная таблица: что реализовано vs что нужно добавить

| Защита | Статус | Приоритет |
|--------|--------|-----------|
| callback_data валидация | 🔴 Нет | CRITICAL |
| Webhook secret | 🔴 Нет | CRITICAL |
| Rate limiting | 🔴 Нет | HIGH |
| Auth на sync-кнопках | 🔴 Нет | HIGH |
| Health endpoint | 🔴 Нет | HIGH |
| Retry iiko POST (writeoff) | 🔴 Нет | HIGH |
| Sync lock (конкурентность) | 🔴 Нет | HIGH |
| FinTablo retry на ConnectError | 🔴 Нет | MEDIUM |
| Admin self-removal защита | 🔴 Нет | MEDIUM |
| Config URL validation | 🔴 Нет | MEDIUM |
| SIGTERM handler (polling) | 🟡 Частично | MEDIUM |
| Pending writeoffs persistence | 🟡 RAM only | MEDIUM |
| Token masking в логах | 🟡 Частично | MEDIUM |
| Alerting (Telegram) | 🔴 Нет | MEDIUM |
| Startup self-check | 🟡 Частично (DB only) | LOW |
| Double-click lock | ✅ Есть | — |
| Retry iiko GET | ✅ Есть | — |
| Retry FinTablo 429 | ✅ Есть | — |
| Batch upsert | ✅ Есть | — |
| Mirror-delete sanity | ✅ Есть | — |
| SyncLog аудит | ✅ Есть | — |
| QTY/length bounds | ✅ Есть | — |
| FSM state guards (text) | ✅ Есть | — |
