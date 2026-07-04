"""
OrderWorker — single-account sequential order processor.

One instance per EA account.  The worker blocks on its asyncio.Queue and
processes orders strictly one at a time, so two buy/list operations never
race on the same account.

State machine per order
────────────────────────
  pending  →  in_progress  →  done
                           ↘  failed

Session refresh
───────────────
Any 401 from the EA API sets session.expired = True.  The worker detects
this flag after every HTTP call and asks the BrowserPool to re-authenticate
this account (password-only, via its persistent browser profile) before
retrying the current operation.  The worker never launches a browser itself.
"""

import asyncio
import logging
import time
from asyncio import Queue

from aiogram import Bot

from auth.session import SessionData
from config import ADMIN_IDS
from db.database import (
    count_transactions_for_order,
    get_order_with_card,
    get_transactions_for_order,
    save_transaction,
    update_order_status,
)
from market.buyer import buy_card, search_card
from market.lister import list_card, move_to_tradepile
from market.player_names import get_player_name, get_player_position
from bot.notifications import send_order_complete, safe_send
from browser_pool.pool import BrowserPool
from utils.delays import human_delay

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────
SEARCH_WAIT_S = 30           # seconds to wait between empty-search retries
MAX_BUY_FAILS = 3            # consecutive buy errors before aborting
MAX_LIST_FAILS = 3           # move/list retries for one held card before aborting
CYCLE_DELAY_MIN = 2.0        # seconds — extra pause after each buy+list cycle
CYCLE_DELAY_MAX = 5.0

# ── Buy-price escalation ────────────────────────────────────────────────────
# Strategy: buy as many of the *same* card as possible, switching to another
# card in the rating band only when the current one runs out.  When nothing is
# found in range after SWITCHES_BEFORE_BUMP consecutive searches, raise the max
# buy price by PRICE_BUMP (keeping the min fixed) until it reaches MAX_BUY_PRICE,
# after which the order aborts rather than overpaying.
SWITCHES_BEFORE_BUMP = 3     # consecutive empty searches before raising the price
PRICE_BUMP = 50              # coins to raise the max buy price by each time
MAX_BUY_PRICE = 950          # hard ceiling on the per-card buy price


class _OrderAborted(Exception):
    """Raised inside _do_process to signal a fatal, non-recoverable failure."""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OrderWorker:
    def __init__(
        self,
        *,
        account_id: int,
        email: str,
        session: SessionData | None,
        bot: Bot,
        browser_pool: BrowserPool,
    ) -> None:
        self.account_id = account_id
        self.email = email
        self.session = session
        self.bot = bot
        self.browser_pool = browser_pool
        self.queue: Queue[int] = Queue()  # holds order_id integers

    # ─────────────────────────────────────────────────────────────────────────
    # Public
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Infinite loop — pull order IDs from the queue and process them."""
        logger.info("Worker account=%d started", self.account_id)
        while True:
            order_id: int = await self.queue.get()
            logger.info("Worker account=%d dequeued order #%d", self.account_id, order_id)
            try:
                await self._process_order(order_id)
            except asyncio.CancelledError:
                self.queue.task_done()
                raise
            except Exception:
                logger.exception(
                    "Unexpected error in worker account=%d order #%d",
                    self.account_id,
                    order_id,
                )
                try:
                    await update_order_status(order_id, "failed")
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    # ─────────────────────────────────────────────────────────────────────────
    # Order lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def _process_order(self, order_id: int) -> None:
        # ── Ensure session ────────────────────────────────────────────────────
        if self.session is None or not self.session.is_valid():
            self.session = await self.browser_pool.get_session(self.account_id)
            if self.session is None:
                logger.error(
                    "Order #%d: no valid session and re-login failed — aborting",
                    order_id,
                )
                await update_order_status(order_id, "failed")
                await self._notify_admins(
                    f"❌ <b>Order #{order_id} aborted</b>\n"
                    f"Account {self.account_id}: session unavailable and re-login failed."
                )
                return

        # ── Load order + card config from DB ─────────────────────────────────
        order = await get_order_with_card(order_id)
        if order is None:
            logger.error("Order #%d: not found in DB — skipping", order_id)
            return

        quantity: int = order["quantity"]
        card_name: str = order["card_name"]
        # Telegram ID of the admin who placed the order (None on orders that
        # predate the client-removal migration — notifications are skipped).
        ordered_by: int | None = order["ordered_by"]
        list_price: int = order["list_price"]

        # ── Mark running ──────────────────────────────────────────────────────
        await update_order_status(order_id, "in_progress")
        logger.info(
            "Order #%d started: card=%s qty=%d buy_range=%d-%d list_at=%d",
            order_id,
            card_name,
            quantity,
            order["buy_price_min"],
            order["buy_price_max"],
            list_price,
        )

        # ── Restart-safe counter ──────────────────────────────────────────────
        # One transaction is written per *listed* card, so counting them lets a
        # bot restart mid-order continue from where it left off rather than
        # re-listing cards that are already on the market.
        cards_listed = await count_transactions_for_order(order_id)
        if cards_listed > 0:
            logger.info(
                "Order #%d: resuming — %d/%d cards already listed",
                order_id,
                cards_listed,
                quantity,
            )

        # ── Main buy/list loop ────────────────────────────────────────────────
        try:
            await self._do_process(
                order_id=order_id,
                quantity=quantity,
                cards_listed=cards_listed,
                card_config=order,
            )
        except _OrderAborted as exc:
            logger.error("Order #%d aborted: %s", order_id, exc.reason)
            await update_order_status(order_id, "failed")
            # _do_process tracks progress in a local copy — recount from the
            # DB so the abort messages show the real number of listed cards.
            cards_listed = await count_transactions_for_order(order_id)
            await self._notify_admins(
                f"❌ <b>Order #{order_id} failed</b>\n"
                f"Card: {card_name}\n"
                f"Reason: {exc.reason}\n"
                f"Cards listed before abort: {cards_listed}"
            )
            if ordered_by:
                await safe_send(
                    self.bot,
                    ordered_by,
                    f"⚠️ سفارش متوقف شد — {cards_listed} از {quantity} کارت لیست شد.",
                )
            return

        # ── Order complete ────────────────────────────────────────────────────
        await update_order_status(order_id, "done")
        logger.info("Order #%d complete — %d cards listed", order_id, quantity)

        if not ordered_by:
            return

        try:
            transactions = await get_transactions_for_order(order_id)
            logger.info(
                "Order #%d complete. ordered_by=%s transactions=%d",
                order_id, ordered_by, len(transactions),
            )
            await send_order_complete(
                bot=self.bot,
                client_telegram_id=ordered_by,
                order_id=order_id,
                order_amount=order["order_amount"],
                transactions=transactions,
            )
        except Exception:
            logger.exception(
                "Order #%d: failed to send completion notification to %s",
                order_id, ordered_by,
            )

    async def _do_process(
        self,
        *,
        order_id: int,
        quantity: int,
        cards_listed: int,
        card_config: dict,
    ) -> None:
        """
        Inner buy/list loop.  Raises _OrderAborted on unrecoverable failure.

        Progress is counted in *listed* cards, not bought cards: a card is only
        counted (and its transaction saved) once it is actually on the market.
        A card that has been bought but not yet listed is "held" and the same
        card is retried for move/list — never abandoned in favour of buying
        another — so the order never silently delivers fewer cards than ordered.
        """
        card_name: str = card_config["card_name"]
        list_price: int = card_config["list_price"]
        start_bid: int = card_config["start_bid"]

        logger.info(
            "Order #%d card config: name=%s list_price=%d start_bid=%d "
            "buy_range=%d-%d rating=%s-%s",
            order_id, card_name, list_price, start_bid,
            card_config.get("buy_price_min", 0), card_config.get("buy_price_max", 0),
            card_config.get("min_rating"), card_config.get("max_rating"),
        )

        if list_price <= 0:
            raise _OrderAborted(
                f"list_price={list_price} is invalid — re-run /setcard to fix the card config"
            )

        consecutive_buy_fails = 0  # consecutive buy errors
        consecutive_list_fails = 0  # consecutive move/list errors for `held`

        # ── Buy-price escalation state ────────────────────────────────────────
        # Start at the configured max but never above the hard ceiling.  When
        # nothing is found in range, the max is raised by PRICE_BUMP (min stays
        # fixed) up to MAX_BUY_PRICE.
        current_max = min(
            card_config.get("buy_price_max", 0) or MAX_BUY_PRICE, MAX_BUY_PRICE
        )
        no_find_count = 0          # consecutive searches with nothing in range

        # ── "Buy the same card as much as possible" ───────────────────────────
        # Once a card is bought we lock onto its resourceId (the exact player +
        # version) and keep buying that same card until none are left in range,
        # then switch to whichever 80-81 card the market offers next.  A
        # resource_id configured on the order pins it to one player — no switch.
        fixed_player = bool(card_config.get("resource_id"))
        locked_resource_id: int | None = card_config.get("resource_id") or None
        locked_player_name: str = card_name

        # EA's market index lags: a card we just bought keeps showing up in the
        # next search, and re-bidding it returns HTTP 461.  Remember every trade
        # we've already bought or that came back unavailable so we always move on
        # to a different listing instead of looping on the stale one.
        seen_trade_ids: set[int] = set()

        # A card that has been bought but not yet listed.  It is retried for
        # move/list on the next iteration rather than abandoned, so a transient
        # listing failure never makes the order deliver fewer cards than ordered.
        # dict(item_id, price_paid, player_name) | None
        held: dict | None = None

        while cards_listed < quantity:

            # ── ACQUIRE (search + buy) unless we already hold a card ──────────
            if held is None:
                # ── SEARCH ────────────────────────────────────────────────────
                # Locked onto a player → search just that player so we see *all*
                # their listings in range (an open band search only returns the
                # global cheapest few, which can hide them).  Otherwise do an
                # open band search to discover whichever card is cheapest now.
                search_cfg = dict(card_config)
                search_cfg["buy_price_max"] = current_max
                if locked_resource_id:
                    search_cfg["resource_id"] = locked_resource_id
                else:
                    search_cfg.pop("resource_id", None)

                listings = await search_card(self.session, search_cfg)

                if self.session.expired:
                    if not await self._refresh_session():
                        raise _OrderAborted("session expired during search — refresh failed")
                    continue

                # Drop listings we've already bought / found unavailable, and —
                # when locked — keep only the targeted player, so the cheapest
                # *fresh* copy of the right card is picked.
                fresh = [l for l in listings if l.trade_id not in seen_trade_ids]
                if locked_resource_id:
                    fresh = [l for l in fresh if l.resource_id == locked_resource_id]

                if not fresh:
                    # Locked onto a player who is now sold out → switch to the
                    # next card in the band (unless the order is pinned to one
                    # player, in which case this counts as a price miss instead).
                    if locked_resource_id and not fixed_player:
                        logger.info(
                            "Order #%d: no more '%s' in range ≤%d — switching to another card",
                            order_id, locked_player_name, current_max,
                        )
                        locked_resource_id = None
                        continue

                    # Nothing buyable in range at all.  After SWITCHES_BEFORE_BUMP
                    # consecutive misses, raise the max price by PRICE_BUMP until
                    # the ceiling, then abort rather than overpay.
                    no_find_count += 1
                    logger.info(
                        "Order #%d: nothing in range [%d-%d] (miss %d/%d, %d stale)",
                        order_id,
                        card_config["buy_price_min"],
                        current_max,
                        no_find_count,
                        SWITCHES_BEFORE_BUMP,
                        len(listings),
                    )
                    if no_find_count >= SWITCHES_BEFORE_BUMP:
                        if current_max < MAX_BUY_PRICE:
                            new_max = min(current_max + PRICE_BUMP, MAX_BUY_PRICE)
                            logger.info(
                                "Order #%d: raising max buy price %d → %d after %d misses",
                                order_id, current_max, new_max, no_find_count,
                            )
                            current_max = new_max
                            no_find_count = 0
                            if not fixed_player:
                                locked_resource_id = None
                            continue  # search again immediately at the higher price
                        raise _OrderAborted(
                            f"no listings found in range up to the max buy price "
                            f"({MAX_BUY_PRICE}) after price escalation"
                        )
                    await asyncio.sleep(SEARCH_WAIT_S)
                    continue

                no_find_count = 0
                cheapest = fresh[0]
                if not locked_resource_id:
                    # First copy of a new card — lock onto it so subsequent
                    # iterations keep buying the same player.
                    locked_resource_id = cheapest.resource_id
                    logger.info(
                        "Order #%d: locked onto card resource=%d price=%d",
                        order_id, locked_resource_id, cheapest.buy_now_price,
                    )
                logger.debug(
                    "Order #%d: cheapest fresh listing trade=%d price=%d",
                    order_id,
                    cheapest.trade_id,
                    cheapest.buy_now_price,
                )

                # ── BUY ───────────────────────────────────────────────────────
                result = await buy_card(self.session, cheapest)

                if self.session.expired:
                    if not await self._refresh_session():
                        raise _OrderAborted("session expired during buy — refresh failed")
                    continue

                if result.error == "item_unavailable":
                    # Snatched / stale / expired — never try this trade again,
                    # search for the next one.
                    seen_trade_ids.add(cheapest.trade_id)
                    logger.info(
                        "Order #%d: trade=%d unavailable — skipping, searching again",
                        order_id,
                        cheapest.trade_id,
                    )
                    continue

                if not result.success:
                    consecutive_buy_fails += 1
                    logger.warning(
                        "Order #%d: buy failed (%d/%d): %s",
                        order_id,
                        consecutive_buy_fails,
                        MAX_BUY_FAILS,
                        result.error,
                    )
                    if consecutive_buy_fails >= MAX_BUY_FAILS:
                        raise _OrderAborted(
                            f"buy failed {MAX_BUY_FAILS} consecutive times: {result.error}"
                        )
                    await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)
                    continue

                # ── BUY SUCCESS ────────────────────────────────────────────────
                consecutive_buy_fails = 0
                consecutive_list_fails = 0
                # Never bid on this trade again — EA keeps returning it in the
                # next search until its index catches up.
                seen_trade_ids.add(cheapest.trade_id)
                # The market API itself carries no names; resolve the real
                # player name from the EA players.json metadata via resourceId,
                # falling back to the configured card name only if unavailable.
                player_name = (
                    cheapest.player_name
                    or await get_player_name(cheapest.resource_id)
                    or card_name
                )
                locked_player_name = player_name
                held = {
                    "item_id": result.item_id,
                    "price_paid": result.price_paid,
                    "player_name": player_name,
                    # Prefer position from the live API response; fall back to
                    # players.json lookup in case the API omits it.
                    "position": cheapest.position or await get_player_position(cheapest.resource_id),
                }
                logger.info(
                    "Order #%d: bought item=%d at %d  (listed %d/%d)",
                    order_id,
                    result.item_id,
                    result.price_paid,
                    cards_listed,
                    quantity,
                )

            item_id = held["item_id"]

            # ── MOVE TO TRADEPILE ─────────────────────────────────────────────
            moved = await move_to_tradepile(self.session, item_id)
            if self.session.expired:
                if not await self._refresh_session():
                    raise _OrderAborted("session expired moving to tradepile — refresh failed")
                moved = await move_to_tradepile(self.session, item_id)

            if not moved:
                consecutive_list_fails += 1
                logger.warning(
                    "Order #%d: item=%d could not be moved to tradepile "
                    "(attempt %d/%d)",
                    order_id,
                    item_id,
                    consecutive_list_fails,
                    MAX_LIST_FAILS,
                )
                if consecutive_list_fails >= MAX_LIST_FAILS:
                    await self._notify_admins(
                        f"⚠️ <b>Tradepile move failed</b> — order #{order_id}\n"
                        f"Item {item_id} was bought at {held['price_paid']:,} but "
                        f"could not be moved to tradepile after {MAX_LIST_FAILS} "
                        f"attempts. Check inventory manually."
                    )
                    raise _OrderAborted(
                        f"could not move bought item {item_id} to tradepile"
                    )
                await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)
                continue  # keep `held` and retry the same card

            await asyncio.sleep(2)

            # ── LIST ──────────────────────────────────────────────────────────
            logger.info(
                "About to list item=%d buy_now=%d start_bid=%d",
                item_id, list_price, start_bid,
            )
            list_result = await list_card(
                self.session,
                item_id,
                list_price,
                start_bid,
            )

            if self.session.expired:
                if not await self._refresh_session():
                    raise _OrderAborted("session expired during list — refresh failed")
                list_result = await list_card(
                    self.session,
                    item_id,
                    list_price,
                    start_bid,
                )

            if not list_result.success:
                consecutive_list_fails += 1
                logger.warning(
                    "Order #%d: item=%d bought but listing failed (attempt %d/%d): %s",
                    order_id,
                    item_id,
                    consecutive_list_fails,
                    MAX_LIST_FAILS,
                    list_result.error,
                )
                if consecutive_list_fails >= MAX_LIST_FAILS:
                    await self._notify_admins(
                        f"⚠️ <b>Listing failed</b> — order #{order_id}\n"
                        f"Item {item_id} was bought at {held['price_paid']:,} but "
                        f"could not be listed after {MAX_LIST_FAILS} attempts. "
                        f"Check inventory manually."
                    )
                    raise _OrderAborted(
                        f"listing failed {MAX_LIST_FAILS}x for item {item_id}: "
                        f"{list_result.error}"
                    )
                await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)
                continue  # keep `held` and retry listing the same card

            # ── LIST SUCCESS ──────────────────────────────────────────────────
            consecutive_list_fails = 0
            # Persist only now — one transaction == one card actually on the
            # market, so the restart counter and the completion summary both
            # reflect listed cards, never bought-but-unlisted ones.
            await save_transaction(
                order_id=order_id,
                card_name=card_name,
                player_name=held["player_name"],
                bought_price=held["price_paid"],
                listed_price=list_price,
                buynow_price=list_price,
                position=held.get("position"),
            )
            cards_listed += 1
            logger.info(
                "Order #%d: listed item=%d trade=%s at %d  (%d/%d)",
                order_id,
                item_id,
                list_result.trade_id,
                list_price,
                cards_listed,
                quantity,
            )
            held = None  # card delivered — clear so the next loop buys a new one

            # ── INTER-CYCLE DELAY ─────────────────────────────────────────────
            await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)

    # ─────────────────────────────────────────────────────────────────────────
    # Session refresh
    # ─────────────────────────────────────────────────────────────────────────

    async def _refresh_session(self) -> bool:
        """
        Ask the BrowserPool to re-authenticate this account (password-only,
        via its persistent browser profile).  Updates self.session on
        success.  Returns True on success.
        """
        logger.info(
            "Account %d: session expired — requesting re-login from BrowserPool (%s)…",
            self.account_id,
            self.email,
        )
        # Clear the flag before attempting so we don't recurse
        if self.session is not None:
            self.session.expired = False

        new_session = await self.browser_pool.force_relogin(self.account_id)

        if new_session:
            self.session = new_session
            logger.info("Account %d: re-login successful (new SID=%s…)", self.account_id, new_session.sid[:12])
            return True

        logger.error("Account %d: re-login FAILED", self.account_id)
        await self._notify_admins(
            f"❌ <b>Session refresh failed</b>\n"
            f"Account {self.account_id} (<code>{self.email}</code>)\n"
            f"Could not re-login. Manual intervention required."
        )
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Admin notifications
    # ─────────────────────────────────────────────────────────────────────────

    async def _notify_admins(self, text: str) -> None:
        for admin_id in ADMIN_IDS:
            await safe_send(self.bot, admin_id, text)
