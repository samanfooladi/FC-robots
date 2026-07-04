"""
Homepage order-board scraping — pure DOM helpers, no navigation or state.

The board is a table whose columns are [ID, Platform, Coins, Amount, Pick up].
Each pick-up button matches:

    a.uk-button.uk-button-default[href*="/comfortable/pickup/"]

We locate every such button, walk up to its ancestor <tr> (or nearest parent
if there is no <tr>) and read the row's cells. Everything the loop needs from
one row is returned as a plain dict so the scraping stays testable without a
live browser.
"""

import re

PICKUP_SELECTOR = 'a.uk-button.uk-button-default[href*="/comfortable/pickup/"]'

# Collect every pick-up button with its row cells in a single page evaluation
# so we never hold stale element handles across the async boundary.
SCAN_JS = """
() => {
  const sel = 'a.uk-button.uk-button-default[href*="/comfortable/pickup/"]';
  return Array.from(document.querySelectorAll(sel)).map(btn => {
    const row = btn.closest('tr') || btn.parentElement;
    const cells = row
      ? Array.from(row.querySelectorAll('td, th')).map(c => (c.innerText || '').trim())
      : [];
    return { href: btn.getAttribute('href'), cells };
  });
}
"""


def _to_int_coins(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _to_float_amount(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def order_id_from_href(href: str | None) -> str | None:
    """'/comfortable/pickup/108192' → '108192'."""
    if not href:
        return None
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def parse_order(href: str | None, cells: list[str] | None) -> dict:
    cells = cells or []

    def cell(i: int) -> str:
        return cells[i].strip() if i < len(cells) and cells[i] else ""

    return {
        "href": href,
        "order_id": order_id_from_href(href) or (cell(0) or None),
        "platform": cell(1),
        "coins_raw": cell(2),
        "amount_raw": cell(3),
        "coins": _to_int_coins(cell(2)),
        "amount": _to_float_amount(cell(3)),
        "cells": cells,
    }


def is_eligible(order: dict) -> bool:
    """
    Only "PlayStation / Xbox" orders are eligible. Rows whose Platform column
    contains "PC" (case-insensitive) are skipped. A row whose platform could
    not be read is also skipped — better to miss one than to grab a PC order.
    """
    platform = (order.get("platform") or "").lower()
    if not platform:
        return False
    return "pc" not in platform


async def scan_orders(page) -> list[dict]:
    """Return every pick-up-able order currently on the page, parsed."""
    raw = await page.evaluate(SCAN_JS)
    return [parse_order(r.get("href"), r.get("cells")) for r in (raw or [])]
