"""
FC Bot — entry point.

Architecture:
  Auth          Playwright login (email + password + single-use backup code)
  Market        search, buy, list, tradepile via httpx
  Telegram Bot  aiogram 3, admin-only: conversational /addaccount,
                clickable /accounts login + /logout, /order with
                account picker
  Queue System  per-account sequential OrderWorker (spawned on login)
  Browser Pool  persistent Chrome profiles; admin-chosen accounts stay
                logged in and are restored after a restart
  Scheduler     APScheduler 9-hour accounting job

Run with:  python main.py
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import ADMIN_IDS, DSFUT_ENABLED
from utils.logger import setup_logging
from db.database import init_db
from bot import create_dispatcher
from bot import bot  # shared Bot instance
from browser_pool.pool import BrowserPool
from dsfut_browser.poller import DsfutPollerManager
from order_queue.manager import QueueManager
from scheduler.runner import create_scheduler


# ---------------------------------------------------------------------------
# Bot command menu
# ---------------------------------------------------------------------------


_ADMIN_COMMANDS = [
    BotCommand(command="start", description="Show what the bot can do"),
    BotCommand(command="accounts", description="List accounts / log one in"),
    BotCommand(command="addaccount", description="Add an EA account (step by step)"),
    BotCommand(command="removeaccount", description="Disable an account by id"),
    BotCommand(command="logout", description="Log an account out"),
    BotCommand(command="refresh", description="Refresh an EA Web App account"),
    BotCommand(command="balance", description="Show an account's coin balance"),
    BotCommand(command="screenshot", description="Screenshot the FC Web App"),
    BotCommand(command="order", description="Buy/list cards in FC Web App"),
    BotCommand(command="setcard", description="Configure the card to trade"),
    BotCommand(command="listcards", description="List configured cards"),
    BotCommand(command="removecard", description="Deactivate a card by id"),
    BotCommand(command="checkcards", description="Check an account's transfer list"),
    BotCommand(command="calculate", description="Calculate bought cards for an account"),
    BotCommand(command="report", description="Send the accounting report now"),
    BotCommand(command="take_on", description="Start DSFUT pickup automation"),
    BotCommand(command="take_off", description="Stop and close DSFUT automation"),
    BotCommand(command="cancel", description="Cancel the current conversation"),
]

# Everyone who is not an admin only ever reaches the client router.
_CLIENT_COMMANDS = [
    BotCommand(command="start", description="How to use this bot"),
    BotCommand(command="order", description="Place an order — /order 100k"),
]


async def _set_bot_commands(bot: Bot) -> None:
    logger = logging.getLogger(__name__)

    await bot.set_my_commands(_CLIENT_COMMANDS, scope=BotCommandScopeDefault())

    # The admin menu is per-chat: Telegram only offers these to the admins.
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                _ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            # Usually "chat not found" — the admin has never opened the bot yet.
            logger.warning("Could not set the admin command menu for %s", admin_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=== FC Bot starting (all phases) ===")

    # 1. DB — create tables + run additive migrations + seed accounts
    await init_db()

    # 2. Browser pool — restore the accounts the admin had logged in before
    #    the restart (persistent Chrome profiles). All other login/logout is
    #    driven from Telegram via /accounts and /logout.
    browser_pool = BrowserPool(bot)
    await browser_pool.start()
    health_check_task = asyncio.create_task(
        browser_pool.health_check_loop(), name="browser-pool-health-check"
    )

    # 3. Queue manager — spawn workers for restored accounts and re-queue
    #    their surviving orders
    queue_manager = QueueManager(bot, browser_pool)
    await queue_manager.start()

    # 4. Accounting scheduler — 9-hour APScheduler job
    scheduler = create_scheduler(bot)
    scheduler.start()
    logger.info("Accounting scheduler started (every 9 hours)")

    # 5. DSFUT poller — browser-drives the dsfut.net board to pick up console
    #    orders (first captcha login is manual; see dsfut_browser).
    #    Wrapped in a manager so /take_on and /take_off can start/stop the
    #    loop at runtime; every restart defaults back to ON (when enabled).
    dsfut_manager = DsfutPollerManager(bot, enabled_in_env=DSFUT_ENABLED)
    if DSFUT_ENABLED:
        try:
            await dsfut_manager.start()
        except Exception:
            logger.exception(
                "DSFUT failed to open at startup — Telegram remains available "
                "so an admin can retry with /take_on"
            )
    else:
        logger.info("DSFUT poller disabled — set DSFUT_ENABLED=true in .env to enable it")

    # 6. Register bot command menu
    await _set_bot_commands(bot)

    # 7. Start Telegram polling
    dp = create_dispatcher()
    logger.info("Bot polling started — press Ctrl+C to stop")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            queue_manager=queue_manager,
            browser_pool=browser_pool,
            dsfut_manager=dsfut_manager,
        )
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
        dsfut_manager.shutdown()
        await queue_manager.stop()
        health_check_task.cancel()
        await browser_pool.stop()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
