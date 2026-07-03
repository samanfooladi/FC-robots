"""
Filters used to gate routes.

IsAdmin — True if the sender's Telegram ID is in ADMIN_IDS from config.
Only admins talk to this bot; there is no client concept anymore.
"""

from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import ADMIN_IDS


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS
