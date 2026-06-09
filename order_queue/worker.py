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
this flag after every HTTP call and re-runs the full Playwright login before
retrying the current operation.
"""

import asyncio
import logging
import time
from asyncio import Queue

from aiogram import Bot

from auth.login import login_to_fc
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
from bot.notifications import send_order_complete, safe_send
from utils.delays import human_delay

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────
MAX_SEARCH_RETRIES = 5       # consecutive empty searches before aborting
SEARCH_WAIT_S = 30           # seconds to wait between empty-search retries
MAX_BUY_FAILS = 3            # consecutive buy errors before aborting
CYCLE_DELAY_MIN = 2.0        # seconds — extra pause after each buy+list cycle
CYCLE_DELAY_MAX = 5.0


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
        password: str,
        otp_key: str,
        session: SessionData | None,
        bot: Bot,
    ) -> None:
        self.account_id = account_id
        self.email = email
        self.password = password
        self.otp_key = otp_key
        self.session = session
        self.bot = bot
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
            if not await self._refresh_session():
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
        client_telegram_id: int = order["client_telegram_id"]
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
        # Count already-done transactions so a bot restart mid-order
        # continues from where it left off rather than re-buying.
        cards_bought = await count_transactions_for_order(order_id)
        if cards_bought > 0:
            logger.info(
                "Order #%d: resuming — %d/%d cards already bought",
                order_id,
                cards_bought,
                quantity,
            )

        # ── Main buy/list loop ────────────────────────────────────────────────
        try:
            await self._do_process(
                order_id=order_id,
                quantity=quantity,
                cards_bought=cards_bought,
                card_config=order,
            )
        except _OrderAborted as exc:
            logger.error("Order #%d aborted: %s", order_id, exc.reason)
            await update_order_status(order_id, "failed")
            await self._notify_admins(
                f"❌ <b>Order #{order_id} failed</b>\n"
                f"Card: {card_name}\n"
                f"Reason: {exc.reason}\n"
                f"Cards bought before abort: {cards_bought}"
            )
            return

        # ── Order complete ────────────────────────────────────────────────────
        await update_order_status(order_id, "done")
        logger.info("Order #%d complete — %d cards listed", order_id, quantity)

        transactions = await get_transactions_for_order(order_id)
        await send_order_complete(
            bot=self.bot,
            client_telegram_id=client_telegram_id,
            order_id=order_id,
            transactions=transactions,
        )

    async def _do_process(
        self,
        *,
        order_id: int,
        quantity: int,
        cards_bought: int,
        card_config: dict,
    ) -> None:
        """
        Inner buy/list loop.  Raises _OrderAborted on unrecoverable failure.
        Mutates `cards_bought` via nonlocal (tracked inline).
        """
        card_name: str = card_config["card_name"]
        list_price: int = card_config["list_price"]
        start_bid: int = card_config["start_bid"]

        search_misses = 0          # consecutive empty searches
        consecutive_buy_fails = 0  # consecutive buy errors

        while cards_bought < quantity:

            # ── SEARCH ────────────────────────────────────────────────────────
            listings = await search_card(self.session, card_config)

            if self.session.expired:
                if not await self._refresh_session():
                    raise _OrderAborted("session expired during search — refresh failed")
                continue

            if not listings:
                search_misses += 1
                logger.info(
                    "Order #%d: no listings (miss %d/%d) — waiting %ds",
                    order_id,
                    search_misses,
                    MAX_SEARCH_RETRIES,
                    SEARCH_WAIT_S,
                )
                if search_misses >= MAX_SEARCH_RETRIES:
                    raise _OrderAborted(
                        f"no listings found after {MAX_SEARCH_RETRIES} retries"
                    )
                await asyncio.sleep(SEARCH_WAIT_S)
                continue

            search_misses = 0
            cheapest = listings[0]
            logger.debug(
                "Order #%d: cheapest listing trade=%d price=%d",
                order_id,
                cheapest.trade_id,
                cheapest.buy_now_price,
            )

            # ── BUY ───────────────────────────────────────────────────────────
            result = await buy_card(self.session, cheapest)

            if self.session.expired:
                if not await self._refresh_session():
                    raise _OrderAborted("session expired during buy — refresh failed")
                continue

            if result.error == "item_unavailable":
                # Item was snatched between search and bid — harmless, retry
                logger.info(
                    "Order #%d: trade=%d snatched — searching again",
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

            # ── BUY SUCCESS ───────────────────────────────────────────────────
            consecutive_buy_fails = 0
            cards_bought += 1
            logger.info(
                "Order #%d: bought item=%d at %d  (%d/%d)",
                order_id,
                result.item_id,
                result.price_paid,
                cards_bought,
                quantity,
            )

            # Persist immediately — if we crash now the restart counter
            # will skip this card.
            player_name = cheapest.player_name or card_name
            await save_transaction(
                order_id=order_id,
                card_name=card_name,
                player_name=player_name,
                bought_price=result.price_paid,
                listed_price=list_price,
                buynow_price=list_price,
            )

            # ── MOVE TO TRADEPILE ─────────────────────────────────────────────
            moved = await move_to_tradepile(self.session, result.item_id)
            if self.session.expired:
                if not await self._refresh_session():
                    raise _OrderAborted("session expired moving to tradepile — refresh failed")
                moved = await move_to_tradepile(self.session, result.item_id)

            if not moved:
                logger.warning(
                    "Order #%d: item=%d could not be moved to tradepile — skipping list",
                    order_id,
                    result.item_id,
                )
                await self._notify_admins(
                    f"⚠️ <b>Tradepile move failed</b> — order #{order_id}\n"
                    f"Item {result.item_id} was bought but could not be moved to tradepile."
                )
                await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)
                continue

            await asyncio.sleep(2)

            # ── LIST ──────────────────────────────────────────────────────────
            list_result = await list_card(
                self.session,
                result.item_id,
                list_price,
                start_bid,
            )

            if self.session.expired:
                # Attempt one more listing after re-login; if refresh fails,
                # card is bought but unlisted — log and keep going.
                if await self._refresh_session():
                    list_result = await list_card(
                        self.session,
                        result.item_id,
                        list_price,
                        start_bid,
                    )
                else:
                    logger.error(
                        "Order #%d: item=%d bought but session refresh failed — "
                        "card is unlisted in inventory",
                        order_id,
                        result.item_id,
                    )

            if list_result.success:
                logger.info(
                    "Order #%d: listed item=%d trade=%s at %d",
                    order_id,
                    result.item_id,
                    list_result.trade_id,
                    list_price,
                )
            else:
                logger.warning(
                    "Order #%d: item=%d bought but listing failed: %s",
                    order_id,
                    result.item_id,
                    list_result.error,
                )
                await self._notify_admins(
                    f"⚠️ <b>Listing failed</b> — order #{order_id}\n"
                    f"Item {result.item_id} was bought at {result.price_paid:,} "
                    f"but could not be listed. Check inventory manually."
                )

            # ── INTER-CYCLE DELAY ─────────────────────────────────────────────
            await human_delay(CYCLE_DELAY_MIN, CYCLE_DELAY_MAX)

    # ─────────────────────────────────────────────────────────────────────────
    # Session refresh
    # ─────────────────────────────────────────────────────────────────────────

    async def _refresh_session(self) -> bool:
        """
        Re-run the full Playwright login for this account.
        Updates self.session on success.  Returns True on success.
        """
        logger.info(
            "Account %d: session expired — re-logging in (%s)…",
            self.account_id,
            self.email,
        )
        # Clear the flag before attempting so we don't recurse
        if self.session is not None:
            self.session.expired = False

        new_session = await login_to_fc(
            account_id=self.account_id,
            email=self.email,
            password=self.password,
            otp_key=self.otp_key,
        )

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
