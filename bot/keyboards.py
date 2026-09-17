from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🎬 جستجوی فیلم و سریال"),
            KeyboardButton(text="🎵 جستجوی موزیک"),
        ],
        [
            KeyboardButton(text="❌ انصراف / بازگشت به منوی اصلی"),
            KeyboardButton(text="📖 راهنمای استفاده"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

CANCEL_KEYBOARD = MAIN_MENU_KEYBOARD
