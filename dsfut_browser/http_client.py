"""
Fast HTTP client for the DSFUT comfort-trade loop.

Reuses the cookies captured from the authenticated Playwright context so the
polling/pickup/detail requests are plain, quick httpx GETs — no browser in the
hot path. When a request is bounced to the login page (or Laravel answers 419 /
401 / 403), SessionExpired is raised so the caller can fall back to the
Playwright login flow and rebuild this client with fresh cookies.

Redirects are NOT auto-followed: the pickup step is confirmed by its 302 to
/comfortable/active, and an unexpected redirect to /login is how we detect an
expired session.
"""

import logging
import time
import urllib.parse
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Laravel: 419 = session/CSRF expired; 401/403 = unauthenticated.
_EXPIRED_STATUSES = {401, 403, 419}


def _ts_ms() -> str:
    """Wall-clock timestamp with millisecond precision, for race-timing logs."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class SessionExpired(Exception):
    """The reused cookies are no longer valid — a Playwright re-login is needed."""


class DsfutHttpClient:
    def __init__(self, board_url: str, user_agent: str, timeout: float) -> None:
        sp = urllib.parse.urlsplit(board_url)
        self.origin = f"{sp.scheme}://{sp.netloc}"
        self.board_url = board_url
        self.user_agent = user_agent
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def rebuild(self, cookie_list: list[dict]) -> int:
        """(Re)create the client from Playwright cookies. Returns the cookie count."""
        await self.aclose()

        cookies = {c["name"]: c.get("value", "") for c in cookie_list if c.get("name")}
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.board_url,
        }
        # Laravel expects the URL-decoded XSRF-TOKEN cookie echoed as a header.
        xsrf = cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)

        self._client = httpx.AsyncClient(
            cookies=cookies,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=False,
        )
        return len(cookies)

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _guard_expired(self, resp: httpx.Response) -> None:
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location", "")
            if "login" in location.lower():
                raise SessionExpired(f"redirected to login ({location})")
        if resp.status_code in _EXPIRED_STATUSES:
            raise SessionExpired(f"HTTP {resp.status_code}")

    async def poll_comfortables(self) -> list:
        """GET /api/json/comfortables — the board as a JSON array (or [] )."""
        assert self._client is not None
        resp = await self._client.get(f"{self.origin}/api/json/comfortables")
        self._guard_expired(resp)

        # --- TEMP DIAGNOSTIC (poll-rate regression, DSFUT_POLL_INTERVAL_S) ----
        # Surface EXACTLY what the server returns at high poll rates. The board
        # JSON carries no credentials, so the raw body is safe to log. Enable
        # with LOG_LEVEL=DEBUG. Remove once the silent-[] regression is
        # understood. Retry-After is logged because Laravel throttling sets it.
        logger.debug(
            "DSFUT poll: HTTP %s ct=%s len=%d retry-after=%s body[:500]=%r",
            resp.status_code,
            resp.headers.get("content-type", ""),
            len(resp.text or ""),
            resp.headers.get("retry-after", ""),
            (resp.text or "")[:500],
        )
        # ---------------------------------------------------------------------

        try:
            data = resp.json()
        except Exception:
            # JSON was expected; an HTML body almost certainly means logged out.
            if looks_like_login_response(resp):
                raise SessionExpired("comfortables returned an HTML/login body")
            # TEMP: this was a silent `return []` — the suspected blind spot.
            # A non-JSON, non-login body (rate-limit / challenge / error page)
            # looked identical to "no orders" with no trace in the logs.
            logger.warning(
                "DSFUT poll: non-JSON, non-login body (HTTP %s ct=%s) — treating "
                "as no orders; body[:300]=%r",
                resp.status_code, resp.headers.get("content-type", ""),
                (resp.text or "")[:300],
            )
            return []
        if not isinstance(data, list):
            # TEMP: also previously silent. A 429 throttle body such as
            # {"message":"Too Many Requests"} is valid JSON but a dict, so it
            # slipped past _guard_expired (429 ∉ {401,403,419}) AND past the
            # list check — looking exactly like an empty board.
            logger.warning(
                "DSFUT poll: JSON body is %s, not a list (HTTP %s) — treating as "
                "no orders; body=%r",
                type(data).__name__, resp.status_code, str(data)[:300],
            )
            return []
        return data

    async def pickup(self, order_id, order_hash: str, detected_at: float | None = None) -> bool:
        """
        GET /comfortable/pickup/{id}/{hash}. A 302 to /comfortable/active means
        the claim was accepted server-side; success still has to be verified on
        the active page. Returns True only when the redirect actually points at
        /comfortable/active — any other redirect target (e.g. back to
        /comfortable) is NOT a claim and returns False.

        *detected_at*, if given, is a time.monotonic() timestamp from the moment
        the poller first saw this order eligible — used only to log how much
        time elapsed before the pickup response came back (race diagnostics).
        """
        assert self._client is not None
        url = f"{self.origin}/comfortable/pickup/{order_id}/{order_hash}"
        request_started = time.monotonic()
        resp = await self._client.get(url)
        response_at = time.monotonic()
        location = resp.headers.get("location", "")

        request_ms = (response_at - request_started) * 1000
        elapsed_note = f", request round-trip: {request_ms:.1f}ms"
        if detected_at is not None:
            since_detect_ms = (response_at - detected_at) * 1000
            elapsed_note += f", elapsed since order detected: {since_detect_ms:.1f}ms"

        # Safe to log plainly — status + Location carry no credentials, and this
        # makes every miss (and the race timing) immediately diagnosable.
        logger.info(
            "DSFUT: pickup(%s) -> HTTP %s, Location: %s (response received at %s%s)",
            order_id, resp.status_code, location, _ts_ms(), elapsed_note,
        )
        if resp.status_code in _REDIRECT_STATUSES:
            if "login" in location.lower():
                raise SessionExpired(f"pickup redirected to login ({location})")
            return "comfortable/active" in location.lower()
        self._guard_expired(resp)
        return False

    async def get_active(self) -> str:
        """GET /comfortable/active — HTML we parse for the <fc-comfortable> block."""
        assert self._client is not None
        resp = await self._client.get(f"{self.origin}/comfortable/active")
        self._guard_expired(resp)
        return resp.text


def looks_like_login_response(resp: httpx.Response) -> bool:
    try:
        from .parser import looks_like_login
        return looks_like_login(resp.text)
    except Exception:
        return False
