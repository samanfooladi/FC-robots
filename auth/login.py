"""
Playwright-based login flow for the FC Companion Web App.

Flow
----
1.  Navigate to FC Web App URL  →  redirects to EA login
2.  Fill email → click Next
3.  Fill password → click Sign In
4.  Detect 2-FA prompt  →  inject TOTP code
5.  Wait for Web App shell to load  (networkidle + UT accountinfo XHR)
6.  Intercept the first UT API request to capture X-UT-SID and
    X-UT-PHISHING-TOKEN from outgoing headers + nucleusId from the response
7.  Dump full cookie jar
8.  Persist SessionData to DB and return it

NOTE: EA occasionally changes login-page selectors; update the constants at
the top of this file if the flow breaks.
"""

import asyncio
import logging
import time
from typing import Any

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    Request,
    Response,
    TimeoutError as PWTimeout,
)

from .otp import generate_otp, remaining_seconds
from .session import SessionData, save_session

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
# 2-FA / OTP — always required (fresh incognito context every run)
SEL_OTP_INPUTS = ["input[placeholder*='6 digit']", "#codeEntry", "input[name='codeEntry']"]
SEL_BTN_OTP_SUBMIT = ["#logInBtn"]
# "Verify your identity" intermediate page
SEL_AUTHENTICATOR = (
    "div.origin-ux-radio-button-control:has-text('App Authenticator'), "
    "input[value*='authenticator'], "
    "div[class*='radio']:has-text('Authenticator')"
)
SEL_BTN_SEND_CODE = "#btnSendCode"

# UT API fingerprint — we listen for this URL to grab session headers
UT_ACCOUNTINFO_PATTERN = "utas"  # broad match; narrowed further in the handler

# Minimum OTP window remaining before we wait for the next one
OTP_MIN_REMAINING = 5  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fill_credentials(page: Page, email: str, password: str) -> None:
    logger.debug("Filling email…")
    await page.wait_for_selector("#email", timeout=20_000)
    await page.fill("#email", email)
    await asyncio.sleep(0.5)
    await page.click("#logInBtn")

    logger.debug("Filling password…")
    await page.wait_for_selector("#password", timeout=20_000)
    await page.fill("#password", password)
    await asyncio.sleep(0.5)
    await page.click("#logInBtn")


async def _handle_2fa(page: Page, otp_key: str) -> None:
    """
    Handle the full EA 2-FA flow:
      1. "Verify your identity" page  →  select App Authenticator, click Send Code
      2. OTP entry page               →  fill code, submit
    Both stages are detected by waiting for their key elements; if neither
    appears the function returns silently (already past 2-FA).
    """
    # --- Stage 1: "Verify your identity" page ---
    try:
        await page.wait_for_selector(SEL_BTN_SEND_CODE, timeout=8_000)
        logger.info("'Verify your identity' page detected — selecting App Authenticator…")
        await page.click(SEL_AUTHENTICATOR)
        await asyncio.sleep(0.3)
        await page.click(SEL_BTN_SEND_CODE)
        logger.debug("Clicked Send Code — waiting for OTP input…")
    except PWTimeout:
        logger.debug("No 'Verify your identity' page — checking for OTP input directly")

    # --- Stage 2: OTP entry ---
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

    # Avoid submitting a code that's about to expire
    rem = remaining_seconds(otp_key)
    if rem < OTP_MIN_REMAINING:
        logger.info("OTP expires in %ds — waiting for fresh window…", rem)
        await asyncio.sleep(rem + 1)

    code = generate_otp(otp_key)
    logger.info("Entering OTP code…")
    await page.fill(otp_selector, code)
    await asyncio.sleep(0.3)

    # Tick "Remember this device" if the checkbox is present
    try:
        await page.check("input[type='checkbox']", timeout=2_000)
        logger.debug("Checked 'Remember this device'")
    except Exception:
        pass

    # Wait for the code to register before submitting
    await asyncio.sleep(1)

    for sel in ["#logInBtn", "a.otkbtn-primary", "button[type='submit']",
                "a:has-text('NEXT')", ".otkbtn-primary"]:
        try:
            await page.click(sel, timeout=3_000)
            logger.info("OTP submit clicked via selector: %s", sel)
            break
        except Exception:
            continue

    await page.wait_for_load_state("networkidle", timeout=15_000)


async def _capture_ut_session(
    page: Page,
    account_id: int,
    captured: dict[str, Any],
    timeout: float = 60.0,
) -> SessionData | None:
    """
    Wait until the UT session headers have been populated in *captured*.
    Listeners must be registered on the page BEFORE navigation starts so
    that early accountinfo XHRs are not missed.
    """
    # Wait for SID only — phishing_token has multiple fallback sources
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if captured.get("sid"):
            break
        await asyncio.sleep(0.4)
    else:
        logger.error(
            "Timed out waiting for UT SID "
            "(SID present=%s, phishing_token present=%s)",
            bool(captured.get("sid")),
            bool(captured.get("phishing_token")),
        )
        return None

    # phishing_token starts at "0" on new sessions; don't fail if absent
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


async def login_to_fc(
    account_id: int,
    email: str,
    password: str,
    otp_key: str,
    *,
    headless: bool = False,
    max_attempts: int = 3,
) -> SessionData | None:
    """
    Log into the FC Companion Web App and return a populated SessionData.

    Retries up to *max_attempts* times on transient failures.
    Persists the session to the DB on success.
    Returns None if all attempts fail.
    """
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Login attempt %d/%d for account %d (%s)",
            attempt,
            max_attempts,
            account_id,
            email,
        )
        session = await _single_login_attempt(
            account_id, email, password, otp_key, headless=headless
        )
        if session:
            await save_session(session)
            logger.info("Login successful for account %d", account_id)
            return session

        if attempt < max_attempts:
            backoff = 5 * attempt
            logger.warning("Attempt %d failed — retrying in %ds…", attempt, backoff)
            await asyncio.sleep(backoff)

    logger.error("All login attempts failed for account %d", account_id)
    return None


async def _single_login_attempt(
    account_id: int,
    email: str,
    password: str,
    otp_key: str,
    *,
    headless: bool,
) -> SessionData | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context: BrowserContext = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            # Register UT session listeners before any navigation so that
            # early accountinfo XHRs fired during Web App load are not missed.
            captured: dict[str, Any] = {}

            def _on_request(req: Request) -> None:
                if "utas" in req.url and req.headers.get("x-ut-sid"):
                    logger.info("UT request intercepted: %s", req.url)
                    logger.info("Headers found: %s", list(req.headers.keys()))
                    captured.setdefault("sid", req.headers.get("x-ut-sid", ""))
                    captured.setdefault(
                        "phishing_token",
                        req.headers.get("x-ut-phishing-token", ""),
                    )

            async def _on_response(resp: Response) -> None:
                if "utas" not in resp.url:
                    return

                # phishing_token may arrive in response headers
                token = resp.headers.get("x-ut-phishing-token", "")
                if token:
                    captured.setdefault("phishing_token", token)
                    logger.info("Captured phishing_token from response headers: %s", resp.url)

                if "accountinfo" in resp.url:
                    try:
                        body = await resp.json()
                        # phishing_token may also be in the response body
                        body_token = (
                            body.get("phishingToken", "")
                            or body.get("phishing_token", "")
                        )
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

            # Step 1: open FC Web App (will redirect to EA login)
            logger.debug("Navigating to FC Web App…")
            await page.goto(FC_WEBAPP_URL, timeout=30_000)

            # Some regions show a "Log In" button before the EA login page
            try:
                await page.wait_for_selector(
                    "a[href*='login'], button:has-text('Login'), .btn-login",
                    timeout=15_000,
                )
                await page.click("a[href*='login'], button:has-text('Login'), .btn-login")
                # Wait for EA login page to load after clicking
                await page.wait_for_selector(SEL_EMAIL, timeout=20_000)
            except PWTimeout:
                pass  # already on login form

            # Step 2 & 3: credentials
            await _fill_credentials(page, email, password)

            # Step 4: optional 2-FA
            await _handle_2fa(page, otp_key)

            # Step 5: wait for Web App shell
            logger.debug("Waiting for Web App to finish loading…")
            await page.wait_for_url("**/web-app/**", timeout=40_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)

            # Step 6-7: grab session from network traffic
            session = await _capture_ut_session(page, account_id, captured)
            return session

        except Exception as exc:
            logger.exception("Login attempt error for account %d: %s", account_id, exc)
            return None

        finally:
            await browser.close()
