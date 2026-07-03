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

# Per-account limits and request pacing
MAX_CLIENTS_PER_ACCOUNT: int = int(os.getenv("MAX_CLIENTS_PER_ACCOUNT", "5"))
REQUEST_DELAY_MIN: float = float(os.getenv("REQUEST_DELAY_MIN", "0.5"))
REQUEST_DELAY_MAX: float = float(os.getenv("REQUEST_DELAY_MAX", "2.0"))

# Browser pool / persistent Chrome profiles
PROFILES_DIR = Path(os.getenv("PROFILES_DIR", "data/profiles"))
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "false").strip().lower() in ("1", "true", "yes")
BROWSER_HEALTH_CHECK_INTERVAL_S: int = int(os.getenv("BROWSER_HEALTH_CHECK_INTERVAL_S", "300"))


def load_ea_accounts() -> list[dict]:
    """Read EA_ACCOUNT_N_* triplets from env, return sorted list."""
    accounts: list[dict] = []
    i = 1
    while True:
        email = os.getenv(f"EA_ACCOUNT_{i}_EMAIL")
        if not email:
            break
        accounts.append(
            {
                "index": i,
                "email": email,
                "password": os.getenv(f"EA_ACCOUNT_{i}_PASSWORD", ""),
                "otp_key": os.getenv(f"EA_ACCOUNT_{i}_OTP_KEY", ""),
                "backup_code": os.getenv(f"EA_ACCOUNT_{i}_BACKUP_CODE", ""),
            }
        )
        i += 1
    return accounts


EA_ACCOUNTS: list[dict] = load_ea_accounts()
