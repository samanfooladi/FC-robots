"""
Outbound notification helpers.

These functions are called by the Phase 5 queue worker (after cards are
listed) and by the Phase 6 scheduler (9-hour accounting job).
They are also called directly from /report in router_admin.py.
"""

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_ts(unix: float) -> str:
    return datetime.utcfromtimestamp(unix).strftime("%Y-%m-%d %H:%M UTC")


async def safe_send(bot: Bot, chat_id: int, text: str) -> None:
    """Send a message; log and skip if the user has blocked the bot."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramForbiddenError:
        logger.warning("Cannot send to %d — user has blocked the bot", chat_id)
    except TelegramBadRequest as exc:
        logger.warning("Bad request sending to %d: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Order completion
# ---------------------------------------------------------------------------


async def send_order_complete(
    bot: Bot,
    client_telegram_id: int,
    order_id: int,
    transactions: list[dict],
) -> None:
    """
    Notify a client that their order has been fully processed.
    *transactions* is the list returned by get_transactions_for_order().
    """
    import time as _time

    logger.info("Sending completion message to %s", client_telegram_id)
    lines = ["✅ <b>سفارش شما آماده شد!</b>\n"]
    for t in transactions:
        name = t.get("player_name") or t.get("card_name", "—")
        bought = t["bought_price"]
        buy_now = t["listed_price"]
        # EA bid increment for 1000-10000 range is 100 coins
        start_bid = (int(buy_now * 0.95) // 100) * 100
        lines.append(
            f"👤 {name}\n"
            f"📦 تعداد: 1\n"
            f"💰 خریداری شده: {bought:,}\n"
            f"🏷 Start Bid: {start_bid:,}\n"
            f"💵 Buy Now: {buy_now:,}\n"
            "─────────────────"
        )

    ts = _fmt_ts(transactions[-1]["listed_at"] if transactions else _time.time())
    lines.append(f"📊 جمع کل: {len(transactions)} کارت")
    lines.append(f"⏰ {ts}")

    text = "\n".join(lines)
    await safe_send(bot, client_telegram_id, text)
    logger.info(
        "Order-complete notification sent to client %d (%d cards, order #%d)",
        client_telegram_id,
        len(transactions),
        order_id,
    )


# ---------------------------------------------------------------------------
# Accounting report
# ---------------------------------------------------------------------------


def _build_report_text(row: dict) -> str:
    """
    Build the report message for a single completed order.

    Profit formula
    ──────────────
    profit_per_card = (list_price × 0.95) − avg_bought_price
    total_profit    = profit_per_card × card_count / 100_000 × order_amount
    """
    list_price: int = row["listed_price"]
    avg_bought: int = row["avg_bought_price"]
    card_count: int = row["card_count"]
    order_amount: int = row["order_amount"]

    profit_per_card = (list_price * 0.95) - avg_bought
    total_profit = profit_per_card * card_count / 100_000 * order_amount

    return (
        "📊 <b>Accounting Report</b>\n\n"
        f"Client: <code>{row['telegram_id']}</code>\n"
        f"Card: <b>{row['card_name']}</b>\n"
        f"Cards bought: <b>{card_count}</b>\n"
        f"Avg bought price: <b>{avg_bought:,}</b>\n"
        f"List price: <b>{list_price:,}</b>\n"
        f"Profit per card: <b>{profit_per_card:,.0f}</b>\n"
        f"Total profit: <b>{total_profit:,.2f}</b>\n"
        f"Completed: {_fmt_ts(row['completed_at'])}"
    )


async def send_accounting_report(
    bot: Bot,
    admin_ids: list[int],
    rows: list[dict],
) -> None:
    """
    Send one report message per completed order to every admin.
    *rows* is the list returned by db.database.get_accounting_report().
    """
    if not rows:
        for admin_id in admin_ids:
            await safe_send(bot,admin_id, "📊 No completed orders to report.")
        return

    for row in rows:
        text = _build_report_text(row)
        for admin_id in admin_ids:
            await safe_send(bot,admin_id, text)

    logger.info("Accounting report (%d order(s)) sent to %d admin(s)", len(rows), len(admin_ids))
