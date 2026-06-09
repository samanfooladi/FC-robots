import asyncio
import random

from config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX


async def human_delay(
    min_s: float | None = None,
    max_s: float | None = None,
) -> None:
    """Sleep for a random interval that mimics human think-time."""
    lo = min_s if min_s is not None else REQUEST_DELAY_MIN
    hi = max_s if max_s is not None else REQUEST_DELAY_MAX
    await asyncio.sleep(random.uniform(lo, hi))
