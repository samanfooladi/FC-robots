"""
Tradepile reader — used by the Phase 6 accounting scheduler to reconcile
buy-now prices of currently listed cards.
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


async def get_tradepile(session: SessionData) -> list[dict]:
    """
    Fetch all entries in the account's tradepile (listed + recently sold).

    Returns the raw list of auction dicts from the EA response so the
    accounting module can extract buyNowPrice, tradeState, etc. without
    an extra abstraction layer.

    Returns an empty list on failure or 401 (session.expired is set).
    """
    url = f"{_BASE}/tradepile"

    for attempt in range(1, MAX_RETRIES + 1):
        await human_delay()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=_build_headers(session))

            logger.info(
                "get_tradepile [attempt %d] → HTTP %d",
                attempt,
                resp.status_code,
            )

            if resp.status_code == 401:
                session.expired = True
                logger.warning("Session expired (401) during tradepile fetch — flagged for re-login")
                return []

            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error("get_tradepile: gave up after %d attempts (HTTP %d)", attempt, resp.status_code)
                return []

            resp.raise_for_status()
            pile = resp.json().get("auctionInfo", [])
            logger.info("get_tradepile: %d item(s) in tradepile", len(pile))
            return pile

        except httpx.RequestError as exc:
            logger.warning("get_tradepile [attempt %d] network error: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error("get_tradepile: all %d attempts failed", MAX_RETRIES)
                return []

    return []
