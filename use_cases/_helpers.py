"""
Shared helpers for use_cases — DRY вместо дублей в каждом sync-модуле.

Конвертеры: _safe_uuid, _safe_bool, _safe_decimal, _safe_int, _safe_float.
Время: now_kgd() — текущее время по Калининграду (Europe/Kaliningrad, UTC+2).
Используются в sync.py, sync_fintablo.py, sync_stock_balances.py и т.д.
"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

# Калининградское время (UTC+2) — используется ВЕЗДЕ в проекте
KGD_TZ = ZoneInfo("Europe/Kaliningrad")


def now_kgd() -> datetime:
    """
    Текущее время по Калининграду (naive, без tzinfo).
    Совместимо с TIMESTAMP WITHOUT TIME ZONE в asyncpg.
    Используется ВЕЗДЕ вместо utcnow().
    """
    return datetime.now(KGD_TZ).replace(tzinfo=None)


def safe_uuid(v: Any) -> uuid.UUID | None:
    """Безопасное преобразование в UUID (None если невалидно)."""
    if v is None:
        return None
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError):
        return None


def safe_bool(v: Any) -> bool:
    """Безопасное преобразование в bool."""
    if isinstance(v, bool):
        return v
    return str(v).lower() == "true" if isinstance(v, str) else False


def safe_decimal(v: Any) -> float | None:
    """Безопасное преобразование в float (для Numeric-полей)."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def safe_int(v: Any) -> int | None:
    """Безопасное преобразование в int."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def safe_float(v: Any) -> float | None:
    """Безопасное преобразование в float (алиас safe_decimal)."""
    return safe_decimal(v)


import re

_SECRET_RE = re.compile(
    r'(key|token|password|secret|bearer)=([^\s&"\']+)', re.IGNORECASE
)


def mask_secrets(text: str) -> str:
    """Маскирует секреты в строке для безопасного логирования."""
    if not text:
        return text
    return _SECRET_RE.sub(r"\1=***", str(text))
