from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.middlewares import IsClient
from db.database import (
    get_active_card,
    get_active_order,
    create_order,
)

if TYPE_CHECKING:
    from order_queue.manager import QueueManager

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_amount(raw: str) -> int | None:
    """
    Parse a coin amount from a string.
    Accepts plain integers ("100000") and shorthand ("100k", "1m").
    Returns None if unparseable.
    """
    raw = raw.strip().lower().replace(",", "").replace("_", "")
    try:
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.reply(
        "👋 <b>Welcome to FC Card Bot!</b>\n\n"
        "I automate buying and listing FC player cards on the Transfer Market.\n\n"
        "<b>How to place an order:</b>\n"
        "  /order {amount} — e.g. <code>/order 100000</code> or <code>/order 100k</code>\n\n"
        "You must be registered by an admin before placing orders.\n"
        "Contact your admin if you cannot place an order.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /order {amount}
# ---------------------------------------------------------------------------


@router.message(Command("order"), IsClient())
async def cmd_order(
    message: Message,
    command: CommandObject,
    db_client: dict,
    queue_manager: QueueManager,
) -> None:
    # ── Parse amount ──────────────────────────────────────────────────────
    if not command.args:
        await message.reply("Usage: /order {amount}  e.g. /order 100000 or /order 100k")
        return

    amount = _parse_amount(command.args)
    if amount is None or amount <= 0:
        await message.reply("❌ Invalid amount. Examples: /order 100000  /order 100k  /order 1m")
        return

    # ── Check for existing active order ───────────────────────────────────
    existing = await get_active_order(message.from_user.id)
    if existing:
        await message.reply(
            "⏳ You already have an active order in progress. "
            "Please wait for it to complete before placing a new one."
        )
        return

    # ── Check card is configured ──────────────────────────────────────────
    card = await get_active_card()
    if not card:
        await message.reply("⚠️ No card is currently configured. Ask an admin to /setcard first.")
        return
    if card["buy_price_max"] <= 0:
        await message.reply("⚠️ Buy price is not set. Ask an admin to /setcard first.")
        return
    if card["list_price"] <= 0:
        await message.reply("⚠️ List price is not set. Ask an admin to /setcard first.")
        return

    # ── Check budget covers at least one card ─────────────────────────────
    # create_order derives quantity from buy_price_max, so validate against
    # the same column (the legacy buy_price column may be absent or 0).
    if amount < card["buy_price_max"]:
        await message.reply(
            f"❌ Order amount ({amount:,}) is less than the current buy price "
            f"({card['buy_price_max']:,}). Please increase your order amount."
        )
        return

    # ── Create order ──────────────────────────────────────────────────────
    order = await create_order(message.from_user.id, amount)
    if order is None:
        await message.reply("❌ Could not create order. Please contact an admin.")
        return

    logger.info(
        "Order #%d created — client %d, card %s, qty %d, amount %d",
        order["id"],
        message.from_user.id,
        card["card_name"],
        order["quantity"],
        amount,
    )

    # Hand off to the account's worker queue
    enqueued = await queue_manager.add_order(order["id"])
    if not enqueued:
        logger.error("Order #%d could not be enqueued — no worker for this account", order["id"])

    await message.reply(
        f"✅ Order received for <b>{amount:,}</b> coins.\n\n"
        f"Card: <b>{card['card_name']}</b>\n"
        f"Cards to buy: <b>{order['quantity']}</b>\n"
        f"Buy price cap: <b>{card['buy_price_max']:,}</b>\n\n"
        "Please wait while we process your cards. "
        "You will be notified when your cards are listed.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Fallback: unregistered user tries a client command
# ---------------------------------------------------------------------------


@router.message(Command("order"))
async def cmd_order_unregistered(message: Message) -> None:
    await message.reply(
        "❌ You are not registered as a client.\n"
        "Please contact an admin to get access."
    )
