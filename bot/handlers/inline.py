import asyncio
import html
import logging
import re
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
    FSInputFile,
)
from aiogram.enums import ChatAction
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR, SERVER_PUBLIC_URL
from bot.services.spotify import (
    search_spotify,
    get_or_prepare_spotify_mp3,
    download_spotify_track_meta,
    SpotifyTrackMetadata,
)
from bot.services.cache import get_cached_audio, save_cached_audio
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="inline_router")


# -------------------------------------------------------------
# 1. Inline Query Handler (Called in any chat: @Timod27_Bot <query>)
# -------------------------------------------------------------
@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    query = inline_query.query.strip()
    me = await inline_query.bot.get_me()
    bot_username = me.username or "Timod27_Bot"
    user_id = inline_query.from_user.id

    logger.info("Spotify inline search by user %s: '%s'", user_id, query)

    # 1. Empty query prompt
    if not query:
        prompt_item = InlineQueryResultArticle(
            id="prompt",
            title="🔍 نام آهنگ یا خواننده را بنویسید...",
            description="جستجوی مستقیم از کاتالوگ اسپاتیفای و ارسال فوری موزیک",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "🎵 <b>جستجوی اینلاین موزیک از اسپاتیفای:</b>\n\n"
                    f"نام آهنگ یا خواننده مورد نظر را بنویسید تا فایل باکیفیت آن درجا ارسال شود:\n"
                    f"<code>@{bot_username} نام آهنگ یا خواننده</code>"
                ),
                parse_mode="HTML",
            ),
        )
        await inline_query.answer(results=[prompt_item], cache_time=1, is_personal=True)
        return

    # 2. Direct Spotify Catalog Search
    try:
        tracks = await search_spotify(query, limit=10)
    except Exception as e:
        logger.exception("Error searching Spotify inline: %s", e)
        tracks = []

    if not tracks:
        no_result = InlineQueryResultArticle(
            id="no_result",
            title="❌ نتیجه‌ای در اسپاتیفای یافت نشد",
            description=f"برای عبارت '{query}' آهنگی پیدا نشد.",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ متأسفانه برای عبارت <b>{html.escape(query)}</b> آهنگی در اسپاتیفای یافت نشد.",
                parse_mode="HTML",
            ),
        )
        await inline_query.answer(results=[no_result], cache_time=5, is_personal=True)
        return

    # 3. Background pre-cache top tracks without blocking inline search response
    if tracks:
        asyncio.create_task(get_or_prepare_spotify_mp3(tracks[0]))
        if len(tracks) > 1:
            asyncio.create_task(get_or_prepare_spotify_mp3(tracks[1]))
        if len(tracks) > 2:
            asyncio.create_task(get_or_prepare_spotify_mp3(tracks[2]))

    # 4. Construct direct Audio Results using a fast dummy URL to bypass Telegram timeouts
    results = []
    dummy_audio_url = "https://github.com/anars/blank-audio/raw/master/1-second-of-silence.mp3"
    
    for track in tracks:
        cache_key = f"sp_{track.track_id}"
        cached = get_cached_audio(cache_key)

        # A) Instant Telegram-cached audio (0.1s send)
        if cached and cached.get("file_id"):
            results.append(
                InlineQueryResultCachedAudio(
                    id=cache_key,
                    audio_file_id=cached["file_id"],
                    caption=None,
                )
            )
        else:
            # B) Dummy Audio result that sends instantly. The ChosenInlineResult handler will replace it!
            results.append(
                InlineQueryResultAudio(
                    id=cache_key,
                    audio_url=dummy_audio_url,
                    title=f"⏳ در حال آماده‌سازی: {track.title}",
                    performer=track.artist,
                    audio_duration=track.duration if track.duration > 0 else None,
                    thumbnail_url=track.cover_url,
                    caption="⏳ <i>لطفاً چند لحظه صبر کنید تا فایل اصلی از سرور دریافت و جایگزین شود...</i>",
                    parse_mode="HTML"
                )
            )

    try:
        await inline_query.answer(results=results, cache_time=5, is_personal=False)
    except Exception as e:
        logger.exception("Error answering inline query: %s", e)


# -------------------------------------------------------------
# 1.5. Chosen Inline Result Handler (Replaces dummy audio with real audio)
# -------------------------------------------------------------
from aiogram.types import ChosenInlineResult, InputMediaAudio

@router.chosen_inline_result(F.result_id.startswith("sp_"))
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    track_id = chosen.result_id.removeprefix("sp_")
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        return

    # Background task to fetch real audio and replace the dummy
    async def process_and_edit():
        try:
            from bot.services.spotify import get_spotify_track_metadata
            meta = await asyncio.to_thread(get_spotify_track_metadata, f"https://open.spotify.com/track/{track_id}")
            if not meta or not meta.title:
                meta = SpotifyTrackMetadata(title="Music Track", artist="Artist", duration=0, cover_url=None, track_id=track_id)

            spot_track = await download_spotify_track_meta(meta)
            
            from bot.config import ADMIN_ID
            # Upload to admin dump to get file_id
            dump_msg = await chosen.bot.send_audio(
                chat_id=ADMIN_ID,
                audio=FSInputFile(spot_track.file_path),
                thumbnail=FSInputFile(spot_track.thumbnail_path) if spot_track.thumbnail_path and spot_track.thumbnail_path.exists() else None,
                title=spot_track.title,
                performer=spot_track.artist,
                duration=spot_track.duration,
                disable_notification=True
            )
            file_id = dump_msg.audio.file_id
            
            # Save to cache
            save_cached_audio(
                track_id=chosen.result_id,
                file_id=file_id,
                title=spot_track.title,
                artist=spot_track.artist,
                duration=spot_track.duration,
            )
            safe_remove(spot_track.file_path, spot_track.thumbnail_path)

            # Replace the dummy audio message in the chat!
            caption = f"🎵 <b>{html.escape(spot_track.title)}</b>\n👤 {html.escape(spot_track.artist)}\n\n🤖 دانلود شده توسط ربات @Timod27_Bot"
            await chosen.bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAudio(media=file_id, caption=caption, parse_mode="HTML")
            )
        except Exception as e:
            logger.exception("Error replacing chosen inline result: %s", e)
            try:
                await chosen.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption="❌ متأسفانه در آماده‌سازی این قطعه خطایی رخ داد."
                )
            except Exception:
                pass

    asyncio.create_task(process_and_edit())



# -------------------------------------------------------------
# 2. Callback Query Handler (When user clicks a song from search list or inline)
# -------------------------------------------------------------
@router.callback_query(F.data.startswith("sp_") | F.data.startswith("play_"))
async def handle_play_callback(callback: CallbackQuery):
    track_id = callback.data.removeprefix("sp_").removeprefix("play_")
    
    # If used via inline query outside bot chat where bot is not a member
    if not callback.message:
        await callback.answer("⏳ لطفا ربات را استارت کنید تا آهنگ ارسال شود", show_alert=True)
        bot_me = await callback.bot.get_me()
        try:
            await callback.bot.edit_message_text(
                inline_message_id=callback.inline_message_id,
                text="🤖 **برای دریافت سریع و مستقیم این آهنگ، ربات را استارت کنید:**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎵 استارت ربات و دانلود آهنگ", url=f"https://t.me/{bot_me.username}?start=dl_{track_id}")
                ]])
            )
        except Exception as e:
            logger.warning("Failed to edit inline message text: %s", e)
        return

    await callback.answer("⏳ در حال دانلود و آماده‌سازی فایل با بالاترین کیفیت...")
    cache_key = f"sp_{track_id}"
    cached = get_cached_audio(cache_key) or get_cached_audio(track_id)
    if cached and cached.get("file_id"):
        await callback.message.reply_audio(
            audio=cached["file_id"],
            title=cached["title"],
            performer=cached["artist"],
            duration=cached["duration"],
            caption=f"🎵 <b>{html.escape(cached['title'])}</b>\n👤 {html.escape(cached['artist'])}\n\n🤖 دانلود سریع از آرشیو",
            parse_mode="HTML",
        )
        return

    status_msg = await callback.message.answer("⏳ در حال دانلود قطعه از اسپاتیفای با بالاترین کیفیت (320kbps)...")
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        from bot.services.spotify import get_spotify_track_metadata
        meta = await asyncio.to_thread(get_spotify_track_metadata, f"https://open.spotify.com/track/{track_id}")
        if not meta or not meta.title:
            meta = SpotifyTrackMetadata(title="Music Track", artist="Artist", duration=0, cover_url=None, track_id=track_id)

        spot_track = await download_spotify_track_meta(meta)
        caption = f"🎵 <b>{html.escape(spot_track.title)}</b>\n👤 {html.escape(spot_track.artist)}\n\n🤖 دانلود شده توسط ربات"

        thumb_input = FSInputFile(spot_track.thumbnail_path) if spot_track.thumbnail_path and spot_track.thumbnail_path.exists() else None
        sent_msg = await callback.message.answer_audio(
            audio=FSInputFile(spot_track.file_path),
            thumbnail=thumb_input,
            title=spot_track.title,
            performer=spot_track.artist,
            duration=spot_track.duration,
            caption=caption,
            parse_mode="HTML",
        )
        await status_msg.delete()

        if sent_msg.audio and sent_msg.audio.file_id:
            save_cached_audio(
                track_id=cache_key,
                file_id=sent_msg.audio.file_id,
                title=spot_track.title,
                artist=spot_track.artist,
                duration=spot_track.duration,
            )
        safe_remove(spot_track.file_path, spot_track.thumbnail_path)

    except Exception as e:
        logger.exception("Error in callback spotify download: %s", e)
        await status_msg.edit_text("❌ خطا در آماده‌سازی و دانلود این قطعه از اسپاتیفای.")

