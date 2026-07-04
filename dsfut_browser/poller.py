"""
Always-on DSFUT order-pickup loop.

Each cycle:
  1. navigate to the homepage
  2. if it redirected to the login page → session expired → pause & alert
  3. scan the board, drop PC-only rows
  4. click the first eligible order's Pick up button
  5. confirm the /comfortable/active redirect (a race can steal it first)
  6. on success, record a minimal stub row and keep hunting

Transient Playwright errors (detached nodes, navigation timeouts, missing
elements) are logged and the loop continues — the process never crashes on
them. Ctrl+C cancels the task and the session is closed cleanly by run().
"""

import asyncio
import json
import logging

from aiogram import Bot
from playwright.async_api import Error as PWError

from config import (
    ADMIN_IDS,
    DSFUT_BROWSER_HEADLESS,
    DSFUT_BROWSER_PROFILE_DIR,
    DSFUT_HOME_URL,
    DSFUT_PICKUP_TIMEOUT_S,
    DSFUT_POLL_INTERVAL_S,
)
from db.database import insert_dsfut_stub_order

from .board import is_eligible, scan_orders
from .session import DsfutBrowserSession

logger = logging.getLogger(__name__)

_ACTIVE_URL_GLOB = "**/comfortable/active**"


class DsfutBrowserPoller:
    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self.session = DsfutBrowserSession(
            DSFUT_BROWSER_PROFILE_DIR,
            DSFUT_BROWSER_HEADLESS,
            DSFUT_HOME_URL,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "DSFUT browser poller starting: home=%s interval=%.1fs headless=%s",
            DSFUT_HOME_URL, DSFUT_POLL_INTERVAL_S, DSFUT_BROWSER_HEADLESS,
        )
        await self.session.start()
        try:
            if not await self.session.ensure_logged_in():
                logger.error(
                    "DSFUT: cannot start polling — login was not completed. "
                    "Poller idle until restart."
                )
                await self._alert(
                    "⚠️ DSFUT poller could not log in — a manual login is needed "
                    "in the Chromium window on the server."
                )
                return
            await self._poll_loop()
        except asyncio.CancelledError:
            logger.info("DSFUT browser poller: cancel requested — shutting down")
            raise
        finally:
            await self.session.close()
            logger.info("DSFUT browser poller stopped")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        page = self.session.page
        while True:
            try:
                await self.session.goto_home()

                if await self.session.is_logged_out():
                    await self._handle_session_expired()
                    continue

                orders = await scan_orders(page)
                eligible = [o for o in orders if is_eligible(o)]

                if not eligible:
                    if orders:
                        logger.debug(
                            "DSFUT: %d order(s) on board, none eligible (console-only filter)",
                            len(orders),
                        )
                    await asyncio.sleep(DSFUT_POLL_INTERVAL_S)
                    continue

                order = eligible[0]
                logger.info(
                    "DSFUT: eligible order id=%s platform=%s coins=%s amount=%s "
                    "(%d on board, %d eligible)",
                    order["order_id"], order["platform"], order["coins_raw"],
                    order["amount_raw"], len(orders), len(eligible),
                )
                await self._try_pickup(order)
                await asyncio.sleep(DSFUT_POLL_INTERVAL_S)

            except asyncio.CancelledError:
                raise
            except PWError as exc:
                logger.warning("DSFUT: playwright error during poll — continuing: %s", exc)
                await asyncio.sleep(DSFUT_POLL_INTERVAL_S)
            except Exception:
                logger.exception("DSFUT: unexpected poll error — continuing")
                await asyncio.sleep(DSFUT_POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Pickup
    # ------------------------------------------------------------------

    async def _try_pickup(self, order: dict) -> bool:
        page = self.session.page
        selector = f'a[href="{order["href"]}"]'
        button = page.locator(selector).first

        try:
            await button.click(timeout=5_000)
        except PWError as exc:
            if await page.locator(selector).count() == 0:
                logger.info(
                    "DSFUT: order %s vanished before click — another buyer took it",
                    order["order_id"],
                )
            else:
                logger.warning("DSFUT: click failed for order %s: %s", order["order_id"], exc)
            return False

        try:
            await page.wait_for_url(_ACTIVE_URL_GLOB, timeout=int(DSFUT_PICKUP_TIMEOUT_S * 1000))
        except PWError:
            if await page.locator(selector).count() == 0:
                logger.info(
                    "DSFUT: order %s taken by another buyer (no redirect, button gone)",
                    order["order_id"],
                )
            else:
                logger.warning(
                    "DSFUT: order %s pickup not confirmed (no /comfortable/active redirect)",
                    order["order_id"],
                )
            return False

        logger.info(
            "✅ DSFUT: picked up order id=%s platform=%s coins=%s amount=%s",
            order["order_id"], order["platform"], order["coins_raw"], order["amount_raw"],
        )
        await self._record_stub(order)
        return True

    async def _record_stub(self, order: dict) -> None:
        """Minimal record of a confirmed pickup — full detail extraction is a later step."""
        raw = {
            k: order.get(k)
            for k in ("order_id", "platform", "coins_raw", "amount_raw", "coins", "amount", "href")
        }
        try:
            row_id = await insert_dsfut_stub_order(
                order_id=order["order_id"],
                platform=order["platform"],
                coins=order["coins"],
                amount=order["amount"],
                raw_json=json.dumps(raw),
            )
            if row_id is not None:
                logger.info("DSFUT: stub stored for order %s (db row %s)", order["order_id"], row_id)
            else:
                logger.info("DSFUT: order %s already recorded — skipping duplicate", order["order_id"])
        except Exception:
            logger.exception("DSFUT: failed to store stub for order %s", order["order_id"])

    # ------------------------------------------------------------------
    # Session expiry
    # ------------------------------------------------------------------

    async def _handle_session_expired(self) -> None:
        logger.error(
            "DSFUT: session expired mid-run — polling PAUSED, manual re-login required"
        )
        await self._alert(
            "⚠️ DSFUT session expired — please re-login in the Chromium window on the server."
        )
        # ensure_logged_in prompts + waits for the human; it does not hammer the
        # login page. If it still fails, back off before the loop tries again.
        if await self.session.ensure_logged_in():
            logger.info("DSFUT: re-login successful — resuming polling")
        else:
            logger.error("DSFUT: re-login not completed — waiting before re-checking")
            await asyncio.sleep(30)

    async def _alert(self, text: str) -> None:
        if not self.bot:
            return
        from bot.notifications import safe_send
        for admin_id in ADMIN_IDS:
            try:
                await safe_send(self.bot, admin_id, text)
            except Exception:
                logger.debug("DSFUT: failed to alert admin %s", admin_id, exc_info=True)
