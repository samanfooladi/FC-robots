"""
QueueManager — lifecycle manager for all per-account OrderWorkers.

Responsibilities
────────────────
1. On startup: spawn one OrderWorker per account the BrowserPool restored
   (accounts the admin had logged in before the restart), and re-queue
   their surviving pending orders.
2. On admin login (/accounts): start_worker() spawns that account's worker
   and re-queues its pending orders.
3. On admin logout (/logout): stop_worker() cancels the worker task.
4. On new order: route it to the chosen account's worker queue.

Orders for accounts that are currently logged out simply stay 'pending' in
the DB — they are enqueued automatically when that account logs in.
"""

import asyncio
import logging
from aiogram import Bot

from browser_pool.pool import BrowserPool
from db.database import (
    get_account_by_id,
    get_logged_in_accounts,
    get_order_by_id,
    get_pending_orders_for_account,
)
from order_queue.worker import OrderWorker

logger = logging.getLogger(__name__)


class QueueManager:
    def __init__(self, bot: Bot, browser_pool: BrowserPool) -> None:
        self.bot = bot
        self.browser_pool = browser_pool
        self.workers: dict[int, OrderWorker] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Startup
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Called once from main.py after the BrowserPool has been started.
        Spawns workers for restored (logged-in) accounts and re-queues their
        surviving pending orders.
        """
        logger.info("QueueManager starting…")

        for account in await get_logged_in_accounts():
            await self.start_worker(account["id"])

        logger.info("QueueManager ready: %d worker(s)", len(self.workers))

    # ─────────────────────────────────────────────────────────────────────────
    # Worker lifecycle (admin-driven)
    # ─────────────────────────────────────────────────────────────────────────

    async def start_worker(self, account_id: int) -> bool:
        """
        Spawn a worker for one account (startup restore or /accounts login)
        and re-queue its pending orders. Idempotent — returns True if a
        worker already exists.
        """
        if account_id in self.workers:
            return True

        account = await get_account_by_id(account_id)
        if account is None:
            logger.error("start_worker: account %d not found", account_id)
            return False

        session = await self.browser_pool.get_session(account_id)
        if session is None:
            logger.error("start_worker: BrowserPool has no session for account %d", account_id)
            return False

        worker = OrderWorker(
            account_id=account_id,
            email=account["email"],
            session=session,
            bot=self.bot,
            browser_pool=self.browser_pool,
        )
        self.workers[account_id] = worker
        self._tasks[account_id] = asyncio.create_task(
            worker.run(), name=f"worker-account-{account_id}"
        )
        logger.info("Worker started for account %d (%s)", account_id, account["email"])

        # Orders placed (or interrupted) while this account was logged out
        # have been waiting in the DB — pick them up now.
        pending = await get_pending_orders_for_account(account_id)
        for row in pending:
            await worker.queue.put(row["id"])
            logger.info("Re-queued order #%d → account %d", row["id"], account_id)

        return True

    async def stop_worker(self, account_id: int) -> bool:
        """Cancel one account's worker task (admin /logout)."""
        task = self._tasks.pop(account_id, None)
        self.workers.pop(account_id, None)
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("Worker stopped for account %d", account_id)
        return True

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
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.workers.clear()
        logger.info("QueueManager stopped — all worker tasks cancelled")
