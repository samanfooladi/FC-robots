"""
Accounting engine — processes completed, not-yet-accounted orders.

Called by:
  • scheduler/runner.py  every 9 hours (automatic)
  • bot/router_admin.py  on /report (manual trigger)

Each order is marked accounted=True immediately after its report messages
are sent, so a crash mid-run cannot double-count any order.
"""

import logging

from aiogram import Bot

from bot.notifications import send_accounting_report
from config import ADMIN_IDS
from db.database import get_unaccounted_orders, mark_order_accounted

logger = logging.getLogger(__name__)


async def run_accounting(bot: Bot) -> int:
    """
    Fetch all unaccounted done orders, send one report per order to every
    admin, then mark each order as accounted.

    Returns the number of orders processed (0 means nothing new to report).
    """
    rows = await get_unaccounted_orders()

    if not rows:
        logger.info("Accounting run: no new completed orders to account")
        return 0

    logger.info("Accounting run: processing %d order(s)", len(rows))
    processed = 0

    for row in rows:
        order_id: int = row["order_id"]
        try:
            # Send report for this single order to all admins.
            # send_accounting_report uses _safe_send internally so a blocked
            # admin cannot abort the loop.
            await send_accounting_report(bot, ADMIN_IDS, [row])

            # Mark immediately after sending — if we crash here the worst
            # case is one duplicate report, not a missed one.
            await mark_order_accounted(order_id)
            processed += 1

            logger.info(
                "Order #%d accounted — client=%s card=%s cards=%d profit≈%.0f",
                order_id,
                row["telegram_id"],
                row["card_name"],
                row["card_count"],
                _calc_profit(row),
            )

        except Exception:
            logger.exception(
                "Failed to process accounting for order #%d — will retry next run",
                order_id,
            )
            # Don't mark as accounted; it will be picked up again next run.

    logger.info("Accounting run complete: %d/%d order(s) processed", processed, len(rows))
    return processed


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _calc_profit(row: dict) -> float:
    """Quick profit estimate for the log line (mirrors notifications formula)."""
    list_price: int = row.get("listed_price", 0)
    avg_bought: int = row.get("avg_bought_price", 0)
    card_count: int = row.get("card_count", 0)
    order_amount: int = row.get("order_amount", 0)
    profit_per_card = (list_price * 0.95) - avg_bought
    return profit_per_card * card_count / 100_000 * order_amount
