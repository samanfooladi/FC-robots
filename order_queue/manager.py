"""
QueueManager — lifecycle manager for all per-account OrderWorkers.

Responsibilities
────────────────
1. On startup: spawn one OrderWorker task per active EA account.
2. On startup: re-queue any orders that were pending/in_progress when the
   bot last shut down (crash-safe restart).
3. On new order: route it to the correct worker queue.
4. Expose queue depth per account for monitoring.
"""

import asyncio
import logging
from aiogram import Bot

from auth.login import login_to_fc
from auth.session import load_session
from config import EA_ACCOUNTS
from db.database import (
    get_all_accounts,
    get_all_pending_orders,
    get_order_by_id,
)
from order_queue.worker import OrderWorker

logger = logging.getLogger(__name__)


class QueueManager:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.workers: dict[int, OrderWorker] = {}
        self._tasks: list[asyncio.Task] = []

    # ─────────────────────────────────────────────────────────────────────────
    # Startup
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Called once from main.py after init_db().
        Spawns workers and re-queues any surviving pending orders.
        """
        logger.info("QueueManager starting…")

        # Build credential lookup by email (from .env config)
        creds_by_email: dict[str, dict] = {a["email"]: a for a in EA_ACCOUNTS}

        for account in await get_all_accounts():
            account_id: int = account["id"]
            email: str = account["email"]

            creds = creds_by_email.get(email)
            if creds is None:
                logger.warning(
                    "Account %d (%s) is in DB but has no matching entry in .env — skipping",
                    account_id,
                    email,
                )
                continue

            session = await load_session(account_id)
            if session is None:
                logger.info(
                    "Account %d (%s): no valid session — attempting login…",
                    account_id,
                    email,
                )
                session = await login_to_fc(
                    account_id=account_id,
                    email=email,
                    password=creds["password"],
                    otp_key=creds["otp_key"],
                )
                if session is None:
                    logger.error(
                        "Account %d (%s): login failed — worker will not be started",
                        account_id,
                        email,
                    )
                    continue

            worker = OrderWorker(
                account_id=account_id,
                email=email,
                password=creds["password"],
                otp_key=creds["otp_key"],
                session=session,
                bot=self.bot,
            )
            self.workers[account_id] = worker
            task = asyncio.create_task(
                worker.run(),
                name=f"worker-account-{account_id}",
            )
            self._tasks.append(task)
            logger.info("Worker started for account %d (%s)", account_id, email)

        # Re-queue orders that were in-flight when the bot last stopped
        pending = await get_all_pending_orders()
        for row in pending:
            order_id: int = row["id"]
            account_id: int = row["account_id"]
            if account_id in self.workers:
                await self.workers[account_id].queue.put(order_id)
                logger.info(
                    "Re-queued order #%d → account %d (startup recovery)",
                    order_id,
                    account_id,
                )
            else:
                logger.warning(
                    "Order #%d belongs to account %d but no worker exists — order stuck",
                    order_id,
                    account_id,
                )

        logger.info(
            "QueueManager ready: %d worker(s), %d order(s) re-queued",
            len(self.workers),
            len(pending),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Runtime
    # ─────────────────────────────────────────────────────────────────────────

    async def add_order(self, order_id: int) -> bool:
        """
        Route a newly created order to the correct worker queue.
        Returns True if successfully enqueued, False if no worker found.
        """
        order = await get_order_by_id(order_id)
        if order is None:
            logger.error("add_order: order #%d not found in DB", order_id)
            return False

        account_id: int = order["account_id"]
        worker = self.workers.get(account_id)
        if worker is None:
            logger.error(
                "add_order: no worker for account %d (order #%d)",
                account_id,
                order_id,
            )
            return False

        await worker.queue.put(order_id)
        logger.info(
            "Order #%d enqueued → account %d (queue depth now %d)",
            order_id,
            account_id,
            worker.queue.qsize(),
        )
        return True

    def get_queue_status(self) -> dict[int, int]:
        """Return {account_id: pending_queue_depth} for all workers."""
        return {aid: w.queue.qsize() for aid, w in self.workers.items()}

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Cancel all worker tasks gracefully."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("QueueManager stopped — all worker tasks cancelled")
