"""
Retry-обёртка над AiohttpSession для aiogram.

При транзиентных сетевых ошибках (ConnectionResetError, ClientOSError и т.д.)
повторяет запрос к Telegram API с экспоненциальным backoff.

Это решает проблему:
  TelegramNetworkError: HTTP Client says - ClientOSError: [Errno 104] Connection reset by peer
"""

import asyncio
import logging

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger(__name__)

# Ошибки, при которых имеет смысл повторить запрос
_RETRYABLE = (TelegramNetworkError, TelegramServerError)


class RetryAiohttpSession(AiohttpSession):
    """
    AiohttpSession с автоматическим retry при сетевых ошибках.

    Параметры:
        max_retries  — сколько раз повторить (по умолчанию 3)
        base_delay   — начальная задержка в секундах (растёт ×2 каждую попытку)
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
        super().__init__(**kwargs)
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return await super().make_request(bot, method, timeout=timeout)

            except TelegramRetryAfter as e:
                # Telegram сам просит подождать — уважаем retry_after
                logger.warning(
                    "[retry] TelegramRetryAfter: wait %.1fs (attempt %d/%d)",
                    e.retry_after,
                    attempt,
                    self.max_retries,
                )
                await asyncio.sleep(e.retry_after)
                last_error = e

            except _RETRYABLE as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self.base_delay * (2 ** (attempt - 1))  # 1s, 2s, 4s
                    logger.warning(
                        "[retry] %s (attempt %d/%d) — retry in %.1fs: %s",
                        type(e).__name__,
                        attempt,
                        self.max_retries,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "[retry] %s (attempt %d/%d) — giving up: %s",
                        type(e).__name__,
                        attempt,
                        self.max_retries,
                        e,
                    )

        raise last_error  # type: ignore[misc]
