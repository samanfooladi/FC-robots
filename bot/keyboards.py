from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def accounts_list_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """One button per account for the /accounts login picker."""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        icon = "🟢" if a["is_logged_in"] else "⚪"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {a['email']}",
                callback_data=f"accsel:{a['id']}",
            )
        )
    return builder.as_markup()


def confirm_login_kb(account_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Yes, login", callback_data=f"acclogin:{account_id}"),
        InlineKeyboardButton(text="❌ No", callback_data="accback"),
    )
    return builder.as_markup()


def logout_list_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """One button per logged-in account for the /logout picker."""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"accoutsel:{a['id']}",
            )
        )
    return builder.as_markup()


def confirm_logout_kb(account_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Yes, logout", callback_data=f"accout:{account_id}"),
        InlineKeyboardButton(text="❌ No", callback_data="accoutback"),
    )
    return builder.as_markup()


def order_account_picker_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Which logged-in account should process the order?"""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"ordacc:{a['id']}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="❌ Cancel order", callback_data="ordcancel")
    )
    return builder.as_markup()
