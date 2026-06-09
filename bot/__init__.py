from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from bot.router_admin import router as admin_router
from bot.router_client import router as client_router

# Single Bot instance — imported by notifications.py and main.py
bot = Bot(token=BOT_TOKEN)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # Admin router first so admin commands are matched before client ones
    dp.include_router(admin_router)
    dp.include_router(client_router)
    return dp
