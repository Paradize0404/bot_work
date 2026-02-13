"""
Глобальная настройка логирования.
Вызывать setup_logging() один раз при старте приложения.
Логи пишутся в stdout + файл logs/app.log (ротация 5 МБ × 3 файла).
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_LEVEL


def setup_logging() -> None:
    """Настроить корневой логгер: консоль + файл."""

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

    # ── Root logger ──
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    # Приглушаем шумные библиотеки (но НЕ наш код bot.*, use_cases.*, adapters.*)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.INFO)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.INFO)
    logging.getLogger("aiogram.middlewares").setLevel(logging.WARNING)
    logging.getLogger("aiogram.session").setLevel(logging.INFO)   # видим ошибки HTTP к Telegram
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)

    logging.info("Logging initialised  level=%s  file=%s", LOG_LEVEL, log_dir / "app.log")
