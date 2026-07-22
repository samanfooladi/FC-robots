"""
Playwright login flow for the FC Companion Web App.

Two entry points, both operating on a page that belongs to a *persistent*
browser context owned by browser_pool.BrowserPool — this module never
launches or closes a browser itself.

first_login()
    Full credential flow: email -> password -> backup code -> capture
    session. Used once per account, the first time its persistent profile
    is created. Ticks "remember this device" so subsequent restores skip
    2FA. Backup codes are single-use — the caller wipes the stored code
    after a successful first login.

restore_session()
    Navigates the already-authenticated persistent page to the web app and
    captures a fresh session without re-entering any credentials. Returns
    None if the profile's session has actually died (login form appears),
    in which case the caller should fall back to a password-only re-login.

NOTE: EA occasionally changes login-page selectors; update the constants at
the top of this file if the flow breaks. The 2FA selectors were verified
against the live "Verify your identity" / "Enter your code" pages (July
2026): Send Code is clicked with the pre-selected method, and the backup
code goes into the same #twoFactorCode input.
"""

import asyncio
import logging
import re
import time
from typing import Any

from playwright.async_api import (
    Page,
    Request,
    Response,
    TimeoutError as PWTimeout,
)

from .session import SessionData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — adjust if EA changes their login page
# ---------------------------------------------------------------------------

FC_WEBAPP_URL = "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/"

# EA login selectors
SEL_EMAIL = "#email"
SEL_BTN_NEXT = "#logInBtn"
SEL_PASSWORD = "#password"
SEL_BTN_SIGNIN = "#logInBtn"
# "Verify your identity" intermediate page — the preferred verification
# method is pre-selected, so we only need to click Send Code.
SEL_BTN_SEND_CODE = "#btnSendCode"

# "Enter your code" page — the backup code goes into the one-time-code input.
# <input type="text" id="twoFactorCode" name="oneTimeCode" …>
SEL_2FA_CODE_INPUT = "#twoFactorCode"
# <a role="button" id="btnSubmit" …>NEXT</a>
SEL_BTN_2FA_SUBMIT = "#btnSubmit"

# UT API fingerprint — we listen for this URL to grab session headers
UT_ACCOUNTINFO_PATTERN = "utas"  # broad match; narrowed further in the handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _register_session_listeners(page: Page) -> dict[str, Any]:
    """
    Attach request/response listeners that capture UT session headers as
    they fly by. Must be called BEFORE navigation so early accountinfo XHRs
    fired during web-app load are not missed.
    """
    captured: dict[str, Any] = {}

    def _on_request(req: Request) -> None:
        if "utas" in req.url and req.headers.get("x-ut-sid"):
            logger.info("UT request intercepted: %s", req.url)
            captured.setdefault("sid", req.headers.get("x-ut-sid", ""))
            captured.setdefault(
                "phishing_token",
                req.headers.get("x-ut-phishing-token", ""),
            )

    async def _on_response(resp: Response) -> None:
        if "utas" not in resp.url:
            return

        token = resp.headers.get("x-ut-phishing-token", "")
        if token:
            captured.setdefault("phishing_token", token)
            logger.info("Captured phishing_token from response headers: %s", resp.url)

        if "accountinfo" in resp.url:
            try:
                body = await resp.json()
                body_token = body.get("phishingToken", "") or body.get("phishing_token", "")
                if body_token:
                    captured.setdefault("phishing_token", body_token)
                    logger.info("Captured phishing_token from response body")

                personas = body.get("userAccountInfo", {}).get("personas", [{}])
                captured.setdefault(
                    "nucleus_id",
                    str(personas[0].get("nucleusId", "")) if personas else "",
                )
                logger.debug("Captured nucleusId from accountinfo response")
            except Exception as exc:
                logger.debug("Could not parse accountinfo response: %s", exc)

    page.on("request", _on_request)
    page.on("response", _on_response)
    return captured


async def _goto_webapp(page: Page) -> None:
    logger.debug("Navigating to FC Web App…")
    await page.goto(FC_WEBAPP_URL, timeout=30_000)

    # Some regions show a "Log In" button before the EA login page
    try:
        await page.wait_for_selector(
            "a[href*='login'], button:has-text('Login'), .btn-login",
            timeout=15_000,
        )
        await page.click("a[href*='login'], button:has-text('Login'), .btn-login")
    except PWTimeout:
        pass  # already on login form / already authenticated


async def _fill_credentials(page: Page, email: str, password: str) -> None:
    logger.debug("Filling email…")
    await page.wait_for_selector(SEL_EMAIL, timeout=20_000)
    await page.fill(SEL_EMAIL, email)
    await asyncio.sleep(0.5)
    await page.click(SEL_BTN_NEXT)

    logger.debug("Filling password…")
    await page.wait_for_selector(SEL_PASSWORD, timeout=20_000)
    await page.fill(SEL_PASSWORD, password)
    await asyncio.sleep(0.5)
    await page.click(SEL_BTN_SIGNIN)


async def _check_remember_device(page: Page) -> None:
    try:
        await page.check("input[type='checkbox']", timeout=2_000)
        logger.debug("Checked 'Remember this device'")
    except Exception:
        pass


async def _submit_2fa_code(page: Page) -> None:
    await asyncio.sleep(1)
    # #btnSubmit is the verified NEXT button; the rest are fallbacks in case
    # EA renames it again.
    for sel in [SEL_BTN_2FA_SUBMIT, "#logInBtn", "a.otkbtn-primary",
                "button[type='submit']", "a:has-text('NEXT')", ".otkbtn-primary"]:
        try:
            await page.click(sel, timeout=3_000)
            logger.info("2FA submit clicked via selector: %s", sel)
            break
        except Exception:
            continue
    await page.wait_for_load_state("networkidle", timeout=15_000)


def _split_backup_codes(backup_code: str) -> list[str]:
    """
    The stored value may hold several single-use codes (DSFUT console orders
    always come with two — if the first is already spent, the second works).
    Accepts comma / space / semicolon / slash separated input.
    """
    return [c for c in re.split(r"[,\s;/|]+", backup_code) if c]


async def _handle_2fa_backup_code(page: Page, backup_code: str) -> None:
    """
    Verified flow (EA "Verify your identity" / "Enter your code", July 2026):
      1. The preferred verification method is already pre-selected on the
         "Verify your identity" page — click Send Code (#btnSendCode).
      2. On the "Enter your code" page, type a backup code into
         #twoFactorCode, tick "Remember this device", and click NEXT
         (#btnSubmit).
      3. If the input is still there after submitting (code rejected/spent),
         try the next stored code.
    Backup codes are single-use, so this should only ever be called once per
    account (during first_login). Returns silently if no 2FA prompt appears.
    """
    try:
        await page.wait_for_selector(SEL_BTN_SEND_CODE, timeout=8_000)
        logger.info("'Verify your identity' page detected — clicking Send Code…")
        await page.click(SEL_BTN_SEND_CODE)
    except PWTimeout:
        logger.debug("No 'Verify your identity' page — checking for the code input directly")

    try:
        await page.wait_for_selector(SEL_2FA_CODE_INPUT, timeout=15_000)
    except PWTimeout:
        logger.warning("No 2FA code input found — 2FA flow may have changed")
        return

    codes = _split_backup_codes(backup_code)
    for idx, code in enumerate(codes, start=1):
        logger.info("Entering backup code %d/%d…", idx, len(codes))
        await page.fill(SEL_2FA_CODE_INPUT, code)
        await asyncio.sleep(0.3)

        await _check_remember_device(page)
        await _submit_2fa_code(page)

        # Input gone (hidden/detached) → the code was accepted and the flow
        # moved on. Still visible → code rejected; try the next one.
        try:
            await page.wait_for_selector(SEL_2FA_CODE_INPUT, state="hidden", timeout=8_000)
            logger.info("Backup code %d/%d accepted", idx, len(codes))
            return
        except PWTimeout:
            if idx < len(codes):
                logger.warning("Backup code %d/%d seems rejected — trying the next one", idx, len(codes))
                try:
                    await page.fill(SEL_2FA_CODE_INPUT, "")
                except Exception:
                    pass
            else:
                logger.warning("All %d backup code(s) submitted — page did not move on", len(codes))


async def _capture_ut_session(
    page: Page,
    account_id: int,
    captured: dict[str, Any],
    timeout: float = 60.0,
) -> SessionData | None:
    """Wait until UT session headers have been populated in *captured*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if captured.get("sid"):
            break
        await asyncio.sleep(0.4)
    else:
        logger.error(
            "Timed out waiting for UT SID (SID present=%s, phishing_token present=%s)",
            bool(captured.get("sid")),
            bool(captured.get("phishing_token")),
        )
        return None

    captured.setdefault("phishing_token", "0")
    if captured["phishing_token"] == "0":
        logger.warning("phishing_token not captured — defaulting to '0'")

    cookies = {c["name"]: c["value"] for c in await page.context.cookies()}

    return SessionData(
        account_id=account_id,
        sid=captured["sid"],
        phishing_token=captured["phishing_token"],
        access_token=cookies.get("access_token", ""),
        nucleus_id=captured.get("nucleus_id", ""),
        cookies=cookies,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def first_login(
    page: Page,
    *,
    account_id: int,
    email: str,
    password: str,
    backup_code: str = "",
    max_attempts: int = 2,
) -> SessionData | None:
    """
    Run the full credential + backup-code flow on *page* (a page from the
    account's persistent context). Ticks "remember this device" so future
    restores can skip 2FA.

    Backup codes are single-use — max_attempts is kept low since a retry
    would resubmit the same already-consumed code.
    """
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "First login attempt %d/%d for account %d (%s)",
            attempt, max_attempts, account_id, email,
        )
        try:
            captured = _register_session_listeners(page)
            await _goto_webapp(page)
            await _fill_credentials(page, email, password)

            if backup_code:
                await _handle_2fa_backup_code(page, backup_code)

            await page.wait_for_url("**/web-app/**", timeout=40_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)

            session = await _capture_ut_session(page, account_id, captured)
            if session:
                logger.info("First login successful for account %d", account_id)
                return session
        except Exception:
            logger.exception("First login attempt error for account %d", account_id)

        if attempt < max_attempts:
            await asyncio.sleep(5 * attempt)

    logger.error("All first-login attempts failed for account %d", account_id)
    return None


async def restore_session(
    page: Page,
    *,
    account_id: int,
    timeout: float = 30.0,
) -> SessionData | None:
    """
    Navigate the persistent page to the web app and capture a fresh session
    using the profile's existing cookies/local storage — no credentials are
    entered. Returns None if the login form appears (the profile's session
    actually died and the caller should fall back to a password-only re-login).
    """
    captured = _register_session_listeners(page)
    try:
        await _goto_webapp(page)

        # If the profile is still authenticated, the email field never
        # appears — the web app loads straight away.
        try:
            await page.wait_for_selector(SEL_EMAIL, timeout=5_000)
            logger.info("Account %d: login form appeared — persistent session has died", account_id)
            return None
        except PWTimeout:
            pass  # good: no login form, already authenticated

        await page.wait_for_url("**/web-app/**", timeout=timeout * 1000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        return await _capture_ut_session(page, account_id, captured, timeout=timeout)
    except Exception:
        logger.exception("Session restore error for account %d", account_id)
        return None


async def password_relogin(
    page: Page,
    *,
    account_id: int,
    email: str,
    password: str,
) -> SessionData | None:
    """
    Re-authenticate with email + password only (no 2FA), relying on the
    persistent profile's "remembered device" cookie to skip it. If EA still
    prompts for 2FA here, this returns None — the caller must not retry with
    a backup code automatically (single-use) and should alert an admin.
    """
    captured = _register_session_listeners(page)
    try:
        await _goto_webapp(page)
        await _fill_credentials(page, email, password)

        # If 2FA shows up despite the remembered device, bail rather than
        # silently failing deeper in the flow.
        for sel in (SEL_BTN_SEND_CODE, SEL_2FA_CODE_INPUT):
            try:
                await page.wait_for_selector(sel, timeout=3_000)
                logger.warning(
                    "Account %d: 2FA required during password-only re-login — manual intervention needed",
                    account_id,
                )
                return None
            except PWTimeout:
                continue

        await page.wait_for_url("**/web-app/**", timeout=40_000)
        await page.wait_for_load_state("networkidle", timeout=30_000)
        return await _capture_ut_session(page, account_id, captured)
    except Exception:
        logger.exception("Password-only re-login error for account %d", account_id)
        return None
