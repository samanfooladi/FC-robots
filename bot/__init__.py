from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from bot.router_admin import router as admin_router

# Single Bot instance — imported by notifications.py and main.py
bot = Bot(token=BOT_TOKEN)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    return dp
