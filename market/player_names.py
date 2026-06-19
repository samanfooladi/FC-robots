"""
Player-name lookup for Transfer Market items.

The EA market API returns no player names — the official web app resolves
them client-side from a static players.json published with the web-app
assets.  This module downloads that file once per process, builds an
id → name map, and resolves names from each listing's resourceId.

Special card versions (TOTW, heroes, …) encode their version in the high
bits of the resourceId in steps of 0x1000000; the low bits are the base
player id used in players.json.

Loading order: a locally-saved file (PLAYERS_JSON_FILE or market/players.json)
is tried first because EA's content CDN is behind Akamai bot protection and a
server-side download usually returns an HTML challenge page, not the JSON.  Save
the file once from a browser (DevTools → Network → players.json) and the bot
reads it directly — no bot check involved.

If neither the local file nor the download yields data, every lookup returns
None and callers fall back to the configured card name — the bot keeps working,
only the displayed name is less specific.
"""

import asyncio
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ── Local file (most reliable) ─────────────────────────────────────────────
# EA's content CDN is behind Akamai bot protection: a server-side request
# usually gets an HTML challenge page instead of the JSON, so the download
# below frequently fails and names fall back to the configured card name.
#
# The fix that always works: open the web app in a browser, find the
# players.json request in DevTools → Network, "Open in new tab", save it next
# to this file as market/players.json (or point PLAYERS_JSON_FILE at it).  The
# browser passes the bot check, so the saved file is the real data.
_PLAYERS_FILE = (
    os.getenv("PLAYERS_JSON_FILE", "").strip()
    or os.path.join(os.path.dirname(__file__), "players.json")
)

# The exact URL changes every game year.  Set PLAYERS_JSON_URL in the
# environment to the file the web app actually loads (find it in the
# browser's DevTools → Network tab) — that always takes priority over the
# hard-coded best-effort fallbacks below.  Note: these only work if the host
# running the bot is not bot-blocked by EA; otherwise use the local file above.
_PLAYERS_URLS = [
    u for u in [os.getenv("PLAYERS_JSON_URL", "").strip()] if u
] + [
    "https://www.ea.com/fifa/ultimate-team/web-app/content/"
    "21D4F1AC-91A3-458D-A64E-895AA6D871D1/2026/fut/items/web/players.json",
    "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/content/"
    "21D4F1AC-91A3-458D-A64E-895AA6D871D1/2026/fut/items/web/players.json",
]

# EA's CDN rejects requests without a browser-like User-Agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
}

_names: dict[int, str] | None = None
_load_failed = False  # only attempt the download once per process
_lock = asyncio.Lock()


def _pick_name(player: dict) -> str:
    """commonName ("Dudek") when present, otherwise last name, else full."""
    return (
        player.get("c")
        or player.get("l")
        or f"{player.get('f', '')} {player.get('l', '')}".strip()
    )


def _build_name_map(body: dict, source: str) -> dict[int, str]:
    """Turn a parsed players.json body into an id → name map."""
    players: list[dict] = []
    for section in ("Players", "LegendsPlayers"):
        players.extend(body.get(section, []))
    if players:
        # Diagnostic: show the shape of one entry so the name fields
        # (and whether they are strings or index ids) are visible.
        logger.info(
            "players.json: sample entry from %s: %s",
            source, json.dumps(players[0], ensure_ascii=False)[:500],
        )
    names: dict[int, str] = {}
    for player in players:
        pid = player.get("id")
        if pid is not None:
            names[int(pid)] = _pick_name(player)
    return names


def _load_local() -> dict[int, str] | None:
    """Load names from a locally-saved players.json, if one exists."""
    if not os.path.isfile(_PLAYERS_FILE):
        return None
    try:
        with open(_PLAYERS_FILE, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        names = _build_name_map(body, _PLAYERS_FILE)
        if names:
            logger.info("players.json: loaded %d player names from local file %s",
                        len(names), _PLAYERS_FILE)
            return names
        logger.warning("players.json: local file %s has no players", _PLAYERS_FILE)
    except Exception as exc:
        logger.warning("players.json: failed to read local file %s: %s",
                       _PLAYERS_FILE, exc)
    return None


async def _download() -> dict[int, str] | None:
    for url in _PLAYERS_URLS:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=_HEADERS)
            if resp.status_code != 200:
                logger.warning("players.json: HTTP %d from %s", resp.status_code, url)
                continue
            try:
                body = resp.json()
            except Exception:
                # EA's Akamai bot protection serves an HTML challenge page with
                # HTTP 200 instead of the JSON — detect it so the failure is
                # obvious rather than a cryptic JSON parse error.
                logger.warning(
                    "players.json: %s returned non-JSON (%s, %d bytes) — likely "
                    "an EA bot-challenge page; save the file locally instead "
                    "(see PLAYERS_JSON_FILE)",
                    url, resp.headers.get("content-type", "?"), len(resp.content),
                )
                continue
            names = _build_name_map(body, url)
            if names:
                logger.info("players.json: loaded %d player names from %s", len(names), url)
                return names
            logger.warning("players.json: empty/unexpected payload from %s", url)
        except Exception as exc:
            logger.warning("players.json: failed to load from %s: %s", url, exc)
    return None


async def get_player_name(resource_id: int) -> str | None:
    """Resolve the player name for *resource_id*, or None if unknown."""
    global _names, _load_failed

    if not resource_id:
        return None

    if _names is None and not _load_failed:
        async with _lock:
            if _names is None and not _load_failed:
                # Prefer a locally-saved file (immune to EA's bot blocking),
                # fall back to downloading from the content CDN.
                result = _load_local() or await _download()
                if result is None:
                    _load_failed = True
                    logger.error(
                        "players.json could not be loaded — card names will "
                        "fall back to the configured card name until restart"
                    )
                else:
                    _names = result

    if _names is None:
        return None

    base_id = resource_id % 0x1000000
    return _names.get(base_id)
