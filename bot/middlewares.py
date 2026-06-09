"""
Filters used to gate admin and client routes.

IsAdmin   — True if the sender's Telegram ID is in ADMIN_IDS from config.
IsClient  — True if the sender is a registered client in the DB.
            Also injects `db_client: dict` into the handler kwargs so
            handlers don't need a second DB round-trip to fetch client data.
"""

from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import ADMIN_IDS
from db.database import get_client_by_telegram_id


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS


class IsClient(BaseFilter):
    """
    Returns False if not registered.
    Returns {"db_client": <dict>} when registered — aiogram 3 injects
    this dict value as a `db_client` kwarg into the matched handler.
    """

    async def __call__(self, message: Message) -> bool | dict:
        client = await get_client_by_telegram_id(message.from_user.id)
        if client is None:
            return False
        return {"db_client": client}
