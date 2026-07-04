"""
Standalone runner:  python -m dsfut_browser

Handy for the first captcha login (and for testing pickups) without starting
the whole bot. Uses the same profile directory as the integrated poller, so a
login done here also authenticates the in-bot poller. Do not run both at once —
the persistent profile can only be opened by one Chromium at a time.
"""

import asyncio

from db.database import init_db
from utils.logger import setup_logging

from .poller import DsfutBrowserPoller


async def _main() -> None:
    setup_logging()
    await init_db()
    await DsfutBrowserPoller(bot=None).run()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
