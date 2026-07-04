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
from aiogram.types import BotCommand

from config import DSFUT_ENABLED
from utils.logger import setup_logging
from db.database import init_db
from bot import create_dispatcher
from bot import bot  # shared Bot instance
from browser_pool.pool import BrowserPool
from dsfut_browser.poller import DsfutBrowserPoller
from order_queue.manager import QueueManager
from scheduler.runner import create_scheduler


# ---------------------------------------------------------------------------
# Bot command menu
# ---------------------------------------------------------------------------


_ADMIN_COMMANDS = [
    BotCommand(command="accounts", description="List accounts / log one in"),
    BotCommand(command="logout", description="Log an account out"),
    BotCommand(command="addaccount", description="Add an EA account (step by step)"),
    BotCommand(command="order", description="Place an order — /order {amount}"),
    BotCommand(command="setcard", description="Configure the card to trade"),
    BotCommand(command="listcards", description="List configured cards"),
    BotCommand(command="report", description="Send the accounting report now"),
    BotCommand(command="cancel", description="Cancel the current conversation"),
]


async def _set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(_ADMIN_COMMANDS)


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
    #    orders (first captcha login is manual; see dsfut_browser)
    dsfut_task = None
    if DSFUT_ENABLED:
        dsfut_task = asyncio.create_task(DsfutBrowserPoller(bot).run(), name="dsfut-poller")
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
        )
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
        if dsfut_task:
            dsfut_task.cancel()
        await queue_manager.stop()
        health_check_task.cancel()
        await browser_pool.stop()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
