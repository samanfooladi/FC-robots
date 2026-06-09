"""
APScheduler wrapper for the 9-hour accounting job.

Design notes
────────────
• AsyncIOScheduler runs inside the bot's event loop — no threads needed.
• persistent=False (MemoryJobStore default) is intentional: the `accounted`
  flag in the DB guarantees no double-counting across restarts regardless of
  whether APScheduler remembers the last fire time.
• `misfire_grace_time=None` means a misfired job (e.g. bot was down at the
  scheduled time) runs immediately on next startup rather than being skipped.
"""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.accounting import run_accounting

logger = logging.getLogger(__name__)

_JOB_ID = "accounting_9h"
INTERVAL_HOURS = 9


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Build and return a configured AsyncIOScheduler.
    Call scheduler.start() after creation; stop it in the shutdown handler.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        _accounting_job,
        trigger=IntervalTrigger(hours=INTERVAL_HOURS),
        id=_JOB_ID,
        replace_existing=True,
        misfire_grace_time=None,       # always run if missed
        kwargs={"bot": bot},
    )

    logger.info(
        "Accounting scheduler configured — job '%s' every %d hours",
        _JOB_ID,
        INTERVAL_HOURS,
    )
    return scheduler


async def _accounting_job(bot: Bot) -> None:
    """Thin async wrapper so APScheduler can call run_accounting."""
    logger.info("Scheduled accounting job triggered")
    try:
        count = await run_accounting(bot)
        if count:
            logger.info("Scheduled accounting job: %d order(s) reported", count)
        else:
            logger.info("Scheduled accounting job: nothing to report")
    except Exception:
        logger.exception("Unhandled error in scheduled accounting job")
