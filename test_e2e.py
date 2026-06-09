"""
End-to-end smoke test — buys exactly ONE card and lists it.

⚠️  This spends REAL coins on a REAL account.  Use a cheap, common card
    and confirm the prompt before proceeding.

Usage
─────
    python test_e2e.py <resource_id> <max_buy_price> <list_price> <start_bid>

Example (a cheap bronze card)
─────────────────────────────
    python test_e2e.py 70000000001 500 600 450

Arguments
─────────
    resource_id    EA player resource / maskedDefId
    max_buy_price  Maximum buy-now price to search for
    list_price     Price to list the card at after buying
    start_bid      Opening auction bid (must be a valid EA price tier,
                   lower than list_price)

The script will:
    1. Load session for account #1 from DB
    2. Search transfer market for the card
    3. Show the cheapest listing found
    4. Ask for confirmation (y/N)
    5. Buy 1 card
    6. List it on the transfer market
    7. Verify it appears in the tradepile
    8. Print a full result summary
"""

import asyncio
import logging
import sys
import time

from utils.logger import setup_logging
from db.database import init_db, get_all_accounts
from auth.session import load_session
from market.buyer import search_card, buy_card
from market.lister import list_card
from market.tradepile import get_tradepile


async def main() -> None:
    setup_logging(logging.INFO)
    logger = logging.getLogger("test_e2e")

    # ── Parse args ─────────────────────────────────────────────────────────
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)

    resource_id = sys.argv[1]
    try:
        max_buy_price = int(sys.argv[2])
        list_price    = int(sys.argv[3])
        start_bid     = int(sys.argv[4])
    except ValueError:
        print("Error: max_buy_price, list_price, and start_bid must be integers.")
        sys.exit(1)

    if start_bid >= list_price:
        print(f"Error: start_bid ({start_bid:,}) must be lower than list_price ({list_price:,}).")
        sys.exit(1)

    # ── Init DB ────────────────────────────────────────────────────────────
    await init_db()

    # ── Load session for account #1 ────────────────────────────────────────
    accounts = await get_all_accounts()
    if not accounts:
        print("No EA accounts found in DB.  Run main.py first to seed accounts.")
        sys.exit(1)

    account = accounts[0]
    account_id: int = account["id"]
    session = await load_session(account_id)

    if session is None:
        print(
            f"No valid session for account #{account_id} ({account['email']}).\n"
            "Run main.py first so the account logs in and saves a session."
        )
        sys.exit(1)

    logger.info(
        "Using session for account #%d (%s) — age %ds",
        account_id, account["email"], session.age()
    )

    # ── STEP 1 — Search ────────────────────────────────────────────────────
    print(f"\n[1/5] Searching for resource_id={resource_id}  maxb={max_buy_price:,} …")
    listings = await search_card(session, resource_id, max_buy_price)

    if session.expired:
        print("Session expired during search.  Re-run main.py to refresh the session.")
        sys.exit(1)

    if not listings:
        print("No listings found at or below the specified max price.  Adjust max_buy_price.")
        sys.exit(0)

    cheapest = listings[0]
    print(f"    Found {len(listings)} listing(s).  Cheapest:")
    print(f"    trade_id      = {cheapest.trade_id}")
    print(f"    item_id       = {cheapest.item_id}")
    print(f"    buy_now_price = {cheapest.buy_now_price:,}")

    # ── Confirmation prompt ────────────────────────────────────────────────
    print(
        f"\n⚠️  About to spend {cheapest.buy_now_price:,} coins to buy 1 card "
        f"and list it for {list_price:,}."
    )
    answer = input("Continue? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    # ── STEP 2 — Buy ───────────────────────────────────────────────────────
    print(f"\n[2/5] Buying trade_id={cheapest.trade_id} at {cheapest.buy_now_price:,} …")
    t_buy_start = time.monotonic()
    buy_result = await buy_card(session, cheapest)
    t_buy = time.monotonic() - t_buy_start

    if session.expired:
        print("Session expired during buy.  Re-run main.py to refresh.")
        sys.exit(1)

    if not buy_result.success:
        print(f"Buy FAILED: {buy_result.error}")
        sys.exit(1)

    print(f"    ✅ Bought item_id={buy_result.item_id} at {buy_result.price_paid:,}  ({t_buy:.1f}s)")

    # ── STEP 3 — List ──────────────────────────────────────────────────────
    print(f"\n[3/5] Listing item_id={buy_result.item_id} at {list_price:,} (bid={start_bid:,}) …")
    t_list_start = time.monotonic()
    list_result = await list_card(session, buy_result.item_id, list_price, start_bid)
    t_list = time.monotonic() - t_list_start

    if session.expired:
        print("Session expired during listing.  Card was bought but not listed — check inventory.")
        sys.exit(1)

    if not list_result.success:
        print(f"Listing FAILED: {list_result.error}")
        print("Card was bought.  Check inventory manually and list it from the web app.")
        sys.exit(1)

    print(f"    ✅ Listed as trade_id={list_result.trade_id}  ({t_list:.1f}s)")

    # ── STEP 4 — Tradepile verification ────────────────────────────────────
    print("\n[4/5] Verifying card appears in tradepile …")
    pile = await get_tradepile(session)

    if session.expired:
        print("Session expired during tradepile fetch — skipping verification.")
        pile = []

    found_in_pile = any(
        str(entry.get("tradeId")) == str(list_result.trade_id)
        for entry in pile
    )

    if found_in_pile:
        print(f"    ✅ trade_id={list_result.trade_id} confirmed in tradepile")
    else:
        print(
            f"    ⚠️  trade_id={list_result.trade_id} NOT found in tradepile snapshot "
            f"({len(pile)} entries).  It may still appear — EA sometimes delays indexing."
        )

    # ── STEP 5 — Summary ───────────────────────────────────────────────────
    print("\n[5/5] Summary")
    print("─" * 40)
    print(f"  Resource ID   : {resource_id}")
    print(f"  Item ID       : {buy_result.item_id}")
    print(f"  Bought at     : {buy_result.price_paid:,}")
    print(f"  Listed at     : {list_price:,}  (buy-now)")
    print(f"  Starting bid  : {start_bid:,}")
    print(f"  Trade ID      : {list_result.trade_id}")
    print(f"  In tradepile  : {'yes' if found_in_pile else 'unconfirmed'}")
    expected_profit = (list_price * 0.95) - buy_result.price_paid
    print(f"  Expected net  : {expected_profit:,.0f} coins (after 5% EA tax)")
    print("─" * 40)
    print("E2E test PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
