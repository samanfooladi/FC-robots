"""
DSFUT order poller — fast HTTP loop with Playwright only for login.

Startup / re-login uses the persistent Playwright context (manual captcha,
handled in session.py). Once authenticated, the session cookies are handed to
an httpx client and the hot loop is pure HTTP:

  1. GET /api/json/comfortables            (poll, ~every 100 ms)
  2. filter PlayStation/Xbox orders, skip PC
  3. GET /comfortable/pickup/{id}/{hash}   (claim ASAP — races finish in seconds)
  4. GET /comfortable/active               (verify + read <fc-comfortable>)
  5. store credentials in dsfut_orders, auto-create the EA account, notify admins

If any request is bounced to the login page (SessionExpired), the loop stops,
re-runs the Playwright login, rebuilds the HTTP client with fresh cookies, and
resumes.

SECURITY: account email / password / backup codes are written to the DB and the
admin Telegram message only. They never appear in logs — emails are redacted.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx
from aiogram import Bot

from config import (
    ADMIN_IDS,
    DSFUT_BOARD_URL,
    DSFUT_BROWSER_HEADLESS,
    DSFUT_BROWSER_PROFILE_DIR,
    DSFUT_HTTP_TIMEOUT_S,
    DSFUT_POLL_INTERVAL_S,
)
from db.database import (
    add_account,
    insert_dsfut_order,
    link_dsfut_order_account,
    update_account_credentials,
)
from utils.redact import redact_email

from .http_client import DsfutHttpClient, SessionExpired
from .parser import (
    eligible_orders,
    order_already_taken,
    parse_active_order,
    redact_html_for_debug,
)
from .session import _USER_AGENT, DsfutBrowserSession

logger = logging.getLogger(__name__)

_MAX_BACKOFF_S = 30.0
# Credential VALUES are redacted before saving (see parser.redact_html_for_debug)
# — only the markup structure is kept, to diagnose parser misses against the
# real page without persisting real account credentials outside the DB.
_DEBUG_DIR = Path("data/dsfut_debug")


def _ts_ms() -> str:
    """Wall-clock timestamp with millisecond precision, for race-timing logs."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class DsfutBrowserPoller:
    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self.session = DsfutBrowserSession(
            DSFUT_BROWSER_PROFILE_DIR,
            DSFUT_BROWSER_HEADLESS,
            DSFUT_BOARD_URL,
        )
        self.http = DsfutHttpClient(DSFUT_BOARD_URL, _USER_AGENT, DSFUT_HTTP_TIMEOUT_S)
        # Orders already claimed/lost this run — avoids re-attempting a pickup
        # every poll while the same id lingers on the board.
        self._handled: set[str] = set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "DSFUT poller starting: board=%s poll=%.2fs headless=%s (HTTP fast loop)",
            DSFUT_BOARD_URL, DSFUT_POLL_INTERVAL_S, DSFUT_BROWSER_HEADLESS,
        )
        try:
            if not await self._login_and_load_cookies():
                await self._alert(
                    "⚠️ DSFUT poller could not log in — a manual login is needed "
                    "in the Chromium window on the server."
                )
                return
            await self._fast_loop()
        except asyncio.CancelledError:
            logger.info("DSFUT poller: cancel requested — shutting down")
            raise
        finally:
            await self.http.aclose()
            await self.session.close()
            logger.info("DSFUT poller stopped")

    # ------------------------------------------------------------------
    # Login / cookie handoff (Playwright)
    # ------------------------------------------------------------------

    async def _login_and_load_cookies(self) -> bool:
        """
        Bring up the Playwright context, ensure we are logged in, copy the
        cookies into the HTTP client, then close the browser (the fast loop is
        HTTP-only, so we don't hold the profile lock or a window open).
        """
        await self.session.start()
        try:
            if not await self.session.ensure_logged_in():
                logger.error("DSFUT: login was not completed — poller idle until restart")
                return False
            count = await self.http.rebuild(await self.session.export_cookies())
            logger.info("DSFUT: loaded %d session cookie(s) into the HTTP client", count)
            return True
        finally:
            await self.session.close()

    # ------------------------------------------------------------------
    # Fast loop
    # ------------------------------------------------------------------

    async def _fast_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                data = await self.http.poll_comfortables()
                orders = eligible_orders(data)
                if orders:
                    await self._handle_batch(orders)
                backoff = 1.0
                await asyncio.sleep(DSFUT_POLL_INTERVAL_S)

            except asyncio.CancelledError:
                raise
            except SessionExpired as exc:
                logger.error("DSFUT: session expired (%s) — re-login via Playwright", exc)
                await self._alert("⚠️ DSFUT session expired — re-login in the Chromium window on the server.")
                if await self._login_and_load_cookies():
                    logger.info("DSFUT: re-login successful — resuming fast loop")
                else:
                    logger.error("DSFUT: re-login not completed — waiting before retry")
                    await asyncio.sleep(_MAX_BACKOFF_S)
            except httpx.HTTPError as exc:
                logger.warning("DSFUT: HTTP error in fast loop — backoff %.1fs: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
            except Exception:
                logger.exception("DSFUT: unexpected fast-loop error — continuing")
                await asyncio.sleep(1.0)

    async def _handle_batch(self, orders: list[dict]) -> None:
        for order in orders:
            oid = str(order.get("id"))
            if oid in self._handled:
                continue
            # Timestamp the moment this order was first seen eligible, right
            # after poll_comfortables() returned — the baseline for race timing.
            detected_at = time.monotonic()
            logger.info(
                "DSFUT: order %s detected at %s (poll_comfortables returned an "
                "eligible order)",
                oid, _ts_ms(),
            )
            if await self._attempt(order, detected_at):
                # One successful pickup per cycle; the next poll continues.
                return

    async def _attempt(self, order: dict, detected_at: float) -> bool:
        oid = str(order.get("id"))
        order_hash = order.get("hash")

        logger.info("DSFUT: order %s — sending pickup request at %s", oid, _ts_ms())
        redirected = await self.http.pickup(oid, order_hash, detected_at=detected_at)
        if not redirected:
            logger.info(
                "DSFUT: pickup for order %s not confirmed (no redirect to "
                "/comfortable/active) — likely already gone",
                oid,
            )
            return False

        html = await self.http.get_active()

        # (a) Lost the race — the active page shows the "already taken" banner.
        if order_already_taken(html):
            logger.info("DSFUT: order %s lost — another supplier took it first", oid)
            self._handled.add(oid)
            return False

        details = parse_active_order(html, oid)

        # (c) No matching <fc-comfortable> AND no "already taken" banner — the
        # active page has no order for us at all (e.g. "There is no information
        # to display"). Despite the 302, our pickup most likely never claimed
        # anything server-side (someone beat us to it, or it didn't take effect).
        # Nothing to check manually, so NO admin alert — just log distinctly and
        # keep a redacted dump so a high rate of this points at the pickup
        # endpoint rather than the parser.
        if details is None:
            self._handled.add(oid)
            dump_path = self._save_debug_html(oid, html, suffix="_notclaimed")
            logger.info(
                "DSFUT: pickup(%s) returned 302 but /comfortable/active shows no "
                "order for us at all — likely claimed by someone else before our "
                "request, or the pickup didn't take effect; redacted HTML saved to %s",
                oid, dump_path,
            )
            return False

        # A matching <fc-comfortable id="oid"> block IS present, so this order is
        # ours — but the credentials could not be extracted. That is a real
        # parser problem on a REAL claimed order: alert a human to grab it.
        if not details.get("email"):
            self._handled.add(oid)
            dump_path = self._save_debug_html(oid, html)
            logger.warning(
                "DSFUT: order %s — matching <fc-comfortable> found but credentials "
                "could not be extracted (parser miss on a claimed order) — redacted "
                "HTML saved to %s; alerting admins to check manually",
                oid, dump_path,
            )
            await self._alert_unparsed(order)
            return False

        # (b) Success.
        self._handled.add(oid)
        await self._store_and_notify(order, details)
        return True

    def _save_debug_html(self, oid: str, html_text: str, suffix: str = "") -> str:
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = _DEBUG_DIR / f"{oid}_{int(time.time())}{suffix}.html"
            path.write_text(redact_html_for_debug(html_text), encoding="utf-8")
            return str(path)
        except Exception:
            logger.debug("DSFUT: failed to save debug HTML for order %s", oid, exc_info=True)
            return "(save failed)"

    async def _alert_unparsed(self, order: dict) -> None:
        oid = order.get("id")
        coins = order.get("coins")
        coins_s = f"{coins:,}" if isinstance(coins, (int, float)) else str(coins)
        price = order.get("price") or order.get("dsfut_price")
        await self._alert(
            f"⚠️ DSFUT: order {oid} was likely claimed but the account credentials "
            f"could not be read automatically from the page.\n"
            f"Coins: {coins_s} | Price: ${price}\n"
            f"Please check dsfut.net → Comfortable → Active RIGHT NOW and handle it manually."
        )

    # ------------------------------------------------------------------
    # Persist + notify (credentials go to DB + Telegram only)
    # ------------------------------------------------------------------

    async def _store_and_notify(self, order: dict, details: dict) -> None:
        oid = str(order.get("id"))
        email = details["email"]
        password = details["password"]
        codes = ",".join(details.get("backup_codes") or [])
        coins = order.get("coins") or details.get("coins") or 0
        price = order.get("price") or order.get("dsfut_price") or details.get("amount")
        console = str(order.get("console_full_name") or order.get("console") or "")

        raw = json.dumps({
            "id": order.get("id"),
            "console": order.get("console"),
            "console_full_name": order.get("console_full_name"),
            "coins": order.get("coins"),
            "price": order.get("price"),
            "dsfut_price": order.get("dsfut_price"),
            "amount_raw": details.get("amount_raw"),
            "coins_raw": details.get("coins_raw"),
        })

        row_id = await insert_dsfut_order(
            transaction_id=int(oid) if oid.isdigit() else None,
            trade_id=oid,
            name="",
            rating=None,
            position="",
            start_price=None,
            buy_now_price=coins or None,
            amount=float(price) if price is not None else None,
            net_price=float(price) if price is not None else None,
            expires=None,
            console=console,
            account_email=email,
            account_password=password,
            account_backup_code=codes,
            raw_json=raw,
        )
        if row_id is None:
            logger.info("DSFUT: order %s already stored — skipping duplicate", oid)
            return

        note = await self._provision_account(row_id, email, password, codes)

        logger.info(
            "✅ DSFUT: picked up & stored order id=%s coins=%s account=%s",
            oid, coins, redact_email(email),
        )
        if self.bot:
            await send_dsfut_notification(
                self.bot, coins=coins or 0,
                net_price=float(price) if price is not None else None,
                email=email, password=password, backup_codes=codes, account_note=note,
            )

    async def _provision_account(self, row_id: int, email: str, password: str, codes: str) -> str:
        """Auto-create (or refresh) the EA account row for /accounts login."""
        if not (email and password):
            return "no credentials in the order — add the account via /addaccount"

        created, res = await add_account(email, password, codes)
        if created:
            await link_dsfut_order_account(row_id, int(res))
            return ""
        if res == "already_exists":
            account_id = await update_account_credentials(email, password, codes)
            if account_id is not None:
                await link_dsfut_order_account(row_id, account_id)
            return "account already existed — credentials refreshed"
        return "could not auto-create the account — add it via /addaccount"

    # ------------------------------------------------------------------
    # Admin alerts
    # ------------------------------------------------------------------

    async def _alert(self, text: str) -> None:
        if not self.bot:
            return
        from bot.notifications import safe_send
        for admin_id in ADMIN_IDS:
            try:
                await safe_send(self.bot, admin_id, text)
            except Exception:
                logger.debug("DSFUT: failed to alert admin %s", admin_id, exc_info=True)


async def send_dsfut_notification(bot, **kwargs) -> None:
    """Thin wrapper (lazy import) around the existing admin notification."""
    from bot.notifications import send_dsfut_order_created
    await send_dsfut_order_created(bot, ADMIN_IDS, **kwargs)
