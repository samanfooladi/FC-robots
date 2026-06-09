from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def confirm_remove_client(telegram_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Yes, remove",
            callback_data=f"remove_client_confirm:{telegram_id}",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data="remove_client_cancel",
        ),
    )
    return builder.as_markup()


def confirm_order(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Confirm",
            callback_data=f"order_confirm:{order_id}",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"order_cancel:{order_id}",
        ),
    )
    return builder.as_markup()
