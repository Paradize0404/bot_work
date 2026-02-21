import time
from collections import defaultdict

_last_action: dict[tuple[int, str], float] = defaultdict(float)

def check_cooldown(tg_id: int, action: str, seconds: float = 1.0) -> bool:
    """
    Возвращает True если действие разрешено, False если слишком рано.
    """
    key = (tg_id, action)
    now = time.monotonic()
    if now - _last_action[key] < seconds:
        return False
    _last_action[key] = now
    return True
