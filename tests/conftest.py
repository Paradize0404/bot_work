"""
conftest.py — заглушки env-переменных для тестовой среды.

Устанавливает все обязательные переменные окружения перед импортом
любого модуля проекта, чтобы config.py не падал с RuntimeError.
Должен находиться в папке tests/ и загружается pytest первым.
"""

import os

# ── Обязательные переменные ──────────────────────────────────────────
# Значения — заглушки для тестов; реальное соединение не устанавливается.
_DUMMY_VARS = {
    "REDIS_URL": "redis://localhost:6379",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "IIKO_BASE_URL": "http://localhost:9900",
    "IIKO_LOGIN": "test_login",
    "IIKO_SHA1_PASSWORD": "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # sha1('')
    "TELEGRAM_BOT_TOKEN": "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "FINTABLO_TOKEN": "test_fintablo_token",
    "GOOGLE_SHEETS_CREDENTIALS": "{}",  # невалидный, но не пустой
    "MIN_STOCK_SHEET_ID": "test_sheet_id",
    "INVOICE_PRICE_SHEET_ID": "test_sheet_id",
    "DAY_REPORT_SHEET_ID": "test_sheet_id",
    "IIKO_CLOUD_WEBHOOK_SECRET": "test_webhook_secret",
}

for _key, _val in _DUMMY_VARS.items():
    os.environ.setdefault(_key, _val)
