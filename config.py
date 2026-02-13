"""
Конфигурация проекта.
Все секреты и настройки читаются из .env — никакого хардкода.
"""

import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env из корня проекта
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


def _require(name: str) -> str:
    """Получить переменную окружения или упасть с понятной ошибкой."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env variable: {name}")
    return value


# ── Database ──
_raw_db_url: str = _require("DATABASE_URL")
# Railway выдаёт URL вида postgresql://… — принудительно ставим asyncpg-драйвер
if _raw_db_url.startswith("postgresql://"):
    _raw_db_url = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
DATABASE_URL: str = _raw_db_url

# ── iiko API ──
IIKO_BASE_URL: str = _require("IIKO_BASE_URL")
IIKO_LOGIN: str = _require("IIKO_LOGIN")
IIKO_SHA1_PASSWORD: str = _require("IIKO_SHA1_PASSWORD")
# SSL-верификация: iiko on-premise часто использует self-signed сертификаты.
# Установите IIKO_VERIFY_SSL=true если сервер имеет валидный SSL-сертификат,
# или укажите путь к CA-bundle: IIKO_VERIFY_SSL=/path/to/ca-bundle.crt
IIKO_VERIFY_SSL: bool | str = (
    lambda v: True if v.lower() == "true"
    else (False if v.lower() in ("false", "0", "") else v)
)(os.getenv("IIKO_VERIFY_SSL", "false"))

# ── FinTablo API ──
FINTABLO_BASE_URL: str = os.getenv("FINTABLO_BASE_URL", "https://api.fintablo.ru")
FINTABLO_TOKEN: str = _require("FINTABLO_TOKEN")

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# ── Webhook security ──
# Секрет для верификации запросов Telegram → set_webhook(secret_token=...)
# Если не задан в env — генерируется при каждом старте (безопасно, Telegram пересогласует).
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET") or secrets.token_hex(32)

# ── Webhook (Railway) ──
# Если WEBHOOK_URL задан — бот работает на вебхуке, иначе — polling.
# На Railway задайте: WEBHOOK_URL=https://<ваш-сервис>.up.railway.app
_wh = os.getenv("WEBHOOK_URL", "").strip()
if _wh:
    # Убираем любые ошибочные префиксы протокола
    for _bad in ("https://", "http://", "ttps://", "tps://"):
        if _wh.startswith(_bad):
            _wh = _wh[len(_bad):]
            break
    # Убираем trailing slash и WEBHOOK_PATH если пользователь вписал его в URL
    _wh = _wh.rstrip("/")
    _default_path = os.getenv("WEBHOOK_PATH", "/webhook")
    if _wh.endswith(_default_path):
        _wh = _wh[: -len(_default_path)]
    # Всегда ставим https://
    _wh = f"https://{_wh}"
WEBHOOK_URL: str | None = _wh or None                       # None → polling
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")   # путь, куда Telegram шлёт апдейты
WEBAPP_HOST: str = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT: int = int(os.getenv("PORT", "8080"))            # Railway пробрасывает PORT

# ── Google Sheets (мин. остатки) ──
GOOGLE_SHEETS_CREDENTIALS: str = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS", "pizzayolo-ocr-3898e5dddcff.json"
)
MIN_STOCK_SHEET_ID: str = os.getenv(
    "MIN_STOCK_SHEET_ID", "1cKQAPXDap6sSAmGROYE-kqyNrVzJnf0bPpTjPyRKa_8"
)

# ── Google Sheets (прайс-лист накладных) ──
# По умолчанию — тот же spreadsheet, но отдельный таб «Прайс-лист»
INVOICE_PRICE_SHEET_ID: str = os.getenv(
    "INVOICE_PRICE_SHEET_ID", "1cKQAPXDap6sSAmGROYE-kqyNrVzJnf0bPpTjPyRKa_8"
)

# ── Timezone ──
# Все даты в проекте — по Калининграду (UTC+2, Europe/Kaliningrad)
TIMEZONE: str = "Europe/Kaliningrad"

# ── Logging ──
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
