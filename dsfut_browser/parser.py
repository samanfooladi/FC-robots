"""
Pure parsing helpers for the DSFUT fast HTTP loop — no I/O, no state.

Two inputs are parsed here:
  * the JSON array from GET /api/json/comfortables  (board polling)
  * the HTML from GET /comfortable/active           (pickup verification +
    account-detail extraction of the <fc-comfortable> custom element)

SECURITY: nothing here logs. Callers must keep the returned credentials out of
logs/exceptions — only the DB and the admin Telegram message may carry them.
"""

import html as _html
import re

# ---------------------------------------------------------------------------
# Board JSON (polling)
# ---------------------------------------------------------------------------


def is_pc(item: dict) -> bool:
    """True for PC orders (to be skipped). PS/Xbox use console 'ps' / 'xbox'."""
    console = str(item.get("console") or "").strip().lower()
    full = str(item.get("console_full_name") or "").lower()
    return console == "pc" or "pc" in full


def eligible_orders(data) -> list[dict]:
    """
    Keep only pickable PlayStation/Xbox orders from a comfortables response.
    Each kept item must carry the id + hash needed for pickup.
    """
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("id") in (None, "") or not item.get("hash"):
            continue
        if is_pc(item):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Active-order HTML (pickup verification + credential extraction)
# ---------------------------------------------------------------------------

_FC_TAG_RE = re.compile(r"<fc-comfortable\b([^>]*)>", re.IGNORECASE | re.DOTALL)
# Attribute names may include ':' (Vue bindings, e.g. :backup-codes) and '-'.
_ATTR_RE = re.compile(r'([:\w-]+)\s*=\s*"([^"]*)"')
_TAKEN_MARKER = "another supplier has already taken"


def order_already_taken(html_text: str) -> bool:
    """
    The pickup 302 fires even when we lost the race; the active page then shows
    a danger alert instead of our order. Detect that so we treat it as a miss.
    """
    return _TAKEN_MARKER in (html_text or "").lower()


# The pickup 302 is the same on success and failure — the failure reason only
# travels as a flash message (<div class="uk-alert uk-alert-danger">…</div>) on
# the page it redirects to. Flat text only; a nested <div> inside the alert
# would truncate the match, which hasn't been observed.
_DANGER_ALERT_RE = re.compile(
    r'<div[^>]*class="[^"]*uk-alert-danger[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


def extract_danger_alerts(html_text: str) -> list[str]:
    """Plain-text contents of every danger alert on the page."""
    out = []
    for m in _DANGER_ALERT_RE.finditer(html_text or ""):
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
        if text:
            out.append(text)
    return out


def order_over_limit(alerts: list[str]) -> bool:
    """
    True when a pickup was rejected because the order would push the account
    past its DSFUT cap ("The amount exceeds the maximum allowed."). The cap is
    cumulative: coins of orders still active count against it.
    """
    return any("amount exceeds the maximum" in a.lower() for a in alerts)


def sum_active_coins(html_text: str) -> int:
    """Total coins across every <fc-comfortable> block on the active page."""
    total = 0
    for m in _FC_TAG_RE.finditer(html_text or ""):
        coins = _to_int(_tag_attrs(m.group(1)).get("coins", ""))
        if coins:
            total += coins
    return total


def looks_like_login(text: str) -> bool:
    """Heuristic that a response is actually a login page (session expired)."""
    low = (text or "").lower()
    return 'type="password"' in low or 'name="password"' in low


def _parse_backup_codes(raw: str) -> list[str]:
    """"['A', 'B']" → ['A', 'B']. Tolerates single/double quotes or plain lists."""
    if not raw:
        return []
    raw = _html.unescape(raw)
    quoted = re.findall(r"'([^']*)'|\"([^\"]*)\"", raw)
    codes = [a or b for a, b in quoted]
    if not codes:  # no quotes at all — fall back to splitting a bare list
        codes = re.split(r"[,\[\]\s]+", raw)
    return [c.strip() for c in codes if c and c.strip()]


def _to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _to_float(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(m.group()) if m else None


def _tag_attrs(attr_blob: str) -> dict[str, str]:
    return {m.group(1).lower(): _html.unescape(m.group(2)) for m in _ATTR_RE.finditer(attr_blob)}


_SENSITIVE_ATTR_RE = re.compile(
    r'((?::)?(?:login|password|backup-codes))(\s*=\s*)"([^"]*)"',
    re.IGNORECASE,
)
# Best-effort safety net for shapes other than the expected <fc-comfortable>
# attributes (e.g. the email appearing as element text or in an embedded JSON
# blob) — emails have a reliable pattern, so this is always safe to strip.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def redact_html_for_debug(html_text: str) -> str:
    """
    Mask login/password/backup-codes attribute VALUES while keeping the
    surrounding markup (tag/attribute names, quoting style, structure) intact,
    plus a generic email-pattern pass as a safety net for page shapes other
    than <fc-comfortable> attributes. Used to save a debug dump when parsing
    misses — safe to inspect/share without exposing the real account email.

    NOT a categorical guarantee: a password or backup code has no reliable
    generic pattern, so if the real page renders them as plain text (rather
    than the expected attributes), they could still end up in the dump. Treat
    files under data/dsfut_debug/ as sensitive regardless.
    """
    text = _SENSITIVE_ATTR_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}"[REDACTED]"', html_text or "")
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return text


def parse_active_order(html_text: str, order_id) -> dict | None:
    """
    Find the <fc-comfortable> block whose id matches *order_id* (there may be
    several active orders on the page) and pull out the account details.
    Returns None if no block matches that id.
    """
    target = str(order_id)
    for m in _FC_TAG_RE.finditer(html_text or ""):
        attrs = _tag_attrs(m.group(1))
        if attrs.get("id", "").strip() != target:
            continue
        return {
            "order_id": target,
            "email": attrs.get("login", "").strip(),
            "password": attrs.get("password", ""),
            "backup_codes": _parse_backup_codes(
                attrs.get(":backup-codes") or attrs.get("backup-codes") or ""
            ),
            "coins": _to_int(attrs.get("coins", "")),
            "amount": _to_float(attrs.get("amount", "")),
            "coins_raw": attrs.get("coins", ""),
            "amount_raw": attrs.get("amount", ""),
        }
    return None
