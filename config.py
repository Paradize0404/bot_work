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
DATABASE_URL: str = _require("DATABASE_URL")

# ── iiko API ──
IIKO_BASE_URL: str = _require("IIKO_BASE_URL")
IIKO_LOGIN: str = _require("IIKO_LOGIN")
IIKO_SHA1_PASSWORD: str = _require("IIKO_SHA1_PASSWORD")

# ── FinTablo API ──
FINTABLO_BASE_URL: str = os.getenv("FINTABLO_BASE_URL", "https://api.fintablo.ru")
FINTABLO_TOKEN: str = _require("FINTABLO_TOKEN")

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# ── Logging ──
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
