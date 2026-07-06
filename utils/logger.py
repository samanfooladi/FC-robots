import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_LEVEL

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)


def setup_logging(level: int | None = None) -> None:
    """Configure root logger.  Uses LOG_LEVEL from .env when *level* is None."""
    if level is None:
        level = LOG_LEVEL
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Make the console tolerant of characters outside its native code page
    # (emoji in log lines, accented player names) so Unicode never raises inside
    # logging on a Windows cp1252 console. The file handler is already UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            _LOG_DIR / "fc_bot.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ]

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Silence noisy third-party loggers.
    # aiosqlite is security-critical, not just noise: at DEBUG it logs every SQL
    # statement WITH its bound parameters — which for dsfut_orders/ea_accounts
    # includes account email/password/backup codes. Pin it to WARNING so those
    # never reach stdout or the log file even when LOG_LEVEL=DEBUG.
    for name in ("httpx", "httpcore", "playwright", "asyncio", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
