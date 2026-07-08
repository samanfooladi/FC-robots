"""
Admin router — the only router; every conversation with this bot is an admin.

Account management
  /addaccount   conversational: email → password → backup code (FSM)
  /accounts     clickable list → per-account menu: login / logout / delete
                (delete is permanent: DB row, orders, history, credential
                 copies in dsfut_orders and the on-disk browser profile)
  /logout       clickable list of logged-in accounts → confirm → logout
  /removeaccount {id}

Evidence / status
  /screenshot   viewport screenshot of a logged-in account's web-app page
                (0 online → error; 1 → straight shot; several → picker)
  /balance      coin balance of a logged-in account
                (same branching, plus an "All accounts" aggregate option)
  /checkcards   Transfer List report (expired/unsold vs closed/sold vs
                active/still listed) read
                from EA's tradepile API, with "Clear Sold" / "Re-list All"
                inline actions clicked via Playwright on the live page
  On every successful /accounts login the admin is also asked whether to
  take a proof-of-balance screenshot right away (Yes/No inline prompt).

Trading
  /order {amount}   0 logged-in accounts → error; 1 → straight to it;
                    several → "which account?" picker
  /setcard, /listcards, /removecard, /report
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message, CallbackQuery

from bot.middlewares import IsAdmin
from bot.keyboards import (
    account_menu_kb,
    accounts_list_kb,
    balance_account_picker_kb,
    calculate_account_picker_kb,
    checkcards_account_picker_kb,
    checkcards_actions_kb,
    confirm_delete_kb,
    confirm_logout_kb,
    logout_list_kb,
    order_account_picker_kb,
    order_no_price_confirm_kb,
    screenshot_account_picker_kb,
    screenshot_prompt_kb,
)
from bot.notifications import _fmt_ts_sec
from browser_pool.profiles import delete_profile_dir
from db.database import (
    add_account,
    create_order,
    deactivate_card,
    delete_account_completely,
    disable_account,
    get_account_by_id,
    get_active_card,
    get_listed_cards_by_trade_ids,
    get_logged_in_accounts,
    list_accounts_overview,
    list_all_cards,
    set_card,
)
from market.credits import get_credits
from market.player_names import get_player_name
from market.tradepile import get_tradepile_response

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
    confirm_no_price = State()
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
        "  /accounts — manage accounts: login / logout / delete\n"
        "  /logout — log an account out\n"
        "  /screenshot — screenshot of a logged-in account\n"
        "  /balance — coin balance of logged-in accounts\n"
        "  /checkcards — transfer list report + Clear Sold / Re-list All\n"
        "  /calculate — sum of C over sold cards (supplier Tomans owed)\n\n"
        "<b>Trading</b>\n"
        "  /setcard — configure the card to trade\n"
        "  /order {amount} [price_per_100k] — place an order, e.g. <code>/order 100k 30000</code>\n"
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
# /accounts — clickable account menu (login / logout / delete)
# ---------------------------------------------------------------------------


async def _accounts_payload() -> tuple[str, object] | None:
    accounts = [a for a in await list_accounts_overview() if a["status"] != "disabled"]
    if not accounts:
        return None
    text = "<b>EA Accounts</b> — tap one to manage it:\n(🟢 logged in / ⚪ logged out)"
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

    status = "🟢 logged in" if account["is_logged_in"] else "⚪ logged out"
    await callback.message.edit_text(
        f"<code>{account['email']}</code> selected — {status}\n\nChoose an action:",
        parse_mode="HTML",
        reply_markup=account_menu_kb(account_id),
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


async def _do_login(
    callback: CallbackQuery,
    account: dict,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    account_id = account["id"]
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

    credits = await get_credits(session)
    coins_line = f"\n\nCoins: {credits:,}" if credits is not None else ""

    await callback.message.edit_text(
        f"✅ Logged in successfully as <code>{account['email']}</code> — "
        f"please send your order:  <code>/order 100k</code>"
        f"{coins_line}",
        parse_mode="HTML",
    )
    await callback.message.answer(
        f"📸 Take screenshot of <code>{account['email']}</code> now that it is logged in?",
        parse_mode="HTML",
        reply_markup=screenshot_prompt_kb(account_id),
    )


@router.callback_query(F.data.startswith("accdo:"))
async def cb_account_action(
    callback: CallbackQuery,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    _, action, raw_id = callback.data.split(":")
    account_id = int(raw_id)
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return

    if action == "login":
        if account["is_logged_in"]:
            await callback.answer(
                f"{account['email']} is already logged in.", show_alert=True
            )
            return
        await _do_login(callback, account, queue_manager, browser_pool)

    elif action == "logout":
        if not account["is_logged_in"]:
            await callback.answer(
                f"{account['email']} is not logged in.", show_alert=True
            )
            return
        await callback.answer()
        await queue_manager.stop_worker(account_id)
        await browser_pool.logout_account(account_id)
        await callback.message.edit_text(
            f"✅ Logged out from <code>{account['email']}</code>.\n"
            f"Pending orders for this account stay queued in the database and "
            f"resume when it logs back in.",
            parse_mode="HTML",
        )

    elif action == "delete":
        await callback.message.edit_text(
            f"⚠️ Delete <code>{account['email']}</code> <b>permanently</b>?\n\n"
            f"This removes everything the bot stores for it:\n"
            f"• credentials and saved session\n"
            f"• its orders and transaction history\n"
            f"• credential copies in DSFUT order records\n"
            f"• the browser profile on disk\n\n"
            f"This cannot be undone.",
            parse_mode="HTML",
            reply_markup=confirm_delete_kb(account_id),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("accdel:"))
async def cb_account_delete(
    callback: CallbackQuery,
    queue_manager: "QueueManager",
    browser_pool: "BrowserPool",
) -> None:
    account_id = int(callback.data.split(":")[1])
    await callback.answer()
    await callback.message.edit_text("⏳ Deleting account…")

    # Tear down the runtime first so Chrome releases the profile directory.
    await queue_manager.stop_worker(account_id)
    await browser_pool.logout_account(account_id)

    account = await delete_account_completely(account_id)
    if account is None:
        await callback.message.edit_text("⚠️ Account not found — nothing deleted.")
        return

    profile_gone = delete_profile_dir(account_id, account.get("profile_path"))
    if not profile_gone:
        # Chrome can hold Windows file locks for a moment after closing.
        await asyncio.sleep(1.0)
        profile_gone = delete_profile_dir(account_id, account.get("profile_path"))

    lines = [
        f"✅ <b>Account deleted</b> — <code>{account['email']}</code>",
        "Removed: credentials, saved session, orders & transaction history, "
        "and credential copies in DSFUT order records.",
    ]
    if profile_gone:
        lines.append("Browser profile folder removed from disk.")
    else:
        lines.append(
            f"⚠️ The browser profile folder could not be fully removed — "
            f"delete <code>data/profiles/{account_id}</code> manually."
        )
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")


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
# /screenshot & /balance — proof-of-balance evidence for order fulfilment
# ---------------------------------------------------------------------------


_SHOT_FAIL_TEXT = (
    "❌ Could not take a screenshot of <code>{email}</code> — "
    "browser session unavailable."
)


async def _send_screenshot(
    message: Message, browser_pool: "BrowserPool", account: dict
) -> bool:
    """Capture the account's live page and send it as a photo. True on success."""
    png = await browser_pool.screenshot_account(account["id"])
    if png is None:
        return False
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await message.answer_photo(
        BufferedInputFile(png, filename=f"account_{account['id']}.png"),
        caption=f"📸 <code>{account['email']}</code> — {stamp}",
        parse_mode="HTML",
    )
    return True


async def _account_balance(browser_pool: "BrowserPool", account_id: int) -> int | None:
    session = await browser_pool.get_session(account_id)
    if session is None:
        return None
    return await get_credits(session)


@router.callback_query(F.data.startswith("shotyes:"))
async def cb_login_screenshot_yes(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"⏳ Taking screenshot of <code>{account['email']}</code>…",
        parse_mode="HTML",
    )
    ok = await _send_screenshot(callback.message, browser_pool, account)
    await callback.message.edit_text(
        f"📸 Screenshot of <code>{account['email']}</code> sent."
        if ok else _SHOT_FAIL_TEXT.format(email=account["email"]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "shotno")
async def cb_login_screenshot_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Ok — no screenshot.")
    await callback.answer()


@router.message(Command("screenshot"))
async def cmd_screenshot(message: Message, browser_pool: "BrowserPool") -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("No account is currently logged in.")
        return

    if len(accounts) == 1:
        account = accounts[0]
        ok = await _send_screenshot(message, browser_pool, account)
        if not ok:
            await message.reply(
                _SHOT_FAIL_TEXT.format(email=account["email"]), parse_mode="HTML"
            )
        return

    await message.reply(
        "<b>Logged-in accounts</b> — tap one to screenshot it:",
        parse_mode="HTML",
        reply_markup=screenshot_account_picker_kb(accounts),
    )


@router.callback_query(F.data.startswith("shotacc:"))
async def cb_screenshot_account(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"⏳ Taking screenshot of <code>{account['email']}</code>…",
        parse_mode="HTML",
    )
    ok = await _send_screenshot(callback.message, browser_pool, account)
    await callback.message.edit_text(
        f"📸 Screenshot of <code>{account['email']}</code> sent."
        if ok else _SHOT_FAIL_TEXT.format(email=account["email"]),
        parse_mode="HTML",
    )


def _balance_line(account: dict, credits: int | None) -> str:
    amount = f"<b>{credits:,}</b> coins" if credits is not None else "❌ unavailable"
    return f"<code>{account['email']}</code> — {amount}"


@router.message(Command("balance"))
async def cmd_balance(message: Message, browser_pool: "BrowserPool") -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("No account is currently logged in.")
        return

    if len(accounts) == 1:
        account = accounts[0]
        status = await message.reply("⏳ Fetching balance…")
        credits = await _account_balance(browser_pool, account["id"])
        await status.edit_text(_balance_line(account, credits), parse_mode="HTML")
        return

    await message.reply(
        "<b>Logged-in accounts</b> — whose balance?",
        parse_mode="HTML",
        reply_markup=balance_account_picker_kb(accounts),
    )


@router.callback_query(F.data.startswith("balacc:"))
async def cb_balance_account(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    target = callback.data.split(":")[1]
    await callback.answer()

    if target == "all":
        accounts = await get_logged_in_accounts()
        if not accounts:
            await callback.message.edit_text("No account is currently logged in.")
            return
        await callback.message.edit_text("⏳ Fetching balances…")
        lines = ["💰 <b>Balances</b>"]
        for account in accounts:
            credits = await _account_balance(browser_pool, account["id"])
            lines.append(_balance_line(account, credits))
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
        return

    account = await get_account_by_id(int(target))
    if account is None:
        await callback.message.edit_text("⚠️ Account not found.")
        return
    await callback.message.edit_text("⏳ Fetching balance…")
    credits = await _account_balance(browser_pool, account["id"])
    await callback.message.edit_text(_balance_line(account, credits), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /checkcards — Transfer List report + Clear Sold / Re-list All actions
# ---------------------------------------------------------------------------


def _tp_version(item: dict) -> str:
    """
    Human "Version" string from itemData. There is no configured card name to
    reuse here (the /order message shows the card config's name), so the
    version is derived from the item itself: rating tier + rareflag.
    Unknown rareflags are surfaced verbatim rather than guessed.
    """
    rating = item.get("rating") or 0
    tier = "Gold" if rating >= 75 else "Silver" if rating >= 65 else "Bronze"
    rare = item.get("rareflag")
    if rare in (0, None):
        kind = "Common"
    elif rare == 1:
        kind = "Rare"
    else:
        kind = f"Special (rareflag={rare})"
    return f"{tier} {kind}"


def _fmt_remaining(seconds: int) -> str:
    """Auction time left, like the EA web app ("59 Minutes", "1h 2m")."""
    if seconds < 60:
        return f"{seconds} Seconds"
    if seconds < 3600:
        return f"{seconds // 60} Minutes"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _tp_expiry_lines(entry: dict) -> list[str]:
    """
    Time Remaining / Expires At lines for an active listing.

    EA sends expiry on the auction object as `expires` = seconds remaining
    (-1 once the auction has ended). Guard anyway: a value large enough to
    be a unix timestamp is treated as the absolute expiry instead.
    """
    expires = entry.get("expires")
    if expires is None:
        expires = (entry.get("itemData") or {}).get("expires")
    if not isinstance(expires, (int, float)) or expires <= 0:
        return ["Time Remaining: —", "Expires At: —"]

    now = time.time()
    if expires > 1_000_000_000:  # absolute unix timestamp, not a duration
        remaining = max(int(expires - now), 0)
        expires_at = expires
    else:
        remaining = int(expires)
        expires_at = now + expires
    return [
        f"Time Remaining: {_fmt_remaining(remaining)}",
        f"Expires At: {_fmt_ts_sec(expires_at)}",
    ]


def _bought_card_c(entry: dict, listed: dict | None) -> int | None:
    """
    C (Tomans owed to the coin supplier) for one closed tradepile entry —
    the single source of this logic for both /checkcards and /calculate.

    T reuses the /order formula — (BuyNowPrice × 0.95) − BoughtPrice — with
    BoughtPrice taken from the listed_cards record saved at list time (the
    tradepile response itself never carries what *we* paid for the card).
    Returns None (rendered/treated as "NotDefinedYet") when the trade has no
    listed_cards record or the order had no supplier rate.
    """
    if not listed or listed.get("price_per_100k") is None:
        return None
    buy_now = entry.get("buyNowPrice") or 0
    t = buy_now * 0.95 - (listed.get("bought_price") or 0)
    return round(t / 100_000 * listed["price_per_100k"])


async def _tp_block(
    entry: dict,
    *,
    sold: bool,
    active: bool = False,
    c_index: int | None = None,
    c_value: int | None = None,
) -> str:
    """One card's lines for the /checkcards report."""
    item = entry.get("itemData") or {}
    resource_id = item.get("resourceId") or item.get("assetId") or 0
    name = await get_player_name(resource_id) or f"Unknown ({resource_id})"
    start = entry.get("startingBid") or 0
    buy_now = entry.get("buyNowPrice") or 0
    ts = item.get("timestamp")
    listed_at = _fmt_ts_sec(ts) if ts else "—"

    lines = [
        f"Player: {escape(name)}",
        f"Version: {_tp_version(item)}",
        f"Start Price: {start:,}",
        f"BuyNow Price: {buy_now:,}",
    ]
    if active:
        lines.extend(_tp_expiry_lines(entry))
    elif sold:
        # Final sale price: itemData.lastSalePrice when EA sends it,
        # otherwise the winning bid (buy-now sales set currentBid = buyNow).
        price = item.get("lastSalePrice") or entry.get("currentBid") or 0
        lines.append(f"Sold For: {price:,}" if price else "Sold For: —")
    else:
        bid = entry.get("currentBid") or 0
        lines.append(f"Price: {bid:,}" if bid else "Price: —")
    if c_index is not None:
        lines.append(
            f"C{c_index}: {c_value:,}" if c_value is not None
            else f"C{c_index}: NotDefinedYet"
        )
    lines.append(f"ListedAt: {listed_at}")
    return "\n".join(lines)


# Diagnostic: log the raw auction fields of the first active listing seen,
# once per process — confirms where EA carries the expiry on active items.
_logged_active_sample = False


async def _build_checkcards_chunks(account: dict, body: dict) -> list[str]:
    """Build the report, packed into Telegram-sized (<4096 char) chunks."""
    global _logged_active_sample
    pile = body.get("auctionInfo") or []
    expired = [e for e in pile if e.get("tradeState") == "expired"]
    closed = [e for e in pile if e.get("tradeState") == "closed"]
    active = [e for e in pile if e.get("tradeState") == "active"]
    other = [
        e for e in pile
        if e.get("tradeState") not in ("expired", "closed", "active")
    ]

    if active and not _logged_active_sample:
        _logged_active_sample = True
        import json as _json
        sample = {k: v for k, v in active[0].items() if k != "itemData"}
        logger.info(
            "checkcards RAW active auction sample (sans itemData): %s",
            _json.dumps(sample, ensure_ascii=False),
        )

    blocks: list[str] = [
        f"📋 <b>Transfer List</b> — <code>{account['email']}</code>\n"
        f"Coins: <b>{body.get('credits', 0):,}</b>"
    ]

    blocks.append("<b>#expired</b>")
    for e in expired:
        blocks.append(await _tp_block(e, sold=False))
    blocks.append(f"Expired cards count: <b>{len(expired)}</b>")

    listed_map = await get_listed_cards_by_trade_ids(
        [e.get("tradeId") for e in closed]
    )
    blocks.append("<b>#bought_cards</b>")
    for n, e in enumerate(closed, start=1):
        c_value = _bought_card_c(e, listed_map.get(e.get("tradeId")))
        blocks.append(await _tp_block(e, sold=True, c_index=n, c_value=c_value))
    blocks.append(f"Bought cards count: <b>{len(closed)}</b>")

    blocks.append("<b>#active_cards</b>")
    for e in active:
        blocks.append(await _tp_block(e, sold=False, active=True))
    blocks.append(f"Active cards count: <b>{len(active)}</b>")

    if other:
        counts = Counter(str(e.get("tradeState")) for e in other)
        detail = ", ".join(f"<code>{escape(s)}</code> × {n}" for s, n in counts.items())
        blocks.append(
            f"⚠️ {len(other)} item(s) with unhandled tradeState excluded "
            f"from all sections: {detail}"
        )

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > 4000:  # headroom under Telegram's 4096 limit
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _send_checkcards_report(
    message: Message, browser_pool: "BrowserPool", account: dict
) -> bool:
    """Fetch the tradepile and send the report (actions kb on the last chunk)."""
    session = await browser_pool.get_session(account["id"])
    if session is None:
        await message.answer(
            f"❌ No live session for <code>{account['email']}</code> — "
            f"log it in via /accounts first.",
            parse_mode="HTML",
        )
        return False

    body = await get_tradepile_response(session)
    if body is None:
        await message.answer(
            f"❌ Could not fetch the transfer list for "
            f"<code>{account['email']}</code> — the session may have expired; "
            f"try again in a moment.",
            parse_mode="HTML",
        )
        return False

    chunks = await _build_checkcards_chunks(account, body)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=checkcards_actions_kb(account["id"]) if is_last else None,
        )
    return True


@router.message(Command("checkcards"))
async def cmd_checkcards(message: Message, browser_pool: "BrowserPool") -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("No account is currently logged in.")
        return

    if len(accounts) == 1:
        await _send_checkcards_report(message, browser_pool, accounts[0])
        return

    await message.reply(
        "<b>Logged-in accounts</b> — whose transfer list?",
        parse_mode="HTML",
        reply_markup=checkcards_account_picker_kb(accounts),
    )


@router.callback_query(F.data.startswith("ccacc:"))
async def cb_checkcards_account(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"📋 Transfer list of <code>{account['email']}</code> ⤵",
        parse_mode="HTML",
    )
    await _send_checkcards_report(callback.message, browser_pool, account)


@router.callback_query(F.data.startswith("ccact:"))
async def cb_checkcards_action(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    _, action, raw_id = callback.data.split(":")
    account_id = int(raw_id)
    label = "Clear Sold" if action == "clear" else "Re-list All"

    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer(
        f"⏳ Clicking <b>{label}</b> on <code>{account['email']}</code>'s "
        f"Transfer List…",
        parse_mode="HTML",
    )

    ok, err = await browser_pool.click_tradepile_button(account_id, label)
    if not ok:
        await status.edit_text(
            f"❌ Could not click <b>{label}</b> for "
            f"<code>{account['email']}</code>: {escape(err) or 'button not found'}.\n"
            f"Open the Transfer List in the account's browser window and try again.",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        f"✅ <b>{label}</b> clicked — fetching the updated list…",
        parse_mode="HTML",
    )
    await _send_checkcards_report(callback.message, browser_pool, account)


# ---------------------------------------------------------------------------
# /calculate — sum C over all bought (sold) cards of an account
# ---------------------------------------------------------------------------


async def _send_calculate_report(
    message: Message, browser_pool: "BrowserPool", account: dict
) -> None:
    """Fetch the tradepile and reply with the running C sum (c1 + c2 + …)."""
    session = await browser_pool.get_session(account["id"])
    if session is None:
        await message.answer(
            f"❌ No live session for <code>{account['email']}</code> — "
            f"log it in via /accounts first.",
            parse_mode="HTML",
        )
        return

    body = await get_tradepile_response(session)
    if body is None:
        await message.answer(
            f"❌ Could not fetch the transfer list for "
            f"<code>{account['email']}</code> — the session may have expired; "
            f"try again in a moment.",
            parse_mode="HTML",
        )
        return

    pile = body.get("auctionInfo") or []
    closed = [e for e in pile if e.get("tradeState") == "closed"]
    if not closed:
        await message.answer(
            f"ℹ️ No bought cards on <code>{account['email']}</code>'s "
            f"transfer list — nothing to calculate.",
            parse_mode="HTML",
        )
        return

    listed_map = await get_listed_cards_by_trade_ids(
        [e.get("tradeId") for e in closed]
    )
    values = [_bought_card_c(e, listed_map.get(e.get("tradeId"))) for e in closed]
    total = sum(v or 0 for v in values)  # NotDefinedYet cards count as 0
    expr = " + ".join(f"c{n}" for n in range(1, len(values) + 1))
    await message.answer(
        f"🧮 <code>{account['email']}</code>\n{expr} = <b>{total:,}</b>",
        parse_mode="HTML",
    )


@router.message(Command("calculate"))
async def cmd_calculate(message: Message, browser_pool: "BrowserPool") -> None:
    accounts = await get_logged_in_accounts()
    if not accounts:
        await message.reply("No account is currently logged in.")
        return

    if len(accounts) == 1:
        await _send_calculate_report(message, browser_pool, accounts[0])
        return

    await message.reply(
        "<b>Logged-in accounts</b> — whose bought cards to calculate?",
        parse_mode="HTML",
        reply_markup=calculate_account_picker_kb(accounts),
    )


@router.callback_query(F.data.startswith("calcacc:"))
async def cb_calculate_account(
    callback: CallbackQuery, browser_pool: "BrowserPool"
) -> None:
    account_id = int(callback.data.split(":")[1])
    account = await get_account_by_id(account_id)
    if account is None:
        await callback.answer("Account not found.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"🧮 Calculating bought cards of <code>{account['email']}</code> ⤵",
        parse_mode="HTML",
    )
    await _send_calculate_report(callback.message, browser_pool, account)


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
# /order {amount} [price_per_100k]
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


def _parse_price_per_100k(raw: str) -> int | None:
    """Admin's per-100k purchase cost in Tomans — a plain positive integer."""
    raw = raw.strip().replace(",", "").replace("_", "")
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


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
    price_per_100k: int | None,
    card: dict,
    queue_manager: "QueueManager",
) -> str:
    """Create the order, enqueue it, and return the confirmation text."""
    order = await create_order(account["id"], ordered_by, amount, price_per_100k)
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


async def _proceed_with_order(
    *,
    send,
    ordered_by: int,
    amount: int,
    price_per_100k: int | None,
    card: dict,
    state: FSMContext,
    queue_manager: "QueueManager",
) -> None:
    """
    Account-selection step shared by /order and the no-price confirmation:
    one logged-in account goes straight to the order, several ask which one.
    *send* delivers text (+ optional keyboard) — message.reply from /order,
    callback.message.edit_text from the Yes button.
    """
    accounts = await get_logged_in_accounts()
    if not accounts:
        await send("⚠️ No account is logged in. Use /accounts to log one in first.")
        return

    if len(accounts) == 1:
        text = await _create_and_enqueue_order(
            account=accounts[0],
            ordered_by=ordered_by,
            amount=amount,
            price_per_100k=price_per_100k,
            card=card,
            queue_manager=queue_manager,
        )
        await send(text)
        return

    # Several accounts are logged in — ask which one should process it.
    await state.set_state(PlaceOrder.pick_account)
    await state.update_data(amount=amount, price_per_100k=price_per_100k)
    await send(
        f"Order of <b>{amount:,}</b> coins — which account should process it?",
        reply_markup=order_account_picker_kb(accounts),
    )


@router.message(Command("order"))
async def cmd_order(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    queue_manager: "QueueManager",
) -> None:
    usage = (
        "Usage: /order {amount} [price_per_100k]\n"
        "e.g. /order 100000 or /order 100k 30000"
    )
    if not command.args:
        await message.reply(usage)
        return

    parts = command.args.split()
    if len(parts) > 2:
        await message.reply(usage)
        return

    amount = _parse_amount(parts[0])
    card = await _validate_order_preconditions(message, amount)
    if card is None:
        return

    price_per_100k: int | None = None
    if len(parts) == 2:
        price_per_100k = _parse_price_per_100k(parts[1])
        if price_per_100k is None:
            await message.reply(
                "❌ Invalid price_per_100k. Example: /order 100k 30000"
            )
            return

    if price_per_100k is None:
        # No supplier price given — ask before proceeding; the completion
        # message will show "Price Per 100k: NotDefinedYet".
        await state.set_state(PlaceOrder.confirm_no_price)
        await state.update_data(amount=amount)
        await message.reply(
            "No price set, continue?",
            reply_markup=order_no_price_confirm_kb(),
        )
        return

    async def _send(text: str, reply_markup=None) -> None:
        await message.reply(text, parse_mode="HTML", reply_markup=reply_markup)

    await _proceed_with_order(
        send=_send,
        ordered_by=message.from_user.id,
        amount=amount,
        price_per_100k=price_per_100k,
        card=card,
        state=state,
        queue_manager=queue_manager,
    )


@router.callback_query(PlaceOrder.confirm_no_price, F.data == "ordpx:yes")
async def cb_order_no_price_yes(
    callback: CallbackQuery,
    state: FSMContext,
    queue_manager: "QueueManager",
) -> None:
    data = await state.get_data()
    await state.clear()
    amount: int = data["amount"]

    card = await get_active_card()
    if card is None:
        await callback.message.edit_text("⚠️ No card is configured anymore. Use /setcard.")
        await callback.answer()
        return

    await callback.answer()

    async def _send(text: str, reply_markup=None) -> None:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )

    await _proceed_with_order(
        send=_send,
        ordered_by=callback.from_user.id,
        amount=amount,
        price_per_100k=None,
        card=card,
        state=state,
        queue_manager=queue_manager,
    )


@router.callback_query(PlaceOrder.confirm_no_price, F.data == "ordpx:no")
async def cb_order_no_price_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "❌ Order cancelled. Re-run /order {amount} {price_per_100k}."
    )
    await callback.answer()


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
    price_per_100k: int | None = data.get("price_per_100k")

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
        price_per_100k=price_per_100k,
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
