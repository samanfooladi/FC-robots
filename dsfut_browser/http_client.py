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
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Laravel: 419 = session/CSRF expired; 401/403 = unauthenticated.
_EXPIRED_STATUSES = {401, 403, 419}


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
        try:
            data = resp.json()
        except Exception:
            # JSON was expected; an HTML body almost certainly means logged out.
            if looks_like_login_response(resp):
                raise SessionExpired("comfortables returned an HTML/login body")
            return []
        return data if isinstance(data, list) else []

    async def pickup(self, order_id, order_hash: str) -> bool:
        """
        GET /comfortable/pickup/{id}/{hash}. A 302 to /comfortable/active means
        the claim was accepted server-side; success still has to be verified on
        the active page. Returns True only when the redirect actually points at
        /comfortable/active — any other redirect target (e.g. back to
        /comfortable) is NOT a claim and returns False.
        """
        assert self._client is not None
        url = f"{self.origin}/comfortable/pickup/{order_id}/{order_hash}"
        resp = await self._client.get(url)
        location = resp.headers.get("location", "")
        # Safe to log plainly — status + Location carry no credentials, and this
        # makes every miss immediately diagnosable.
        logger.info(
            "DSFUT: pickup(%s) -> HTTP %s, Location: %s",
            order_id, resp.status_code, location,
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
