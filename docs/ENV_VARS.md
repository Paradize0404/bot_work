# ⚙️ Переменные окружения

> **Зависимости:** PROJECT_MAP.md (прочитан).  
> **Загружай когда:** деплой, настройка, новая env-переменная.

---

## Обязательные

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | PostgreSQL (`postgresql+asyncpg://...`) |
| `IIKO_BASE_URL` | URL iiko API (`https://ip-merzlyakov-e-a-co.iiko.it`) |
| `IIKO_LOGIN` | Логин iiko API |
| `IIKO_SHA1_PASSWORD` | SHA1-хеш пароля iiko |
| `FINTABLO_TOKEN` | Bearer-токен FinTablo API |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `GOOGLE_SHEETS_CREDENTIALS` | Путь к JSON Service Account **или** inline JSON (Railway). Определяется автоматически по `startswith("{")` |

## Опциональные (с дефолтами)

| Переменная | Дефолт | Описание |
|------------|--------|----------|
| `FINTABLO_BASE_URL` | `https://api.fintablo.ru` | Base URL FinTablo |
| `WEBHOOK_URL` | — | URL на Railway (`https://xxx.up.railway.app`). Если задан → webhook, иначе polling |
| `WEBHOOK_PATH` | `/webhook` | Путь вебхука |
| `PORT` | `8080` | Порт (Railway задаёт автоматически) |
| `LOG_LEVEL` | `INFO` | Уровень логирования |
| `TIMEZONE` | Хардкод `Europe/Kaliningrad` | Не читается из env |
| `MIN_STOCK_SHEET_ID` | Хардкод в config.py | Google Таблица (мин. остатки, права, настройки, маппинг) |
| `INVOICE_PRICE_SHEET_ID` | = `MIN_STOCK_SHEET_ID` | Таблица прайс-листа |
| `REDIS_URL` | — | Redis для FSM storage (если не задан — MemoryStorage) |

## iikoCloud

| Переменная | Дефолт | Описание |
|------------|--------|----------|
| `IIKO_CLOUD_ORG_ID` | fallback → `ORG_ID` | UUID организации iikoCloud |
| `IIKO_CLOUD_BASE_URL` | `https://api-ru.iiko.services` | Базовый URL iikoCloud |
| `IIKO_CLOUD_WEBHOOK_SECRET` | auto-generated | authToken для верификации вебхуков |
| `STOCK_CHANGE_THRESHOLD_PCT` | `3.0` | Порог изменения позиции в % для триггера обновления |
| `STOCK_UPDATE_INTERVAL_MIN` | `30` | Минимальный интервал между отправками остатков (мин) |

## Авто-перемещение (23:00)

| Переменная | Дефолт | Описание |
|------------|--------|----------|
| `NEGATIVE_TRANSFER_SOURCE_PREFIX` | `Хоз. товары` | Префикс склада-источника |
| `NEGATIVE_TRANSFER_TARGET_PREFIXES` | `Бар,Кухня` | CSV целевых складов |
| `NEGATIVE_TRANSFER_PRODUCT_GROUP` | `Расходные материалы` | Корневая группа номенклатуры |

## Безопасность

| Переменная | Дефолт | Описание |
|------------|--------|----------|
| `WEBHOOK_SECRET` | auto-generated | Секрет для Telegram webhook `secret_token` |
| `IIKO_VERIFY_SSL` | `false` | SSL-верификация для iiko on-premise |
| `WEBAPP_HOST` | `0.0.0.0` | Хост веб-сервера |

## Правила

- Все обязательные читаются через `_require()` — fail-fast с понятной ошибкой.
- `DATABASE_URL` автоматически нормализуется под `asyncpg` в `config.py`.
- Секреты — только через переменные окружения Railway. Никогда не коммитить `.env`.
- При добавлении новой переменной — добавить в эту таблицу + `config.py`.

---

## Команды

```bash
pip install -r requirements.txt   # Зависимости
python -m db.init_db              # Миграции
python main.py                    # Запуск (webhook или polling по наличию WEBHOOK_URL)
```

## Чеклист деплоя

**Перед:**
- [ ] Все env vars заданы (проверить `config.py _require()`)
- [ ] `python -m db.init_db` выполнен
- [ ] Нет `print()`, hardcoded secrets, `LOG_LEVEL=DEBUG`
- [ ] `Procfile` = `web` (webhook) или `worker` (polling)
- [ ] Webhook URL = `https://`

**После:**
- [ ] `/start` отвечает за 5 сек
- [ ] `/health` → `200`
- [ ] SyncLog пишется (запустить sync вручную)
- [ ] Логи чистые (INFO, без токенов)
