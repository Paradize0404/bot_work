"""
Глобальная настройка логирования.
Вызывать setup_logging() один раз при старте приложения.
Логи пишутся в stdout + файл logs/app.log (ротация 5 МБ × 3 файла).
Критические ошибки (ERROR+) → уведомления админам в Telegram.
"""

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_LEVEL


class TelegramAlertHandler(logging.Handler):
    """
    Logging handler: отправляет ERROR/CRITICAL сообщения админам в Telegram.
    Debounce: не чаще 1 сообщения в 60 сек на один logger.
    Подключается после старта бота через attach_bot().
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._bot = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_sent: dict[str, float] = {}
        self._debounce_sec = 60.0

    def attach_bot(self, bot, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Привязать bot-инстанс (вызывать из on_startup/run_polling)."""
        self._bot = bot
        self._loop = loop
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        if self._bot is None:
            return

        # Debounce по имени logger'а
        now = time.monotonic()
        last = self._last_sent.get(record.name, 0)
        if (now - last) < self._debounce_sec:
            return
        self._last_sent[record.name] = now

        text = (
            f"🚨 <b>{record.levelname}</b> | {record.name}\n"
            f"<pre>{self.format(record)[:3500]}</pre>"
        )
        try:
            asyncio.ensure_future(self._send_alert(text), loop=self._loop)
        except RuntimeError:
            pass  # event loop закрыт при shutdown

    async def _send_alert(self, text: str) -> None:
        try:
            # Технические алерты — только сисадминам (🔧 Сис.Админ).
            # Если сисадмин не назначен — fallback на обычных админов.
            from use_cases.permissions import get_sysadmin_ids

            recipient_ids = await get_sysadmin_ids()
            for aid in recipient_ids[:5]:  # макс 5 получателей чтобы не спамить
                try:
                    await self._bot.send_message(aid, text, parse_mode="HTML")
                except Exception:
                    pass  # внутри log handler — нельзя логировать (рекурсия)
        except Exception:
            pass  # внутри log handler — нельзя логировать (рекурсия)


# Singleton — создаётся 1 раз, bot привязывается позже
_telegram_handler = TelegramAlertHandler()


def get_telegram_handler() -> TelegramAlertHandler:
    """Получить singleton TelegramAlertHandler для привязки бота."""
    return _telegram_handler


def setup_logging() -> None:
    """Настроить корневой логгер: консоль + файл."""

    # ── Гарантировать UTF-8 в stdout/stderr (Windows CP437/CP1251 → mojibake) ──
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass  # readonly / redirected — не фатально

    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler ──
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    # ── File handler (ротация 5 МБ × 3 файла) ──
    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    # ── Telegram handler (ERROR+) ──
    _telegram_handler.setFormatter(fmt)

    # ── DB Error handler (ERROR+ → таблица bot_error) ──
    from use_cases.error_store import get_db_error_handler

    _db_handler = get_db_error_handler()
    _db_handler.setFormatter(fmt)

    # ── DB Log handler (INFO+ → таблица bot_log, буферизованный) ──
    from use_cases.log_store import get_db_log_handler

    _db_log = get_db_log_handler()
    _db_log.setFormatter(fmt)

    # ── Root logger ──
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(_telegram_handler)
    root.addHandler(_db_handler)
    root.addHandler(_db_log)

    # Приглушаем шумные библиотеки (но НЕ наш код bot.*, use_cases.*, adapters.*)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.INFO)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.INFO)
    logging.getLogger("aiogram.middlewares").setLevel(logging.WARNING)
    logging.getLogger("aiogram.session").setLevel(
        logging.INFO
    )  # видим ошибки HTTP к Telegram
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)

    logging.info(
        "Logging initialised  level=%s  file=%s", LOG_LEVEL, log_dir / "app.log"
    )
