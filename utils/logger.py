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

    # Silence noisy third-party loggers
    for name in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
