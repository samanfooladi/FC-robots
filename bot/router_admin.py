"""
Admin router — the only router; every conversation with this bot is an admin.

Account management
  /addaccount   conversational: email → password → backup code (FSM)
  /accounts     clickable list → confirm → login (account stays logged in)
  /logout       clickable list of logged-in accounts → confirm → logout
  /removeaccount {id}

Trading
  /order {amount}   0 logged-in accounts → error; 1 → straight to it;
                    several → "which account?" picker
  /setcard, /listcards, /removecard, /report
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from bot.middlewares import IsAdmin
from bot.keyboards import (
    accounts_list_kb,
    confirm_login_kb,
    confirm_logout_kb,
    logout_list_kb,
    order_account_picker_kb,
)
from db.database import (
    add_account,
    create_order,
    deactivate_card,
    disable_account,
    get_account_by_id,
    get_active_card,
    get_logged_in_accounts,
    list_accounts_overview,
    list_all_cards,
    set_card,
)

if TYPE_CHECKING:
    from browser_pool.pool import BrowserPool
    from order_queue.manager import QueueManager

logger = logging.getLogger(__name__)
router = Router()

# Every handler in this router requires an admin sender.
# (IsAdmin only reads .from_user.id, which CallbackQuery also has.)
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------


class AddAccount(StatesGroup):
    email = State()
    password = State()
    backup_code = State()


class PlaceOrder(StatesGroup):
    pick_account = State()


# ---------------------------------------------------------------------------
# /start & /cancel
# ---------------------------------------------------------------------------


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.reply(
        "👋 <b>FC Trading Bot</b>\n\n"
        "<b>Accounts</b>\n"
        "  /addaccount — add an EA account (step by step)\n"
        "  /accounts — list accounts and log one in\n"
        "  /logout — log an account out\n\n"
        "<b>Trading</b>\n"
        "  /setcard — configure the card to trade\n"
        "  /order {amount} — place an order, e.g. <code>/order 100k</code>\n"
        "  /report — profit report",
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.reply("Nothing to cancel.")
        return
    await state.clear()
    await message.reply("❌ Cancelled.")


# ---------------------------------------------------------------------------
# /addaccount — conversational flow
# ---------------------------------------------------------------------------


@router.message(Command("addaccount"))
async def cmd_addaccount(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccount.email)
    await message.reply("Enter your email:  (/cancel to abort)")


@router.message(AddAccount.email)
async def addaccount_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or " " in email or len(email) < 5:
        await message.reply("❌ That doesn't look like an email. Enter your email:")
        return
    await state.update_data(email=email)
    await state.set_state(AddAccount.password)
    await message.reply("Enter your password:")


@router.message(AddAccount.password)
async def addaccount_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.reply("❌ Password cannot be empty. Enter your password:")
        return
    await state.update_data(password=password)
    # The message contains a plaintext password — remove it from the chat.
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(AddAccount.backup_code)
    await message.answer("Enter your backup code:")


@router.message(AddAccount.backup_code)
async def addaccount_backup_code(message: Message, state: FSMContext) -> None:
    backup_code = (message.text or "").strip()
    if not backup_code:
        await message.reply("❌ Backup code cannot be empty. Enter your backup code:")
        return
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    await state.clear()

    ok, detail = await add_account(data["email"], data["password"], backup_code)
    if not ok:
        await message.answer(
            f"⚠️ Account <code>{data['email']}</code> already exists.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"✅ <b>Account created</b> — <code>{data['email']}</code> (id={detail}).\n"
        f"Use /accounts to log it in.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /accounts — clickable login flow
# ---------------------------------------------------------------------------


async def _accounts_payload() -> tuple[str, object] | None:
    accounts = [a for a in await list_accounts_overview() if a["status"] != "disabled"]
    if not accounts:
        return None
    text = "<b>EA Accounts</b> — tap one to log in:\n(🟢 logged in / ⚪ logged out)"
    return text, accounts_list_kb(accounts)


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    payload = await _accounts_payload()
    if payload is None:
        await message.reply("No accounts yet. Use /addaccount to add one.")
        return
    text, kb = payload
    await message.reply(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("accsel:"))
async def cb_account_selected(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    if account["is_logged_in"]:
        await callback.answer(
            f"{account['email']} is already logged in — use /logout to log it out.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"Are you sure you want to login as <code>{account['email']}</code>?",
        parse_mode="HTML",
        reply_markup=confirm_login_kb(account_id),
    )
    await callback.answer()


@router.callback_query(F.data == "accback")
async def cb_account_back(callback: CallbackQuery) -> None:
    payload = await _accounts_payload()
    if payload is None:
        await callback.message.edit_text("No accounts yet. Use /addaccount to add one.")
    else:
        text, kb = payload
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("acclogin:"))
async def cb_account_login(
    callback: CallbackQuery,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        f"⏳ Logging in as <code>{account['email']}</code>…\n"
        f"(first login opens a browser and can take a minute)",
        parse_mode="HTML",
    )

    session = await browser_pool.login_account(account_id)
    if session is None:
        await callback.message.edit_text(
            f"❌ Login failed for <code>{account['email']}</code>.\n"
            f"Check the credentials/backup code and logs, then try again "
            f"from /accounts.",
            parse_mode="HTML",
        )
        return

    ok = await queue_manager.start_worker(account_id)
    if not ok:
        await callback.message.edit_text(
            f"⚠️ <code>{account['email']}</code> logged in, but its worker "
            f"could not be started — check logs.",
            parse_mode="HTML",
        )
        return

    await callback.message.edit_text(
        f"✅ Logged in successfully as <code>{account['email']}</code> — "
        f"please send your order:  <code>/order 100k</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /logout — clickable logout flow
# ---------------------------------------------------------------------------


@router.message(Command("logout"))
async def cmd_logout(message: Message) -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("No account is logged in.")
        return
    await message.reply(
        "<b>Logged-in accounts</b> — tap one to log out:",
        parse_mode="HTML",
        reply_markup=logout_list_kb(accounts),
    )


@router.callback_query(F.data.startswith("accoutsel:"))
async def cb_logout_selected(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Are you sure you want to logout from <code>{account['email']}</code>?",
        parse_mode="HTML",
        reply_markup=confirm_logout_kb(account_id),
    )
    await callback.answer()


@router.callback_query(F.data == "accoutback")
async def cb_logout_back(callback: CallbackQuery) -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await callback.message.edit_text("No account is logged in.")
    else:
        await callback.message.edit_text(
            "<b>Logged-in accounts</b> — tap one to log out:",
            parse_mode="HTML",
            reply_markup=logout_list_kb(accounts),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("accout:"))
async def cb_logout_confirm(
    callback: CallbackQuery,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    email = account["email"] if account else str(account_id)

    await callback.answer()
    await queue_manager.stop_worker(account_id)
    await browser_pool.logout_account(account_id)
    await callback.message.edit_text(
        f"✅ Logged out from <code>{email}</code>.\n"
        f"Pending orders for this account stay queued in the database and "
        f"resume when it logs back in.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /removeaccount {account_id}
# ---------------------------------------------------------------------------


@router.message(Command("removeaccount"))
async def cmd_removeaccount(
    message: Message,
    command: CommandObject,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Usage: /removeaccount {account_id}")
        return

    account_id = int(command.args.strip())
    result = await disable_account(account_id)
    if result == "ok":
        # If it was logged in, tear it down too.
        await queue_manager.stop_worker(account_id)
        await browser_pool.logout_account(account_id)
        await message.reply(f"✅ Account <code>{account_id}</code> removed.", parse_mode="HTML")
    else:
        await message.reply(f"⚠️ Account <code>{account_id}</code> not found.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /order {amount}
# ---------------------------------------------------------------------------


def _parse_amount(raw: str) -> int | None:
    """Accepts plain integers ("100000") and shorthand ("100k", "1m")."""
    raw = raw.strip().lower().replace(",", "").replace("_", "")
    try:
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except (ValueError, TypeError):
        return None


async def _validate_order_preconditions(message: Message, amount: int | None) -> dict | None:
    """Shared checks for /order. Returns the active card dict or None (after replying)."""
    if amount is None or amount <= 0:
        await message.reply("❌ Invalid amount. Examples: /order 100000  /order 100k  /order 1m")
        return None

    card = await get_active_card()
    if not card:
        await message.reply("⚠️ No card is configured. Use /setcard first.")
        return None
    if card["buy_price_max"] <= 0 or card["list_price"] <= 0:
        await message.reply("⚠️ Card prices are not set. Use /setcard first.")
        return None
    if amount < card["list_price"]:
        await message.reply(
            f"❌ Order amount ({amount:,}) is less than the list price of one card "
            f"({card['list_price']:,})."
        )
        return None
    return card


async def _create_and_enqueue_order(
    *,
    account: dict,
    ordered_by: int,
    amount: int,
    card: dict,
    queue_manager: "QueueManager",
) -> str:
    """Create the order, enqueue it, and return the confirmation text."""
    order = await create_order(account["id"], ordered_by, amount)
    if order is None:
        return "❌ Could not create the order — check the card configuration."

    enqueued = await queue_manager.add_order(order["id"])
    if not enqueued:
        logger.error("Order #%d could not be enqueued — no worker", order["id"])
        return (
            f"⚠️ Order #{order['id']} was saved but its account has no running "
            f"worker — it will start when the account logs in again."
        )

    logger.info(
        "Order #%d created — account %s, qty %d, amount %d",
        order["id"], account["email"], order["quantity"], amount,
    )
    return (
        f"✅ Order received for <b>{amount:,}</b> coins on "
        f"<code>{account['email']}</code>.\n\n"
        f"Card: <b>{card['card_name']}</b>\n"
        f"Cards to buy: <b>{order['quantity']}</b>\n"
        f"List price per card: <b>{card['list_price']:,}</b>\n\n"
        f"You will be notified as cards are listed."
    )


@router.message(Command("order"))
async def cmd_order(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    queue_manager: "QueueManager",
) -> None:
    if not command.args:
        await message.reply("Usage: /order {amount}  e.g. /order 100000 or /order 100k")
        return

    amount = _parse_amount(command.args)
    card = await _validate_order_preconditions(message, amount)
    if card is None:
        return

    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("⚠️ No account is logged in. Use /accounts to log one in first.")
        return

    if len(accounts) == 1:
        text = await _create_and_enqueue_order(
            account=accounts[0],
            ordered_by=message.from_user.id,
            amount=amount,
            card=card,
            queue_manager=queue_manager,
        )
        await message.reply(text, parse_mode="HTML")
        return

    # Several accounts are logged in — ask which one should process it.
    await state.set_state(PlaceOrder.pick_account)
    await state.update_data(amount=amount)
    await message.reply(
        f"Order of <b>{amount:,}</b> coins — which account should process it?",
        parse_mode="HTML",
        reply_markup=order_account_picker_kb(accounts),
    )


@router.callback_query(PlaceOrder.pick_account, F.data.startswith("ordacc:"))
async def cb_order_account(
    callback: CallbackQuery,
    state: FSMContext,
    queue_manager: "QueueManager",
) -> None:
    account_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    await state.clear()
    amount: int = data["amount"]

    account = await get_account_by_id(account_id)
    if account is None or not account["is_logged_in"]:
        await callback.answer("That account is not logged in anymore.", show_alert=True)
        await callback.message.edit_text("❌ Order cancelled — account unavailable.")
        return

    card = await get_active_card()
    if card is None:
        await callback.message.edit_text("⚠️ No card is configured anymore. Use /setcard.")
        await callback.answer()
        return

    await callback.answer()
    text = await _create_and_enqueue_order(
        account=account,
        ordered_by=callback.from_user.id,
        amount=amount,
        card=card,
        queue_manager=queue_manager,
    )
    await callback.message.edit_text(text, parse_mode="HTML")


@router.callback_query(F.data == "ordcancel")
async def cb_order_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Order cancelled.")
    await callback.answer()


# ---------------------------------------------------------------------------
# /setcard {name} {min_rating} {max_rating} {buy_min} {buy_max} {list_price}
# ---------------------------------------------------------------------------


@router.message(Command("setcard"))
async def cmd_setcard(message: Message, command: CommandObject) -> None:
    usage = 'Usage: /setcard "Name" {min_rating} {max_rating} {buy_min} {buy_max} {list_price}'
    if not command.args:
        await message.reply(usage)
        return

    # Support quoted name: /setcard "Rare Gold 80-81" 80 81 700 850 3800
    raw = command.args.strip()
    if raw.startswith('"'):
        end_quote = raw.find('"', 1)
        if end_quote == -1:
            await message.reply(usage)
            return
        card_name = raw[1:end_quote]
        rest = raw[end_quote + 1:].split()
    else:
        parts = raw.split(maxsplit=1)
        card_name = parts[0]
        rest = parts[1].split() if len(parts) > 1 else []

    if len(rest) != 5 or not all(p.isdigit() for p in rest):
        await message.reply(usage)
        return

    min_rating, max_rating, buy_min, buy_max, list_price = (int(p) for p in rest)
    # Must match the rounding in db.set_card — floor to a 100-coin tier
    start_bid = (int(list_price * 0.95) // 100) * 100

    card_id = await set_card(
        card_name=card_name,
        min_rating=min_rating,
        max_rating=max_rating,
        buy_price_min=buy_min,
        buy_price_max=buy_max,
        list_price=list_price,
    )
    await message.reply(
        f"✅ Active card set (id={card_id}):\n"
        f"Name: <b>{card_name}</b>\n"
        f"Rating: <b>{min_rating}–{max_rating}</b>  |  Rarity: Rare Gold\n"
        f"Buy range: <b>{buy_min:,}–{buy_max:,}</b>\n"
        f"List price: <b>{list_price:,}</b>  |  Start bid: <b>{start_bid:,}</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /listcards
# ---------------------------------------------------------------------------


@router.message(Command("listcards"))
async def cmd_listcards(message: Message) -> None:
    cards = await list_all_cards()
    if not cards:
        await message.reply("No cards configured yet. Use /setcard to add one.")
        return

    lines = ["<b>Cards</b>\n"]
    for c in cards:
        status = "✅ active" if c["is_active"] else "⏹ inactive"
        lines.append(
            f"[{c['id']}] <b>{c['card_name']}</b> {status}\n"
            f"  Rating {c['min_rating']}–{c['max_rating']} | "
            f"Buy {c['buy_price_min']:,}–{c['buy_price_max']:,} | "
            f"List {c['list_price']:,} | Bid {c['start_bid']:,}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /removecard {card_id}
# ---------------------------------------------------------------------------


@router.message(Command("removecard"))
async def cmd_removecard(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Usage: /removecard {card_id}")
        return

    card_id = int(command.args.strip())
    removed = await deactivate_card(card_id)
    if removed:
        await message.reply(f"✅ Card <code>{card_id}</code> deactivated.", parse_mode="HTML")
    else:
        await message.reply(f"⚠️ Card <code>{card_id}</code> not found.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    from bot import bot
    from scheduler.accounting import run_accounting

    await message.reply("⏳ Running accounting…")
    count = await run_accounting(bot)

    if count == 0:
        await message.reply("ℹ️ No new completed orders since the last report.")
    else:
        await message.reply(
            f"📊 Accounting report sent to all admins — <b>{count}</b> order(s) processed.",
            parse_mode="HTML",
        )
