import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from bot.services.channel_sub import (
    is_user_subscribed,
    SUB_REQUIRED_TEXT,
    get_join_channel_keyboard,
)
from bot.services.user_storage import save_or_update_user

logger = logging.getLogger(__name__)


class ChannelSubscriptionMiddleware(BaseMiddleware):
    """Middleware enforcing membership in the official channel before bot usage and tracking users."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Track user in database
        if hasattr(event, "from_user") and event.from_user:
            save_or_update_user(
                user_id=event.from_user.id,
                username=event.from_user.username,
                first_name=event.from_user.first_name,
                last_name=event.from_user.last_name,
            )

        # 1. Handle incoming Message
        if isinstance(event, Message):
            # Only enforce in private chat with the bot
            if event.chat.type == "private" and event.from_user:
                bot = data["bot"]
                subscribed = await is_user_subscribed(bot, event.from_user.id)
                if not subscribed:
                    await event.reply(
                        SUB_REQUIRED_TEXT,
                        reply_markup=get_join_channel_keyboard(),
                        parse_mode="Markdown",
                    )
                    return

        # 2. Handle incoming CallbackQuery
        elif isinstance(event, CallbackQuery):
            # Always allow the verification button itself
            if event.data == "verify_subscription":
                return await handler(event, data)

            # For any other callback in private chat
            if event.message and event.message.chat.type == "private" and event.from_user:
                bot = data["bot"]
                subscribed = await is_user_subscribed(bot, event.from_user.id)
                if not subscribed:
                    await event.answer(
                        "🔒 ابتدا باید در کانال عضو شوید و روی «تایید عضویت» بزنید!",
                        show_alert=True,
                    )
                    return

        return await handler(event, data)
