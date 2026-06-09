"""
FC Bot — entry point.

Phases implemented:
  Phase 1 — Auth         (Playwright login + OTP + session storage)
  Phase 2 — Market       (search, buy, list, tradepile via httpx)
  Phase 3 — Telegram Bot (aiogram 3, admin + client flows)
  Phase 4 — Queue System (per-account sequential OrderWorker)
  Phase 5 — Scheduler    (APScheduler 9-hour accounting job)

Run with:  python main.py
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommand

from utils.logger import setup_logging
from db.database import init_db
from bot import create_dispatcher
from bot import bot  # shared Bot instance
from order_queue.manager import QueueManager
from scheduler.runner import create_scheduler


# ---------------------------------------------------------------------------
# Bot command menu
# ---------------------------------------------------------------------------


_CLIENT_COMMANDS = [
    BotCommand(command="start", description="Welcome message and instructions"),
    BotCommand(command="order", description="Place an order — /order {amount}"),
]


async def _set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(_CLIENT_COMMANDS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=== FC Bot starting (all phases) ===")

    # 1. DB — create tables + run additive migrations + seed accounts
    await init_db()

    # 2. Queue manager — spawn per-account workers, re-queue surviving orders
    queue_manager = QueueManager(bot)
    await queue_manager.start()

    # 3. Accounting scheduler — 9-hour APScheduler job
    scheduler = create_scheduler(bot)
    scheduler.start()
    logger.info("Accounting scheduler started (every 9 hours)")

    # 4. Register bot command menu
    await _set_bot_commands(bot)

    # 5. Start Telegram polling
    dp = create_dispatcher()
    logger.info("Bot polling started — press Ctrl+C to stop")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            queue_manager=queue_manager,
        )
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
        await queue_manager.stop()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
