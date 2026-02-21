import httpx

_TRANSIENT = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def is_transient(exc: Exception) -> bool:
    """Определяет, является ли ошибка транзиентной (стоит retry)."""
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False
