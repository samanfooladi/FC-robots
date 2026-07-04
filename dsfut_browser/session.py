"""
Persistent Chromium session for dsfut.net.

The login page has a manually-solved captcha we deliberately do NOT automate.
Instead we keep a persistent context on disk so a human solves the captcha
once and the session survives restarts. Login state is inferred from the
homepage: a logged-out visit redirects to the login page / shows a password
field; a logged-in visit shows the order board.
"""

import asyncio
import logging

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    Error as PWError,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Substrings that mark a login/register/auth URL (used to detect logged-out).
_LOGIN_URL_HINTS = ("/login", "/signin", "/sign-in", "/auth", "/register")


class DsfutBrowserSession:
    def __init__(self, profile_dir, headless: bool, home_url: str) -> None:
        self.profile_dir = str(profile_dir)
        self.headless = headless
        self.home_url = home_url
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        logger.info(
            "DSFUT browser context ready (profile=%s headless=%s)",
            self.profile_dir, self.headless,
        )

    async def close(self) -> None:
        # Close the context cleanly so the on-disk profile is not corrupted.
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                logger.debug("DSFUT: error closing browser context", exc_info=True)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                logger.debug("DSFUT: error stopping playwright", exc_info=True)
        self.context = None
        self.page = None
        self._pw = None

    # ------------------------------------------------------------------
    # Navigation & auth state
    # ------------------------------------------------------------------

    async def goto_home(self) -> None:
        assert self.page is not None
        await self.page.goto(self.home_url, wait_until="domcontentloaded", timeout=30_000)

    def url_is_login(self, url: str | None) -> bool:
        low = (url or "").lower()
        return any(hint in low for hint in _LOGIN_URL_HINTS)

    async def is_logged_out(self) -> bool:
        """
        Heuristic: we are logged out if the current URL looks like a login page,
        or the page is showing a password field. Called only when already on
        (or having just navigated to) the homepage.
        """
        assert self.page is not None
        if self.url_is_login(self.page.url):
            return True
        try:
            return await self.page.locator("input[type='password']").count() > 0
        except PWError:
            return False

    async def ensure_logged_in(self, *, interactive: bool = True) -> bool:
        """
        Navigate to the homepage and confirm we are authenticated. If not and
        *interactive*, ask the human to solve the captcha in the visible window
        and press Enter, then re-check once. Returns True only when logged in.
        """
        await self.goto_home()
        if not await self.is_logged_out():
            logger.info("DSFUT: existing session is authenticated")
            return True

        logger.warning("DSFUT: not logged in (login page detected)")
        if not interactive:
            return False

        await self._wait_for_manual_login()

        await self.goto_home()
        if await self.is_logged_out():
            logger.error("DSFUT: still not logged in after manual step")
            return False
        logger.info("DSFUT: manual login successful — session stored in profile")
        return True

    async def _wait_for_manual_login(self) -> None:
        """
        Block on terminal input (off the event loop) until the human confirms
        they have logged in. Uses to_thread so the rest of the bot keeps running.
        """
        banner = (
            "\n"
            "==================================================================\n"
            "  DSFUT LOGIN REQUIRED\n"
            "  A Chromium window is open. Log in to dsfut.net and solve the\n"
            "  captcha in that window. Your session is saved to the profile,\n"
            "  so this is normally only needed once.\n"
            "==================================================================\n"
        )
        print(banner, flush=True)
        try:
            await asyncio.to_thread(input, "Press Enter here once you are logged in… ")
        except (EOFError, RuntimeError):
            # No interactive stdin (e.g. detached service) — fall back to polling
            # the auth state so we still don't hammer the login page.
            logger.warning("DSFUT: no interactive console — waiting for login by polling")
            for _ in range(120):  # up to ~10 min
                await asyncio.sleep(5)
                await self.goto_home()
                if not await self.is_logged_out():
                    return
