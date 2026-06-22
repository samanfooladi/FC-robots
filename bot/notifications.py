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


def _fmt_ts_sec(unix: float) -> str:
    return datetime.utcfromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S UTC")


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    """
    Send a message; log and skip if the user has blocked the bot.
    Returns True when the message was actually delivered to Telegram.
    """
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return True
    except TelegramForbiddenError:
        logger.warning("Cannot send to %d — user has blocked the bot", chat_id)
    except TelegramBadRequest as exc:
        logger.warning("Bad request sending to %d: %s", chat_id, exc)
    return False


# ---------------------------------------------------------------------------
# Per-card listing notification
# ---------------------------------------------------------------------------


async def send_card_listed(
    bot: Bot,
    client_telegram_id: int,
    player_name: str,
    start_bid: int,
    buy_now: int,
    cards_done: int,
    total: int,
) -> None:
    """Notify the client that one card of their order is now listed."""
    from html import escape

    text = (
        f"🟢 <b>کارت شما لیست شد</b> ({cards_done}/{total})\n\n"
        f"👤 {escape(player_name)}\n"
        f"📦 تعداد: 1\n"
        f"🏷 Start Bid: {start_bid:,}\n"
        f"💵 Buy Now: {buy_now:,}"
    )
    await safe_send(bot, client_telegram_id, text)


# ---------------------------------------------------------------------------
# Order completion
# ---------------------------------------------------------------------------


# Telegram rejects messages longer than this with TelegramBadRequest.
_TELEGRAM_MAX_MESSAGE_LEN = 4096


async def send_order_complete(
    bot: Bot,
    client_telegram_id: int,
    order_id: int,
    order_amount: int,
    transactions: list[dict],
) -> None:
    """
    Notify a client that their order has been fully processed.
    *transactions* is the list returned by get_transactions_for_order().

    Large orders (100+ cards) produce more text than fits in one Telegram
    message, so the per-card blocks are split across as many messages as
    needed — otherwise the whole notification is rejected with
    TelegramBadRequest and the client receives nothing.
    """
    import time as _time
    from html import escape
    from collections import OrderedDict

    logger.info("Sending completion message to %s", client_telegram_id)

    if not client_telegram_id:
        logger.error(
            "Order #%d: client_telegram_id is %r — cannot send completion message",
            order_id,
            client_telegram_id,
        )
        return

    # Group same-player transactions together (preserving first-seen order).
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for t in transactions:
        key = t.get("player_name") or t.get("card_name") or "—"
        groups.setdefault(key, []).append(t)

    blocks: list[str] = []
    for player_name, txs in groups.items():
        count = len(txs)
        total_bought = sum(t["bought_price"] for t in txs)
        buy_now = txs[0]["listed_price"]
        start_bid = (int(buy_now * 0.95) // 100) * 100
        card_version = txs[0].get("card_name", "—")
        position = next((t.get("position") for t in txs if t.get("position")), None)
        first_ts = min(t["listed_at"] for t in txs)
        last_ts = max(t["listed_at"] for t in txs)

        block = (
            f"🔢 Count: {count}\n"
            f"\n"
            f"💰 Initial Seller Coins: {order_amount:,}$\n"
            f"⚽️ Player: {escape(player_name)}\n"
            f"🏆 Version: {escape(card_version)}\n"
            f"🎯 Start Price: {start_bid:,}\n"
            f"🛒 Buynow Price: {buy_now:,}\n"
            f"\n"
            f"✅ Bought Price: {total_bought:,}\n"
        )
        if position:
            block += f"\n📍 Position: {escape(position)}\n"
        block += (
            f"⏳ ListedAt_from: {_fmt_ts_sec(first_ts)}\n"
            f"⌛️ ListedAt_to: {_fmt_ts_sec(last_ts)}"
        )
        blocks.append(block + "\n─────────────────")

    ts = _fmt_ts(transactions[-1]["listed_at"] if transactions else _time.time())
    blocks.append(f"📊 جمع کل: {len(transactions)} کارت\n⏰ {ts}")

    # Pack the blocks into as few messages as possible, each under the limit.
    chunks: list[str] = []
    current = "✅ <b>سفارش شما آماده شد!</b>\n"
    for block in blocks:
        candidate = f"{current}\n{block}"
        if len(candidate) > _TELEGRAM_MAX_MESSAGE_LEN:
            chunks.append(current)
            current = block
        else:
            current = candidate
    chunks.append(current)

    delivered = 0
    for chunk in chunks:
        if await safe_send(bot, client_telegram_id, chunk):
            delivered += 1

    if delivered == len(chunks):
        logger.info(
            "Order-complete notification sent to client %d (%d cards, order #%d, %d message(s))",
            client_telegram_id,
            len(transactions),
            order_id,
            len(chunks),
        )
    else:
        logger.error(
            "Order-complete notification only partially delivered to client %s "
            "(order #%d): %d/%d message(s) sent",
            client_telegram_id,
            order_id,
            delivered,
            len(chunks),
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
