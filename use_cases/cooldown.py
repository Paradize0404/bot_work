import time
from collections import defaultdict

_last_action: dict[tuple[int, str], float] = defaultdict(float)

# Кол-во операций между чистками протухших записей
_CLEANUP_INTERVAL = 500
_ops_since_cleanup = 0


def _cleanup_expired(max_age: float = 3600.0) -> None:
    """Удалить записи старше max_age секунд (по умолчанию 1 час)."""
    now = time.monotonic()
    expired = [k for k, v in _last_action.items() if (now - v) > max_age]
    for k in expired:
        del _last_action[k]


def check_cooldown(tg_id: int, action: str, seconds: float = 1.0) -> bool:
    """
    Возвращает True если действие разрешено, False если слишком рано.
    Автоматически очищает протухшие записи каждые 500 вызовов.
    """
    global _ops_since_cleanup
    _ops_since_cleanup += 1
    if _ops_since_cleanup >= _CLEANUP_INTERVAL:
        _ops_since_cleanup = 0
        _cleanup_expired()

    key = (tg_id, action)
    now = time.monotonic()
    if now - _last_action[key] < seconds:
        return False
    _last_action[key] = now
    return True
