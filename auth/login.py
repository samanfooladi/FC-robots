"""
Playwright login flow for the FC Companion Web App.

Two entry points, both operating on a page that belongs to a *persistent*
browser context owned by browser_pool.BrowserPool — this module never
launches or closes a browser itself.

first_login()
    Full credential flow: email -> password -> 2FA (TOTP or backup code) ->
    capture session. Used once per account, the first time its persistent
    profile is created. Ticks "remember this device" so subsequent restores
    skip 2FA.

restore_session()
    Navigates the already-authenticated persistent page to the web app and
    captures a fresh session without re-entering any credentials. Returns
    None if the profile's session has actually died (login form appears),
    in which case the caller should fall back to a password-only re-login.

NOTE: EA occasionally changes login-page selectors; update the constants at
the top of this file if the flow breaks. The backup-code selectors in
particular are best-effort placeholders that have not been verified against
a live "use a backup code instead" screen yet.
"""

import asyncio
import logging
import time
from typing import Any

from playwright.async_api import (
    Page,
    Request,
    Response,
    TimeoutError as PWTimeout,
)

from .otp import generate_otp, remaining_seconds
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
SEL_OTP_INPUTS = ["input[placeholder*='6 digit']", "#codeEntry", "input[name='codeEntry']"]
SEL_BTN_OTP_SUBMIT = ["#logInBtn"]
# "Verify your identity" intermediate page
SEL_AUTHENTICATOR = (
    "div.origin-ux-radio-button-control:has-text('App Authenticator'), "
    "input[value*='authenticator'], "
    "div[class*='radio']:has-text('Authenticator')"
)
SEL_BTN_SEND_CODE = "#btnSendCode"

# Backup-code 2FA — UNVERIFIED, best-effort selectors
SEL_USE_ANOTHER_WAY = (
    "a:has-text('another way'), a:has-text('Use a different way'), "
    "button:has-text('another way'), a:has-text('backup code'), "
    "div[class*='radio']:has-text('backup code')"
)
SEL_BACKUP_CODE_INPUTS = [
    "input[name='backupCode']",
    "#backupCode",
    "input[placeholder*='backup' i]",
    "input[placeholder*='code' i]",
]

# UT API fingerprint — we listen for this URL to grab session headers
UT_ACCOUNTINFO_PATTERN = "utas"  # broad match; narrowed further in the handler

# Minimum OTP window remaining before we wait for the next one
OTP_MIN_REMAINING = 5  # seconds


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
    for sel in ["#logInBtn", "a.otkbtn-primary", "button[type='submit']",
                "a:has-text('NEXT')", ".otkbtn-primary"]:
        try:
            await page.click(sel, timeout=3_000)
            logger.info("2FA submit clicked via selector: %s", sel)
            break
        except Exception:
            continue
    await page.wait_for_load_state("networkidle", timeout=15_000)


async def _handle_2fa_totp(page: Page, otp_key: str) -> None:
    """
    1. "Verify your identity" page  ->  select App Authenticator, send code
    2. OTP entry page               ->  fill code, submit
    Returns silently if neither stage appears (already past 2-FA).
    """
    try:
        await page.wait_for_selector(SEL_BTN_SEND_CODE, timeout=8_000)
        logger.info("'Verify your identity' page detected — selecting App Authenticator…")
        await page.click(SEL_AUTHENTICATOR)
        await asyncio.sleep(0.3)
        await page.click(SEL_BTN_SEND_CODE)
        logger.debug("Clicked Send Code — waiting for OTP input…")
    except PWTimeout:
        logger.debug("No 'Verify your identity' page — checking for OTP input directly")

    otp_selector: str | None = None
    for sel in SEL_OTP_INPUTS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            otp_selector = sel
            break
        except PWTimeout:
            continue

    if otp_selector is None:
        logger.debug("No OTP prompt found — continuing")
        return

    rem = remaining_seconds(otp_key)
    if rem < OTP_MIN_REMAINING:
        logger.info("OTP expires in %ds — waiting for fresh window…", rem)
        await asyncio.sleep(rem + 1)

    code = generate_otp(otp_key)
    logger.info("Entering OTP code…")
    await page.fill(otp_selector, code)
    await asyncio.sleep(0.3)

    await _check_remember_device(page)
    await _submit_2fa_code(page)


async def _handle_2fa_backup_code(page: Page, backup_code: str) -> None:
    """
    UNVERIFIED best-effort flow:
      1. "Verify your identity" page -> switch to the backup-code option
      2. Backup-code entry page      -> fill code, submit
    Backup codes are single-use, so this should only ever be called once per
    account (during first_login). Returns silently if no 2FA prompt appears.
    """
    try:
        await page.wait_for_selector(SEL_BTN_SEND_CODE, timeout=8_000)
        logger.info("'Verify your identity' page detected — switching to backup code…")
        try:
            await page.click(SEL_USE_ANOTHER_WAY, timeout=5_000)
            await asyncio.sleep(0.3)
        except Exception:
            logger.debug("No explicit 'use another way' link found — trying direct backup-code input")
    except PWTimeout:
        logger.debug("No 'Verify your identity' page — checking for backup-code input directly")

    code_selector: str | None = None
    for sel in SEL_BACKUP_CODE_INPUTS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            code_selector = sel
            break
        except PWTimeout:
            continue

    if code_selector is None:
        logger.warning("No backup-code input found — 2FA flow may have changed")
        return

    logger.info("Entering backup code…")
    await page.fill(code_selector, backup_code)
    await asyncio.sleep(0.3)

    await _check_remember_device(page)
    await _submit_2fa_code(page)


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
    otp_key: str = "",
    backup_code: str = "",
    max_attempts: int = 2,
) -> SessionData | None:
    """
    Run the full credential + 2FA flow on *page* (a page from the account's
    persistent context). Ticks "remember this device" so future restores can
    skip 2FA. Prefers TOTP when an otp_key is configured; falls back to the
    (unverified) backup-code flow otherwise.

    Backup codes are single-use — max_attempts is kept low for that path
    since a retry would resubmit the same already-consumed code.
    """
    use_backup_code = not otp_key and bool(backup_code)
    if use_backup_code:
        max_attempts = min(max_attempts, 2)

    for attempt in range(1, max_attempts + 1):
        logger.info(
            "First login attempt %d/%d for account %d (%s)",
            attempt, max_attempts, account_id, email,
        )
        try:
            captured = _register_session_listeners(page)
            await _goto_webapp(page)
            await _fill_credentials(page, email, password)

            if otp_key:
                await _handle_2fa_totp(page, otp_key)
            elif backup_code:
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
        for sel in (SEL_BTN_SEND_CODE, *SEL_OTP_INPUTS, *SEL_BACKUP_CODE_INPUTS):
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
