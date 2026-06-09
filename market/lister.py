"""
Transfer Market listing logic.

Posts to /auctionhouse to put a purchased card up for sale.
"""

import logging
import asyncio

import httpx

from auth.session import SessionData
from utils.delays import human_delay
from .models import ListResult

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


async def move_to_tradepile(
    session: SessionData,
    item_id: int,
) -> bool:
    """
    Move *item_id* from the club/unassigned pile into the tradepile.

    EA requires the item to be in the tradepile before it can be listed
    on the Transfer Market.  Returns True on success, False on failure.
    On HTTP 401 sets session.expired = True.
    """
    url = f"{_BASE}/item"
    payload = {"itemData": [{"id": item_id, "pile": "trade"}]}

    for attempt in range(1, MAX_RETRIES + 1):
        await human_delay()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.put(
                    url,
                    json=payload,
                    headers=_build_headers(session),
                )

            logger.info(
                "move_to_tradepile [attempt %d] item_id=%d → HTTP %d",
                attempt,
                item_id,
                resp.status_code,
            )

            if resp.status_code == 401:
                session.expired = True
                logger.warning(
                    "Session expired (401) moving item_id=%d to tradepile",
                    item_id,
                )
                return False

            if resp.status_code == 400:
                logger.error(
                    "move_to_tradepile 400 for item_id=%d — response: %s",
                    item_id,
                    resp.text,
                )
                return False

            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error(
                    "move_to_tradepile: gave up after %d attempts (HTTP %d)",
                    attempt,
                    resp.status_code,
                )
                return False

            resp.raise_for_status()
            logger.info("move_to_tradepile SUCCESS item_id=%d", item_id)
            return True

        except httpx.RequestError as exc:
            logger.warning(
                "move_to_tradepile [attempt %d] network error: %s", attempt, exc
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error("move_to_tradepile: all %d attempts failed", MAX_RETRIES)
                return False

    return False


async def list_card(
    session: SessionData,
    item_id: int,
    buy_now_price: int,
    starting_bid: int,
    duration: int = 3600,
) -> ListResult:
    """
    List *item_id* on the Transfer Market.

    Parameters
    ----------
    item_id       : itemData.id returned from the buy step
    buy_now_price : price at which clients can instantly purchase the card
    starting_bid  : opening auction bid (must be a valid EA price tier,
                    lower than buy_now_price)
    duration      : auction length in seconds (default 1 hour)

    Returns a ListResult with the new trade_id (idStr) on success.
    On HTTP 401 sets session.expired = True and returns a failed ListResult.
    """
    # EA bid increments for 1000-10000 are 100 coins — snap to floor multiple
    starting_bid = (starting_bid // 100) * 100

    url = f"{_BASE}/auctionhouse"
    payload = {
        "buyNowPrice": buy_now_price,
        "duration": duration,
        "itemData": {"id": item_id},
        "startingBid": starting_bid,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        await human_delay()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=_build_headers(session),
                )

            logger.info(
                "list_card [attempt %d] item_id=%d buy_now=%d → HTTP %d",
                attempt,
                item_id,
                buy_now_price,
                resp.status_code,
            )

            # ── 401: session dead ──────────────────────────────────────────
            if resp.status_code == 401:
                session.expired = True
                logger.warning(
                    "Session expired (401) during list item_id=%d — flagged for re-login",
                    item_id,
                )
                return ListResult(success=False, error="session_expired")

            # ── 400: bad request — log body for debugging ──────────────────
            if resp.status_code == 400:
                logger.error(
                    "list_card 400 for item_id=%d buy_now=%d starting_bid=%d — response: %s",
                    item_id,
                    buy_now_price,
                    starting_bid,
                    resp.text,
                )
                return ListResult(success=False, error="bad_request_400")

            # ── transient errors ───────────────────────────────────────────
            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return ListResult(
                    success=False,
                    error=f"http_{resp.status_code}_after_{MAX_RETRIES}_attempts",
                )

            resp.raise_for_status()

            # ── success ────────────────────────────────────────────────────
            body = resp.json()
            # EA returns the new trade ID as "idStr" (string) alongside numeric "id"
            trade_id = str(body.get("idStr") or body.get("id", ""))
            logger.info(
                "list_card SUCCESS item_id=%d trade_id=%s listed_price=%d",
                item_id,
                trade_id,
                buy_now_price,
            )
            return ListResult(
                success=True,
                trade_id=trade_id,
                listed_price=buy_now_price,
            )

        except httpx.RequestError as exc:
            logger.warning("list_card [attempt %d] network error: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                return ListResult(success=False, error=str(exc))

    return ListResult(success=False, error="max_retries_exceeded")
