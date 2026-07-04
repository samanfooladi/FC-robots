"""
Credits reader — fetches the account's current coin balance.
"""

import logging
import asyncio

import httpx

from auth.session import SessionData
from utils.delays import human_delay

logger = logging.getLogger(__name__)

_BASE = "https://utas.mob.v5.prd.futc-ext.gcp.ea.com/ut/game/fc26"

_COMMON_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.ea.com",
    "Host": "utas.mob.v5.prd.futc-ext.gcp.ea.com",
}

MAX_RETRIES = 3
_RETRYABLE = {429, 500, 502, 503, 504}


def _build_headers(session: SessionData) -> dict[str, str]:
    return {**_COMMON_HEADERS, **session.ut_headers()}


async def get_credits(session: SessionData) -> int | None:
    """
    Fetch the account's current coin balance.

    Returns None on failure or 401 (session.expired is set) so callers
    can decide whether to show the balance at all.
    """
    url = f"{_BASE}/user/credits"

    for attempt in range(1, MAX_RETRIES + 1):
        await human_delay()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=_build_headers(session))

            logger.info(
                "get_credits [attempt %d] → HTTP %d",
                attempt,
                resp.status_code,
            )

            if resp.status_code == 401:
                session.expired = True
                logger.warning("Session expired (401) during credits fetch — flagged for re-login")
                return None

            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error("get_credits: gave up after %d attempts (HTTP %d)", attempt, resp.status_code)
                return None

            resp.raise_for_status()
            credits = resp.json().get("credits")
            logger.info("get_credits: %s coin(s)", credits)
            return credits

        except httpx.RequestError as exc:
            logger.warning("get_credits [attempt %d] network error: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error("get_credits: all %d attempts failed", MAX_RETRIES)
                return None

    return None
