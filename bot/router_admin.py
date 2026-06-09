import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery

from bot.middlewares import IsAdmin
from bot.keyboards import confirm_remove_client
from db.database import (
    add_client,
    remove_client,
    list_all_clients,
    set_card,
    list_all_cards,
    deactivate_card,
)
from config import ADMIN_IDS

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# /addclient {telegram_id}
# ---------------------------------------------------------------------------


@router.message(Command("addclient"), IsAdmin())
async def cmd_addclient(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Usage: /addclient {telegram_id}")
        return

    telegram_id = int(command.args.strip())
    ok, detail = await add_client(telegram_id)

    if ok:
        await message.reply(
            f"✅ Client <code>{telegram_id}</code> registered.\n"
            f"Assigned to account: <code>{detail}</code>",
            parse_mode="HTML",
        )
    elif detail == "already_registered":
        await message.reply(f"⚠️ Client <code>{telegram_id}</code> is already registered.", parse_mode="HTML")
    else:
        await message.reply("❌ All EA accounts are at maximum capacity. Add a new account first.")


# ---------------------------------------------------------------------------
# /removeclient {telegram_id}
# ---------------------------------------------------------------------------


@router.message(Command("removeclient"), IsAdmin())
async def cmd_removeclient(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Usage: /removeclient {telegram_id}")
        return

    telegram_id = int(command.args.strip())
    await message.reply(
        f"Remove client <code>{telegram_id}</code>?",
        parse_mode="HTML",
        reply_markup=confirm_remove_client(telegram_id),
    )


@router.callback_query(F.data.startswith("remove_client_confirm:"), IsAdmin())
async def cb_remove_client_confirm(callback: CallbackQuery) -> None:
    telegram_id = int(callback.data.split(":")[1])
    removed = await remove_client(telegram_id)
    if removed:
        await callback.message.edit_text(f"✅ Client <code>{telegram_id}</code> removed.", parse_mode="HTML")
    else:
        await callback.message.edit_text(f"⚠️ Client <code>{telegram_id}</code> not found.")
    await callback.answer()


@router.callback_query(F.data == "remove_client_cancel", IsAdmin())
async def cb_remove_client_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Cancelled.")
    await callback.answer()


# ---------------------------------------------------------------------------
# /setcard {name} {min_rating} {max_rating} {buy_min} {buy_max} {list_price}
# ---------------------------------------------------------------------------


@router.message(Command("setcard"), IsAdmin())
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
    start_bid = int(list_price * 0.95)

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


@router.message(Command("listcards"), IsAdmin())
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


@router.message(Command("removecard"), IsAdmin())
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
# /clients
# ---------------------------------------------------------------------------


@router.message(Command("clients"), IsAdmin())
async def cmd_clients(message: Message) -> None:
    clients = await list_all_clients()
    if not clients:
        await message.reply("No clients registered yet.")
        return

    lines = ["<b>Registered Clients</b>\n"]
    for c in clients:
        status = c.get("latest_order_status") or "no orders"
        lines.append(
            f"• <code>{c['telegram_id']}</code> → {c['account_email']} "
            f"[{status}]"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------


@router.message(Command("report"), IsAdmin())
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
