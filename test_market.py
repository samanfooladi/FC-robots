"""
Smoke-test for Phase 2: Market Module.

Loads the session for account #1 from the DB and runs a Transfer Market
search to verify that the search endpoint and header auth work.
Does NOT buy or list anything.

Usage
-----
    python test_market.py [resource_id] [max_price]

Defaults
--------
    resource_id : 70000000001  (placeholder — replace with a real player ID)
    max_price   : 5000
"""

import asyncio
import sys
import logging

from utils.logger import setup_logging
from db.database import init_db, get_account_db_id
from auth.session import load_session
from config import EA_ACCOUNTS
from market.buyer import search_card


async def main() -> None:
    setup_logging(logging.INFO)
    logger = logging.getLogger("test_market")

    resource_id: str = sys.argv[1] if len(sys.argv) > 1 else "70000000001"
    max_price: int = int(sys.argv[2]) if len(sys.argv) > 2 else 5000

    # ── 1. Init DB & resolve account id ───────────────────────────────────
    await init_db()

    if not EA_ACCOUNTS:
        logger.error("No EA accounts found in .env — add EA_ACCOUNT_1_* variables")
        return

    first_account = EA_ACCOUNTS[0]
    account_id = await get_account_db_id(first_account["email"])
    if account_id is None:
        logger.error("Account not found in DB for email: %s", first_account["email"])
        return

    # ── 2. Load session ────────────────────────────────────────────────────
    session = await load_session(account_id)
    if session is None:
        logger.error(
            "No valid session for account %d — run main.py first to log in",
            account_id,
        )
        return

    logger.info(
        "Using session for account %d (age %ds, SID=%s…)",
        account_id,
        session.age(),
        session.sid[:12],
    )

    # ── 3. Search Transfer Market ──────────────────────────────────────────
    logger.info(
        "Searching for resource_id=%s with maxb=%d …",
        resource_id,
        max_price,
    )
    listings = await search_card(session, resource_id, max_price)

    # ── 4. Report results ──────────────────────────────────────────────────
    if not listings:
        print("\nNo listings found (or search failed — check logs above).")
        if session.expired:
            print("Session was flagged as expired — re-login required.")
        return

    cheapest = listings[0]
    print(f"\nFound {len(listings)} listing(s). Cheapest:")
    print(f"  trade_id      : {cheapest.trade_id}")
    print(f"  item_id       : {cheapest.item_id}")
    print(f"  buy_now_price : {cheapest.buy_now_price:,}")
    print(f"  start_price   : {cheapest.start_price:,}")
    print(f"  resource_id   : {cheapest.resource_id}")

    if len(listings) > 1:
        print(f"\nAll buy-now prices: {[l.buy_now_price for l in listings]}")


if __name__ == "__main__":
    asyncio.run(main())
