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

    # 3. Pre-cache top tracks in background and give top 1 track a 2.0s head start
    if tracks:
        top_task = asyncio.create_task(get_or_prepare_spotify_mp3(tracks[0]))
        if len(tracks) > 1:
            asyncio.create_task(get_or_prepare_spotify_mp3(tracks[1]))
        try:
            await asyncio.wait([top_task], timeout=2.0)
        except Exception:
            pass

    # 4. Construct direct Audio Results with NO caption (clean native player)
    results = []
    server_base = SERVER_PUBLIC_URL.rstrip("/")

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
            # B) Direct High-Quality Audio URL (Telegram downloads and sends directly into the chat)
            direct_audio_url = f"{server_base}/audio/sp_{track.track_id}.mp3"
            results.append(
                InlineQueryResultAudio(
                    id=cache_key,
                    audio_url=direct_audio_url,
                    title=track.title,
                    performer=track.artist,
                    audio_duration=track.duration if track.duration > 0 else None,
                    thumbnail_url=track.cover_url,
                    caption=None,
                )
            )

    try:
        await inline_query.answer(results=results, cache_time=120, is_personal=False)
    except Exception as e:
        logger.exception("Error answering inline query: %s", e)



# -------------------------------------------------------------
# 2. In-Chat Text Search Handler (When user sends music title directly in chat)
# -------------------------------------------------------------
@router.message(F.text, ~F.text.startswith("/"))
async def handle_in_chat_text_search(message: Message):
    text = (message.text or "").strip()

    # Ignore links (links are handled by downloader_router)
    if re.search(r"https?://", text):
        return

    # Clean bot username if user typed e.g. "@Timod27_Bot eminem"
    cleaned_query = re.sub(r"@\w+_bot\s*", "", text, flags=re.IGNORECASE).strip()
    if not cleaned_query:
        return

    status_msg = await message.reply(
        f"🔍 در حال جستجوی موزیک در اسپاتیفای برای <b>{html.escape(cleaned_query)}</b>...",
        parse_mode="HTML",
    )
    tracks = await search_spotify(cleaned_query, limit=5)

    if not tracks:
        await status_msg.edit_text(
            f"❌ هیچ آهنگی در اسپاتیفای برای <b>{html.escape(cleaned_query)}</b> یافت نشد.",
            parse_mode="HTML",
        )
        return

    buttons = []
    text_lines = [
        f"🎧 <b>نتایج اسپاتیفای برای:</b> <code>{html.escape(cleaned_query)}</code>\n",
        "برای دانلود هر قطعه با بالاترین کیفیت روی دکمه زیر بزنید:\n"
    ]

    for idx, track in enumerate(tracks, start=1):
        minutes = track.duration // 60
        seconds = track.duration % 60
        dur = f"{minutes:02d}:{seconds:02d}" if track.duration > 0 else ""
        text_lines.append(f"{idx}. 🎵 <b>{html.escape(track.title)}</b> - {html.escape(track.artist)} <code>({dur})</code>")

        buttons.append([
            InlineKeyboardButton(
                text=f"📥 {idx}. {track.title[:30]}",
                callback_data=f"sp_{track.track_id}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text("\n".join(text_lines), reply_markup=keyboard, parse_mode="HTML")


# -------------------------------------------------------------
# 3. Callback Query Handler (When user clicks a song from search list)
# -------------------------------------------------------------
@router.callback_query(F.data.startswith("sp_") | F.data.startswith("play_"))
async def handle_play_callback(callback: CallbackQuery):
    track_id = callback.data.removeprefix("sp_").removeprefix("play_")
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

    status_msg = await callback.message.reply("⏳ در حال دانلود قطعه از اسپاتیفای با بالاترین کیفیت (320kbps)...")
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        from bot.services.spotify import get_spotify_track_metadata
        meta = await asyncio.to_thread(get_spotify_track_metadata, f"https://open.spotify.com/track/{track_id}")
        if not meta or not meta.title:
            meta = SpotifyTrackMetadata(title="Music Track", artist="Artist", duration=0, cover_url=None, track_id=track_id)

        spot_track = await download_spotify_track_meta(meta)
        caption = f"🎵 <b>{html.escape(spot_track.title)}</b>\n👤 {html.escape(spot_track.artist)}\n\n🤖 دانلود شده توسط ربات"

        thumb_input = FSInputFile(spot_track.thumbnail_path) if spot_track.thumbnail_path and spot_track.thumbnail_path.exists() else None
        sent_msg = await callback.message.reply_audio(
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

