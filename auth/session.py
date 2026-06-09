import json
import time
import logging
from dataclasses import dataclass, asdict, field

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

# Re-login when session is older than this (EA sessions last ~1 h in practice).
SESSION_TTL = 3_600  # seconds


@dataclass
class SessionData:
    account_id: int
    sid: str               # X-UT-SID header value
    phishing_token: str    # X-UT-PHISHING-TOKEN header value
    access_token: str      # OAuth access_token cookie
    nucleus_id: str        # EA Nucleus / persona ID
    cookies: dict          # full cookie jar from Playwright (name → value)
    created_at: float = field(default_factory=time.time)
    # Set to True in-memory by market functions on HTTP 401 to signal
    # the queue that a re-login is needed before the next request.
    expired: bool = field(default=False, compare=False, repr=False)

    # ------------------------------------------------------------------
    # Validity helpers
    # ------------------------------------------------------------------

    def is_valid(self) -> bool:
        return (time.time() - self.created_at) < SESSION_TTL

    def age(self) -> int:
        return int(time.time() - self.created_at)

    # ------------------------------------------------------------------
    # HTTP headers ready for httpx
    # ------------------------------------------------------------------

    def ut_headers(self) -> dict[str, str]:
        """Return the minimal set of headers required for UT API calls."""
        return {
            "X-UT-SID": self.sid,
            "X-UT-PHISHING-TOKEN": self.phishing_token,
            "Easw-Session-Data-Nucleus-Id": self.nucleus_id,
            "Content-Type": "application/json",
        }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def save_session(session: SessionData) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE ea_accounts
               SET session_token       = :sid,
                   session_data        = :data,
                   session_created_at  = :created_at
             WHERE id = :account_id
            """,
            {
                "sid": session.sid,
                "data": json.dumps({k: v for k, v in asdict(session).items() if k != "expired"}),
                "created_at": session.created_at,
                "account_id": session.account_id,
            },
        )
        await db.commit()
    logger.info("Session saved for account %d (age 0s)", session.account_id)


async def load_session(account_id: int) -> SessionData | None:
    """
    Load a stored session from the DB.
    Returns None if no session exists or if it has expired.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT session_data FROM ea_accounts WHERE id = ?",
            (account_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row or not row[0]:
        return None

    try:
        data = json.loads(row[0])
        data.pop("expired", None)  # never restore an expired=True from disk
        session = SessionData(**data)
    except Exception as exc:
        logger.warning("Could not deserialise session for account %d: %s", account_id, exc)
        return None

    if not session.is_valid():
        logger.info(
            "Session for account %d expired (%ds old — TTL %ds)",
            account_id,
            session.age(),
            SESSION_TTL,
        )
        return None

    logger.info(
        "Loaded valid session for account %d (age %ds)",
        account_id,
        session.age(),
    )
    return session


async def invalidate_session(account_id: int) -> None:
    """Clear stored session so the next call forces a fresh login."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE ea_accounts
               SET session_token = NULL, session_data = NULL, session_created_at = NULL
             WHERE id = ?
            """,
            (account_id,),
        )
        await db.commit()
    logger.info("Session invalidated for account %d", account_id)
