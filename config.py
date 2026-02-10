"""
Конфигурация проекта.
Все секреты и настройки читаются из .env — никакого хардкода.
"""

import os
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

# ── FinTablo API ──
FINTABLO_BASE_URL: str = os.getenv("FINTABLO_BASE_URL", "https://api.fintablo.ru")
FINTABLO_TOKEN: str = _require("FINTABLO_TOKEN")

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# ── Webhook (Railway) ──
# Если WEBHOOK_URL задан — бот работает на вебхуке, иначе — polling.
# На Railway задайте: WEBHOOK_URL=https://<ваш-сервис>.up.railway.app
WEBHOOK_URL: str | None = os.getenv("WEBHOOK_URL")          # None → polling
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")   # путь, куда Telegram шлёт апдейты
WEBAPP_HOST: str = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT: int = int(os.getenv("PORT", "8080"))            # Railway пробрасывает PORT

# ── Logging ──
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
