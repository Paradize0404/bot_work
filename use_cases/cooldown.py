"""
In-memory cooldown / rate limiting для handler'ов бота.

Паттерн:
    if not check_cooldown(callback.from_user.id, "sync", seconds=5.0):
        await callback.answer("⏳ Подождите...")
        return

Cleanup: протухшие записи чистятся каждые 100 вызовов check_cooldown().
"""

import logging
import time

logger = logging.getLogger(__name__)

# {(tg_id, action): last_call_timestamp}
_store: dict[tuple[int, str], float] = {}
_call_counter: int = 0
_CLEANUP_EVERY: int = 100
_MAX_COOLDOWN: float = 60.0  # запись старше этого — точно протухла


def check_cooldown(tg_id: int, action: str, seconds: float = 1.0) -> bool:
    """
    Проверить, прошло ли достаточно времени с последнего вызова.

    Returns True если действие разрешено (cooldown прошёл),
    False если ещё рано (нужно подождать).
    """
    global _call_counter
    _call_counter += 1
    if _call_counter >= _CLEANUP_EVERY:
        _cleanup()
        _call_counter = 0

    key = (tg_id, action)
    now = time.monotonic()
    last = _store.get(key)
    if last is not None and (now - last) < seconds:
        logger.debug("[cooldown] tg:%d action=%s blocked (%.1fs < %.1fs)",
                     tg_id, action, now - last, seconds)
        return False

    _store[key] = now
    return True


def reset(tg_id: int, action: str) -> None:
    """Сбросить cooldown для конкретного пользователя/действия."""
    _store.pop((tg_id, action), None)


def reset_all() -> None:
    """Сбросить все cooldown'ы (для тестов)."""
    _store.clear()


def _cleanup() -> None:
    """Удалить протухшие записи."""
    now = time.monotonic()
    expired = [k for k, v in _store.items() if (now - v) > _MAX_COOLDOWN]
    for k in expired:
        del _store[k]
    if expired:
        logger.debug("[cooldown] Очищено %d протухших записей", len(expired))
