import logging
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL

logger = logging.getLogger(__name__)

SUB_REQUIRED_TEXT = (
    "🔒 **عضویت در کانال الزامی است!**\n\n"
    "جهت استفاده از تمامی امکانات ربات، ابتدا باید در کانال رسمی مالک ربات عضو شوید:\n\n"
    f"📢 **کانال:** {REQUIRED_CHANNEL}\n\n"
    "پس از عضویت در کانال، روی دکمه **«تایید عضویت ✅»** زیر کلیک کنید تا دسترسی شما باز شود."
)


def get_join_channel_keyboard() -> InlineKeyboardMarkup:
    """Return inline keyboard with channel link and verification button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال",
                    url=REQUIRED_CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ تایید عضویت",
                    callback_data="verify_subscription",
                )
            ],
        ]
    )


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    """Check if the user is a member/admin/creator of REQUIRED_CHANNEL."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        if member.status in (
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        ):
            return True
        if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            return True
        return False
    except Exception as e:
        logger.warning("Error checking channel subscription for user %s: %s", user_id, e)
        return False
