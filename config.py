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

# DSFUT board poller (browser automation — the partner API served a different
# queue and never returned credentials, so we drive the website instead).
DSFUT_ENABLED: bool = os.getenv("DSFUT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# The comfort-trade order board we poll for pickable orders and return to after
# each pickup (a claimed order goes to /comfortable/active).
DSFUT_BOARD_URL: str = os.getenv("DSFUT_BOARD_URL", "https://dsfut.net/comfortable").strip()
# Persistent Chromium profile (keeps the captcha login across restarts).
DSFUT_BROWSER_PROFILE_DIR = Path(os.getenv("DSFUT_BROWSER_PROFILE_DIR", "data/dsfut_profile"))
DSFUT_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
# Headed by default — a human must see the window to solve the login captcha.
DSFUT_BROWSER_HEADLESS: bool = os.getenv("DSFUT_BROWSER_HEADLESS", "false").strip().lower() in ("1", "true", "yes")
# Fast HTTP loop: how often to poll /api/json/comfortables (the site's own
# frontend polls every few seconds; we go faster for a competitive edge) and
# the per-request HTTP timeout.
DSFUT_POLL_INTERVAL_S: float = float(os.getenv("DSFUT_POLL_INTERVAL_S", "0.4"))
DSFUT_HTTP_TIMEOUT_S: float = float(os.getenv("DSFUT_HTTP_TIMEOUT_S", "15"))
