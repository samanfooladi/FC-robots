"""Per-account persistent Chrome profile directories."""

import shutil
from pathlib import Path

from config import PROFILES_DIR


def profile_dir(account_id: int) -> Path:
    """Return (and create) the persistent profile directory for an EA account."""
    path = PROFILES_DIR / str(account_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def delete_profile_dir(account_id: int, stored_path: str | None = None) -> bool:
    """
    Remove the on-disk Chrome profile(s) of an account (account deletion).
    The browser context must already be closed, otherwise Chrome still holds
    file locks on Windows. Returns True when nothing is left on disk.
    """
    candidates = {PROFILES_DIR / str(account_id)}
    if stored_path:
        candidates.add(Path(stored_path))

    gone = True
    for path in candidates:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            gone = gone and not path.exists()
    return gone
