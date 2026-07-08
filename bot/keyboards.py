from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def accounts_list_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """One button per account for the /accounts picker."""
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


def account_menu_kb(account_id: int) -> InlineKeyboardMarkup:
    """Action menu shown after an account is selected in /accounts."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔑 Login", callback_data=f"accdo:login:{account_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🚪 Logout", callback_data=f"accdo:logout:{account_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Delete account", callback_data=f"accdo:delete:{account_id}")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Back to accounts", callback_data="accback")
    )
    return builder.as_markup()


def confirm_delete_kb(account_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗑 Yes, delete permanently", callback_data=f"accdel:{account_id}"),
        InlineKeyboardButton(text="❌ No", callback_data=f"accsel:{account_id}"),
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


def screenshot_prompt_kb(account_id: int) -> InlineKeyboardMarkup:
    """Yes/No prompt shown right after a successful login."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📸 Yes", callback_data=f"shotyes:{account_id}"),
        InlineKeyboardButton(text="❌ No", callback_data="shotno"),
    )
    return builder.as_markup()


def screenshot_account_picker_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Which logged-in account should be screenshotted? (/screenshot)"""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"shotacc:{a['id']}",
            )
        )
    return builder.as_markup()


def balance_account_picker_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Which logged-in account's balance to show? (/balance, with All)"""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"balacc:{a['id']}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="📊 All accounts", callback_data="balacc:all")
    )
    return builder.as_markup()


def checkcards_account_picker_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Which logged-in account's Transfer List to inspect? (/checkcards)"""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"ccacc:{a['id']}",
            )
        )
    return builder.as_markup()


def calculate_account_picker_kb(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Which logged-in account to sum bought-card C values for? (/calculate)"""
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.row(
            InlineKeyboardButton(
                text=f"🟢 {a['email']}",
                callback_data=f"calcacc:{a['id']}",
            )
        )
    return builder.as_markup()


def checkcards_actions_kb(account_id: int) -> InlineKeyboardMarkup:
    """Transfer List actions shown under a /checkcards report."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🧹 Clear Sold", callback_data=f"ccact:clear:{account_id}"),
        InlineKeyboardButton(text="🔁 Re-list All", callback_data=f"ccact:relist:{account_id}"),
    )
    return builder.as_markup()


def order_no_price_confirm_kb() -> InlineKeyboardMarkup:
    """/order was sent without price_per_100k — proceed anyway?"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Yes", callback_data="ordpx:yes"),
        InlineKeyboardButton(text="❌ No", callback_data="ordpx:no"),
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
