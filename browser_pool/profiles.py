"""Per-account persistent Chrome profile directories."""

from pathlib import Path

from config import PROFILES_DIR


def profile_dir(account_id: int) -> Path:
    """Return (and create) the persistent profile directory for an EA account."""
    path = PROFILES_DIR / str(account_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
