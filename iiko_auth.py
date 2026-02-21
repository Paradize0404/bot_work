## ────────────── Модуль авторизации в iiko API ──────────────
import httpx
import logging
import asyncio
import time

from config import IIKO_BASE_URL, IIKO_LOGIN, IIKO_SHA1_PASSWORD

# Таймауты и количество попыток авторизации
AUTH_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=None)
AUTH_ATTEMPTS = 4
AUTH_RETRY_DELAY = 3  # секунды
_TOKEN_TTL_SEC = 10 * 60  # 10 минут

logger = logging.getLogger(__name__)

# Кеш токена (monotonic clock — не зависит от timezone / NTP скачков)
_token_cache: dict[str, str | float | None] = {
    "token": None,
    "expires_mono": None,  # time.monotonic() + TTL
}


## ────────────── Получение токена авторизации ──────────────
async def get_auth_token() -> str:
    """Получить токен авторизации от iiko (async) с кешированием."""

    # Проверяем кеш
    if _token_cache["token"] and _token_cache["expires_mono"]:
        if time.monotonic() < _token_cache["expires_mono"]:
            logger.debug("✅ Используем кешированный токен")
            return _token_cache["token"]

    # Токен устарел или отсутствует - получаем новый
    auth_url = f"{IIKO_BASE_URL}/resto/api/auth"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"login": IIKO_LOGIN, "pass": IIKO_SHA1_PASSWORD}

    # Попытка с повтором при сетевых/403 ошибках
    for attempt in range(1, AUTH_ATTEMPTS + 1):
        try:
            # Используем persistent client из iiko_api если доступен,
            # иначе создаём короткоживущий (auth вызывается редко)
            try:
                from adapters.iiko_api import _get_client

                client = await _get_client()
                response = await client.post(auth_url, headers=headers, data=data)
            except ImportError:
                from config import IIKO_VERIFY_SSL

                async with httpx.AsyncClient(
                    verify=IIKO_VERIFY_SSL, timeout=AUTH_TIMEOUT
                ) as client:
                    response = await client.post(auth_url, headers=headers, data=data)

            response.raise_for_status()
            token = response.text.strip()
            if not token:
                raise ValueError("Не удалось получить токен")

            # Сохраняем в кеш на 10 минут
            _token_cache["token"] = token
            _token_cache["expires_mono"] = time.monotonic() + _TOKEN_TTL_SEC
            logger.debug("🔑 Получен новый токен, кешируем на 10 минут")

            return token

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and attempt < AUTH_ATTEMPTS:
                logger.warning(
                    "⚠️ Rate limit (403), ждём %s сек... (попытка %s/%s)",
                    AUTH_RETRY_DELAY,
                    attempt,
                    AUTH_ATTEMPTS,
                )
                await asyncio.sleep(AUTH_RETRY_DELAY)
                continue
            logger.exception(
                "[Ошибка авторизации] HTTP error на попытке %s/%s: %s",
                attempt,
                AUTH_ATTEMPTS,
                e,
            )
            raise
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as e:
            if attempt < AUTH_ATTEMPTS:
                logger.warning(
                    "⏳ Таймаут/сеть при авторизации, ждём %s сек и повторяем (%s/%s)",
                    AUTH_RETRY_DELAY,
                    attempt,
                    AUTH_ATTEMPTS,
                )
                await asyncio.sleep(AUTH_RETRY_DELAY)
                continue
            logger.exception(
                "[Ошибка авторизации] Таймаут/сеть на попытке %s/%s: %s",
                attempt,
                AUTH_ATTEMPTS,
                e,
            )
            raise
        except Exception as e:
            logger.exception(
                "[Ошибка авторизации] попытка %s/%s: %s", attempt, AUTH_ATTEMPTS, e
            )
            raise

    # Недостижимо (все ветки либо return, либо raise), но оставляем для static analysis
    raise RuntimeError("Не удалось получить токен после повторных попыток")


## ────────────── Получение базового URL ──────────────
def get_base_url() -> str:
    """Вернуть базовый URL для iiko API."""
    return IIKO_BASE_URL
