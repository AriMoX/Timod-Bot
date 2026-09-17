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
            KeyboardButton(text="📖 راهنمای استفاده"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="❌ انصراف / بازگشت به منوی اصلی"),
        ]
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)
