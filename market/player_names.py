"""
Player-name lookup for Transfer Market items.

The EA market API returns no player names — the official web app resolves
them client-side from a static players.json published with the web-app
assets.  This module downloads that file once per process, builds an
id → name map, and resolves names from each listing's resourceId.

Special card versions (TOTW, heroes, …) encode their version in the high
bits of the resourceId in steps of 0x1000000; the low bits are the base
player id used in players.json.

If the download fails (EA moved the file, network error) every lookup
returns None and callers fall back to the configured card name — the bot
keeps working, only the displayed name is less specific.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# The content GUID has been stable across game years; the year segment and
# site section have not, so several candidates are tried in order.
_PLAYERS_URLS = [
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


async def _download() -> dict[int, str] | None:
    for url in _PLAYERS_URLS:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=_HEADERS)
            if resp.status_code != 200:
                logger.warning("players.json: HTTP %d from %s", resp.status_code, url)
                continue
            body = resp.json()
            names: dict[int, str] = {}
            for section in ("Players", "LegendsPlayers"):
                for player in body.get(section, []):
                    pid = player.get("id")
                    if pid is not None:
                        names[int(pid)] = _pick_name(player)
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
                result = await _download()
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
