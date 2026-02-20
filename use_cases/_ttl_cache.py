"""
Reusable in-memory TTL-кеш.

Хранит пары (data, timestamp) и автоматически удаляет протухшие записи.
Используется writeoff_cache и invoice_cache.
"""

import time
from typing import Any


class TtlCache:
    """Simple in-memory TTL cache backed by a plain dict."""

    __slots__ = ("_store", "_default_ttl")

    def __init__(self, default_ttl: float = 600) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._default_ttl = default_ttl

    # ── core ──

    def get(self, key: str, *, ttl: float | None = None) -> Any | None:
        """Return cached value or ``None`` if missing / expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        data, ts = entry
        if time.monotonic() - ts > (ttl if ttl is not None else self._default_ttl):
            del self._store[key]
            return None
        return data

    def set(self, key: str, data: Any) -> None:   # noqa: A003
        """Store *data* with the current timestamp."""
        self._store[key] = (data, time.monotonic())

    # ── bulk ops ──

    def drop_matching(self, predicate: ...) -> int:
        """Delete keys matching *predicate(key)*.  Returns count."""
        keys = [k for k in self._store if predicate(k)]
        for k in keys:
            del self._store[k]
        return len(keys)

    def drop_all(self) -> int:
        """Drop every entry.  Returns count."""
        n = len(self._store)
        self._store.clear()
        return n

    # ── diagnostics ──

    def stats(self, ttl: float | None = None) -> dict:
        """Return basic cache statistics."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        now = time.monotonic()
        alive = sum(1 for _, (__, ts) in self._store.items() if now - ts < effective_ttl)
        return {"total_keys": len(self._store), "alive": alive}
