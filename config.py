import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# Database
DB_PATH = Path(os.getenv("DB_PATH", "data/fc_bot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Logging
import logging as _logging
_LOG_LEVEL_MAP = {"DEBUG": _logging.DEBUG, "INFO": _logging.INFO,
                  "WARNING": _logging.WARNING, "ERROR": _logging.ERROR}
LOG_LEVEL: int = _LOG_LEVEL_MAP.get(os.getenv("LOG_LEVEL", "INFO").upper(), _logging.INFO)

# Request pacing
REQUEST_DELAY_MIN: float = float(os.getenv("REQUEST_DELAY_MIN", "0.5"))
REQUEST_DELAY_MAX: float = float(os.getenv("REQUEST_DELAY_MAX", "2.0"))

# Browser pool / persistent Chrome profiles
PROFILES_DIR = Path(os.getenv("PROFILES_DIR", "data/profiles"))
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").strip().lower() in ("1", "true", "yes")
BROWSER_HEALTH_CHECK_INTERVAL_S: int = int(os.getenv("BROWSER_HEALTH_CHECK_INTERVAL_S", "300"))
