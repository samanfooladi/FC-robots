"""
BrowserPool — owns one persistent Chromium context per EA account.

OrderWorkers never launch browsers or authenticate directly; they call
get_session()/force_relogin() on the pool and trade over httpx exactly as
before. The pool is responsible for:
  - launching/keeping alive one persistent Chrome profile per account
  - first-time credential login (TOTP or backup code)
  - silent session restoration on subsequent runs (no re-entering credentials)
  - health-checking pages and relaunching crashed browsers from the same
    on-disk profile
"""

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

from auth.login import first_login, password_relogin, restore_session
from auth.session import SessionData, save_session
from bot.notifications import safe_send
from config import ADMIN_IDS, BROWSER_HEADLESS, BROWSER_HEALTH_CHECK_INTERVAL_S
from db.database import (
    get_all_accounts,
    mark_first_login_done,
    set_account_status,
    set_profile_path,
)

from .profiles import profile_dir

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class _PoolEntry:
    account_id: int
    email: str
    password: str
    otp_key: str
    backup_code: str
    context: BrowserContext
    page: Page
    session: SessionData | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserPool:
    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self._pw: Playwright | None = None
        self._entries: dict[int, _PoolEntry] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        for account in await get_all_accounts():
            await self._provision(account)
        logger.info("BrowserPool ready: %d account(s) authenticated", len(self._entries))

    async def _launch_context(self, account_id: int) -> BrowserContext:
        assert self._pw is not None
        return await self._pw.chromium.launch_persistent_context(
            str(profile_dir(account_id)),
            headless=BROWSER_HEADLESS,
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )

    async def _provision(self, account: dict) -> None:
        account_id = account["id"]
        email = account["email"]
        password = account["password"] or ""
        otp_key = account["otp_key"] or ""
        backup_code = account["backup_code"] or ""

        if not password:
            logger.warning(
                "Account %d (%s) has no password stored — cannot authenticate, skipping",
                account_id, email,
            )
            return

        context = await self._launch_context(account_id)
        page = context.pages[0] if context.pages else await context.new_page()
        entry = _PoolEntry(
            account_id=account_id, email=email, password=password,
            otp_key=otp_key, backup_code=backup_code,
            context=context, page=page,
        )
        self._entries[account_id] = entry
        await set_profile_path(account_id, str(profile_dir(account_id)))

        async with entry.lock:
            session = await self._authenticate_entry(entry, first_login_done=bool(account["first_login_done"]))
            if session is None:
                await set_account_status(account_id, "login_failed")
                return
            entry.session = session
            await save_session(session)
            logger.info("Account %d (%s): browser session ready", account_id, email)

    async def _authenticate_entry(self, entry: "_PoolEntry", *, first_login_done: bool) -> SessionData | None:
        """Caller must hold entry.lock."""
        if not first_login_done:
            session = await first_login(
                entry.page,
                account_id=entry.account_id,
                email=entry.email,
                password=entry.password,
                otp_key=entry.otp_key,
                backup_code=entry.backup_code,
            )
            if session:
                await mark_first_login_done(entry.account_id)
            else:
                logger.error("Account %d (%s): first login failed", entry.account_id, entry.email)
                await self._alert_admins(
                    f"❌ Account {entry.account_id} ({entry.email}): first login failed. "
                    "Manual intervention required."
                )
            return session

        session = await restore_session(entry.page, account_id=entry.account_id)
        if session is not None:
            return session

        logger.info(
            "Account %d: persistent session died — attempting password-only re-login…",
            entry.account_id,
        )
        session = await password_relogin(
            entry.page, account_id=entry.account_id, email=entry.email, password=entry.password,
        )
        if session is None:
            logger.error("Account %d (%s): password-only re-login failed", entry.account_id, entry.email)
            await self._alert_admins(
                f"❌ Account {entry.account_id} ({entry.email}): could not restore session "
                "and password-only re-login failed. EA may be asking for 2FA again — "
                "manual intervention required."
            )
        return session

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    async def get_session(self, account_id: int) -> SessionData | None:
        """Return a fresh, valid session — reusing the cached one if possible."""
        entry = self._entries.get(account_id)
        if entry is None:
            return None
        if entry.session and entry.session.is_valid() and not entry.session.expired:
            return entry.session

        async with entry.lock:
            if entry.session and entry.session.is_valid() and not entry.session.expired:
                return entry.session
            session = await restore_session(entry.page, account_id=account_id)
            if session is None:
                return await self._force_relogin_locked(entry)
            entry.session = session
            await save_session(session)
            return session

    async def force_relogin(self, account_id: int) -> SessionData | None:
        """Hard re-auth after a confirmed 401 from EA — password-only, never auto-spends a backup code."""
        entry = self._entries.get(account_id)
        if entry is None:
            return None
        async with entry.lock:
            return await self._force_relogin_locked(entry)

    async def _force_relogin_locked(self, entry: "_PoolEntry") -> SessionData | None:
        """Caller must hold entry.lock."""
        session = await password_relogin(
            entry.page, account_id=entry.account_id, email=entry.email, password=entry.password,
        )
        if session is None:
            # Callers (OrderWorker._refresh_session) already alert admins with
            # order-level context on failure — just track account status here.
            logger.error("Account %d: re-login failed — manual intervention required", entry.account_id)
            await set_account_status(entry.account_id, "login_failed")
            return None

        entry.session = session
        await save_session(session)
        logger.info("Account %d: re-login successful", entry.account_id)
        return session

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    async def health_check_loop(self) -> None:
        while True:
            await asyncio.sleep(BROWSER_HEALTH_CHECK_INTERVAL_S)
            for account_id, entry in list(self._entries.items()):
                try:
                    await entry.page.title()
                except Exception:
                    logger.warning("Account %d: browser page unresponsive — relaunching…", account_id)
                    await self._relaunch(entry)

    async def _relaunch(self, entry: "_PoolEntry") -> None:
        async with entry.lock:
            try:
                await entry.context.close()
            except Exception:
                pass
            try:
                entry.context = await self._launch_context(entry.account_id)
                entry.page = entry.context.pages[0] if entry.context.pages else await entry.context.new_page()
                session = await self._authenticate_entry(entry, first_login_done=True)
                if session:
                    entry.session = session
                    await save_session(session)
                    logger.info("Account %d: browser relaunched and session restored", entry.account_id)
                else:
                    await set_account_status(entry.account_id, "login_failed")
            except Exception:
                logger.exception("Account %d: relaunch failed", entry.account_id)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        for entry in self._entries.values():
            try:
                await entry.context.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()
        logger.info("BrowserPool stopped")

    async def _alert_admins(self, text: str) -> None:
        if not self.bot:
            return
        for admin_id in ADMIN_IDS:
            await safe_send(self.bot, admin_id, text)
