"""
Transfer Market search and buy-now logic.

All HTTP calls use httpx.AsyncClient with the session headers from Phase 1.
Each public function applies a human-like random delay before the request
and retries transient failures up to MAX_RETRIES times.
"""

import logging
import asyncio

import httpx

from auth.session import SessionData
from utils.delays import human_delay
from .models import CardListing, BuyResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://utas.mob.v5.prd.futc-ext.gcp.ea.com/ut/game/fc26"

_COMMON_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.ea.com",
    "Host": "utas.mob.v5.prd.futc-ext.gcp.ea.com",
}

MAX_RETRIES = 3
# HTTP status codes that are worth retrying
_RETRYABLE = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_headers(session: SessionData) -> dict[str, str]:
    return {**_COMMON_HEADERS, **session.ut_headers()}


def _parse_listings(raw: dict) -> list[CardListing]:
    """Turn the raw auctionInfo list into typed CardListing objects."""
    listings: list[CardListing] = []
    for auction in raw.get("auctionInfo", []):
        item = auction.get("itemData", {})
        buy_now = auction.get("buyNowPrice", 0)
        if buy_now <= 0:
            continue  # not available for buy-now
        player_name = (
            item.get("commonName")
            or item.get("lastName")
            or item.get("name")
            or ""
        )
        listings.append(
            CardListing(
                trade_id=int(auction.get("tradeId", 0)),
                item_id=int(item.get("id", 0)),
                buy_now_price=buy_now,
                resource_id=int(item.get("resourceId", item.get("maskedDefId", 0))),
                start_price=int(auction.get("startingBid", 0)),
                player_name=player_name,
            )
        )
    return sorted(listings, key=lambda c: c.buy_now_price)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_card(
    session: SessionData,
    card_config: dict,
) -> list[CardListing]:
    """
    Search the Transfer Market using the full card config from the DB.

    Builds query params from rarity/level/rating/resource_id fields and
    filters the results to listings within [buy_price_min, buy_price_max].
    Returns listings sorted by buy_now_price ascending.
    Returns an empty list when nothing is found or the request fails.
    """
    url = f"{_BASE}/transfermarket"
    params: dict = {
        "num": 21,
        "start": 0,
        "type": "player",
        "rarityIds": card_config["rarity_ids"],
        "lev": card_config["lev"],
        "maxb": card_config["buy_price_max"],
    }
    if card_config.get("min_rating"):
        params["minrating"] = card_config["min_rating"]
    if card_config.get("max_rating"):
        params["maxrating"] = card_config["max_rating"]
    if card_config.get("resource_id"):
        params["maskedDefId"] = card_config["resource_id"]

    buy_price_min: int = card_config.get("buy_price_min", 0)
    card_name: str = card_config.get("card_name", "?")

    for attempt in range(1, MAX_RETRIES + 1):
        await human_delay()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    url,
                    params=params,
                    headers=_build_headers(session),
                )

            logger.info(
                "search_card [attempt %d] card=%s buy_range=%d-%d → HTTP %d",
                attempt,
                card_name,
                buy_price_min,
                card_config["buy_price_max"],
                resp.status_code,
            )

            if resp.status_code == 401:
                session.expired = True
                logger.warning("Session expired (401) during search — flagged for re-login")
                return []

            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error("search_card: gave up after %d attempts (HTTP %d)", attempt, resp.status_code)
                return []

            resp.raise_for_status()
            all_listings = _parse_listings(resp.json())
            listings = [l for l in all_listings if l.buy_now_price >= buy_price_min]
            logger.info(
                "search_card: %d listing(s) in range [%d-%d] for %s (cheapest=%s)",
                len(listings),
                buy_price_min,
                card_config["buy_price_max"],
                card_name,
                listings[0].buy_now_price if listings else "—",
            )
            return listings

        except httpx.RequestError as exc:
            logger.warning("search_card [attempt %d] network error: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error("search_card: all %d attempts failed", MAX_RETRIES)
                return []

    return []


async def buy_card(
    session: SessionData,
    listing: CardListing,
) -> BuyResult:
    """
    Buy-now a single listing via PUT /trade/{trade_id}/bid.

    On HTTP 401 sets session.expired = True and returns a failed BuyResult
    so the queue can trigger a re-login before the next card.
    """
    url = f"{_BASE}/trade/{listing.trade_id}/bid"
    payload = {"bid": listing.buy_now_price}

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
                "buy_card [attempt %d] trade_id=%d price=%d → HTTP %d",
                attempt,
                listing.trade_id,
                listing.buy_now_price,
                resp.status_code,
            )

            # ── 401: session dead ──────────────────────────────────────────
            if resp.status_code == 401:
                session.expired = True
                logger.warning(
                    "Session expired (401) during buy trade_id=%d — flagged for re-login",
                    listing.trade_id,
                )
                return BuyResult(success=False, error="session_expired")

            # ── 422: item already gone or bid too low ──────────────────────
            if resp.status_code == 422:
                logger.warning(
                    "buy_card trade_id=%d: item no longer available (422)",
                    listing.trade_id,
                )
                return BuyResult(success=False, error="item_unavailable")

            # ── transient server errors ────────────────────────────────────
            if resp.status_code in _RETRYABLE:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return BuyResult(
                    success=False,
                    error=f"http_{resp.status_code}_after_{MAX_RETRIES}_attempts",
                )

            resp.raise_for_status()

            # ── success ────────────────────────────────────────────────────
            body = resp.json()
            bought_item_id = (
                body.get("auctionInfo", [{}])[0]
                    .get("itemData", {})
                    .get("id", listing.item_id)
            )
            logger.info(
                "buy_card SUCCESS trade_id=%d item_id=%d price=%d",
                listing.trade_id,
                bought_item_id,
                listing.buy_now_price,
            )
            return BuyResult(
                success=True,
                item_id=bought_item_id,
                price_paid=listing.buy_now_price,
            )

        except httpx.RequestError as exc:
            logger.warning("buy_card [attempt %d] network error: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
            else:
                return BuyResult(success=False, error=str(exc))

    return BuyResult(success=False, error="max_retries_exceeded")
