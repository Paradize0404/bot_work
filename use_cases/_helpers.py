"""
Shared helpers for use_cases — DRY вместо дублей в каждом sync-модуле.

Конвертеры: _safe_uuid, _safe_bool, _safe_decimal, _safe_int, _safe_float.
Используются в sync.py, sync_fintablo.py, sync_stock_balances.py и т.д.
"""

import uuid
from datetime import datetime, timezone
from typing import Any


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


def utcnow() -> datetime:
    """
    Naive UTC now (без tzinfo) — совместимо с TIMESTAMP WITHOUT TIME ZONE в asyncpg.
    Замена deprecated datetime.utcnow() (Python 3.12+).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
