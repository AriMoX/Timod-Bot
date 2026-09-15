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

from bot.config import DOWNLOADS_DIR
from bot.services.music_search import search_music
from bot.services.cache import get_cached_audio, save_cached_audio
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="inline_router")


# -------------------------------------------------------------
# 0. Helper: Download & Cache Audio on Telegram
# -------------------------------------------------------------
async def get_or_download_cached_audio(
    bot: Bot,
    chat_id: int,
    track,
    bot_username: str,
) -> str | None:
    """Ensure track is cached on Telegram servers and return its file_id."""
    # 1. Check local SQLite cache
    cached = get_cached_audio(track.id)
    if cached and cached.get("file_id"):
        return cached["file_id"]

    # 2. Download audio file from YouTube
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "cache_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        },
    }

    track_path = None
    try:
        url = f"https://www.youtube.com/watch?v={track.id}"

        def _dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fn = ydl.prepare_filename(info)
                return Path(fn)

        track_path = await asyncio.to_thread(_dl)
        if not track_path or not track_path.exists():
            matches = list(DOWNLOADS_DIR.glob(f"cache_{track.id}.*"))
            if matches:
                track_path = matches[0]
            else:
                return None

        # 3. Upload to Telegram silently to get a permanent file_id
        safe_caption = f"🎵 <b>{html.escape(track.title)}</b>\n👤 {html.escape(track.artist)}\n\n🤖 @{bot_username}"
        sent_msg = await bot.send_audio(
            chat_id=chat_id,
            audio=FSInputFile(track_path),
            title=track.title,
            performer=track.artist,
            duration=track.duration if track.duration > 0 else None,
            caption=safe_caption,
            parse_mode="HTML",
            disable_notification=True,
        )

        if sent_msg.audio and sent_msg.audio.file_id:
            file_id = sent_msg.audio.file_id
            save_cached_audio(
                track_id=track.id,
                file_id=file_id,
                title=track.title,
                artist=track.artist,
                duration=track.duration,
            )
            return file_id

    except Exception as e:
        logger.warning("Could not auto-cache track %s to chat %s: %s", track.id, chat_id, e)
        return None
    finally:
        if track_path:
            safe_remove(track_path)

    return None


# -------------------------------------------------------------
# 1. Inline Query Handler (Called in any chat: @Timod27_Bot <query>)
# -------------------------------------------------------------
@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    query = inline_query.query.strip()
    me = await inline_query.bot.get_me()
    bot_username = me.username or "Timod27_Bot"
    user_id = inline_query.from_user.id

    logger.info("Inline search by user %s: '%s'", user_id, query)

    # If user hasn't typed a search term yet, show helpful placeholder
    if not query:
        prompt_item = InlineQueryResultArticle(
            id="prompt",
            title="🔍 نام آهنگ یا خواننده را بنویسید...",
            description="مثال: @Timod27_Bot vinak bi setare",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "🎵 <b>راهنمای جستجوی اینلاین موزیک:</b>\n\n"
                    f"برای اشتراک‌گذاری آهنگ در این چت، نام آهنگ را پس از آیدی ربات بنویسید:\n"
                    f"<code>@{bot_username} نام آهنگ یا خواننده</code>"
                ),
                parse_mode="HTML",
            ),
        )
        await inline_query.answer(results=[prompt_item], cache_time=1, is_personal=True)
        return

    # Fast flat music search (< 1s)
    tracks = await search_music(query, limit=5)

    if not tracks:
        no_result = InlineQueryResultArticle(
            id="no_result",
            title="❌ نتیجه‌ای یافت نشد",
            description=f"برای عبارت '{query}' آهنگی پیدا نشد.",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ متأسفانه برای عبارت <b>{html.escape(query)}</b> آهنگی یافت نشد.",
                parse_mode="HTML",
            ),
        )
        await inline_query.answer(results=[no_result], cache_time=5, is_personal=True)
        return

    results = []
    cached_tracks = []
    uncached_tracks = []

    for track in tracks:
        c = get_cached_audio(track.id)
        if c:
            cached_tracks.append((track, c["file_id"]))
        else:
            uncached_tracks.append(track)

    # If the top match is uncached, download & cache it on Telegram immediately!
    if uncached_tracks and len(query) >= 3:
        top_track = uncached_tracks[0]
        file_id = await get_or_download_cached_audio(
            bot=inline_query.bot,
            chat_id=user_id,
            track=top_track,
            bot_username=bot_username,
        )
        if file_id:
            cached_tracks.insert(0, (top_track, file_id))
            uncached_tracks.pop(0)

    # 1. Add all Telegram-cached audio results (plays natively in chat, full file size!)
    for track, file_id in cached_tracks:
        safe_title = html.escape(track.title)
        safe_artist = html.escape(track.artist)
        caption = f"🎵 <b>{safe_title}</b>\n👤 {safe_artist}\n\n🤖 @{bot_username}"
        results.append(
            InlineQueryResultCachedAudio(
                id=f"cached_{track.id}",
                audio_file_id=file_id,
                caption=caption,
                parse_mode="HTML",
            )
        )

    # 2. For any remaining uncached tracks, provide deep link card
    for track in uncached_tracks[:3]:
        safe_title = html.escape(track.title)
        safe_artist = html.escape(track.artist)
        minutes = track.duration // 60
        seconds = track.duration % 60
        dur_str = f"{minutes:02d}:{seconds:02d}" if track.duration > 0 else "--:--"

        dl_url = f"https://t.me/{bot_username}?start=dl_{track.id}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📥 دریافت این قطعه در ربات",
                        url=dl_url,
                    )
                ]
            ]
        )
        results.append(
            InlineQueryResultArticle(
                id=f"art_{track.id}",
                title=track.title,
                description=f"👤 {track.artist} | ⏱ {dur_str}",
                thumbnail_url=track.thumbnail,
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"🎵 <b>{safe_title}</b>\n"
                        f"👤 <b>هنرمند:</b> {safe_artist}\n"
                        f"⏱ <b>مدت زمان:</b> {dur_str}\n\n"
                        f"👇 برای دریافت و کش این قطعه روی دکمه زیر بزنید:"
                    ),
                    parse_mode="HTML",
                ),
                reply_markup=keyboard,
            )
        )

    try:
        await inline_query.answer(results=results, cache_time=300, is_personal=False)
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

    status_msg = await message.reply(f"🔍 در حال جستجوی موزیک برای **'{cleaned_query}'**...")
    tracks = await search_music(cleaned_query, limit=5, with_audio_url=False)

    if not tracks:
        await status_msg.edit_text(f"❌ هیچ آهنگی برای **'{cleaned_query}'** یافت نشد.")
        return

    # Build interactive inline keyboard list
    buttons = []
    text_lines = [f"🎵 **نتایج جستجو برای:** `{cleaned_query}`\n", "برای دانلود هر قطعه، روی دکمه زیر کلیک کنید:\n"]

    for idx, track in enumerate(tracks, start=1):
        minutes = track.duration // 60
        seconds = track.duration % 60
        dur = f"{minutes:02d}:{seconds:02d}" if track.duration > 0 else ""
        text_lines.append(f"{idx}. **{track.title}** - {track.artist} `({dur})`")

        # Telegram callback data max 64 bytes
        buttons.append([
            InlineKeyboardButton(
                text=f"📥 {idx}. {track.title[:30]}",
                callback_data=f"play_{track.id}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text("\n".join(text_lines), reply_markup=keyboard, parse_mode="Markdown")


# -------------------------------------------------------------
# 3. Callback Query Handler (When user clicks a song from search list)
# -------------------------------------------------------------
@router.callback_query(F.data.startswith("play_"))
async def handle_play_callback(callback: CallbackQuery):
    track_id = callback.data.removeprefix("play_")
    await callback.answer("⏳ در حال دانلود و آماده‌سازی فایل صوتی...")

    # Check cache
    cached = get_cached_audio(track_id)
    if cached:
        await callback.message.reply_audio(
            audio=cached["file_id"],
            title=cached["title"],
            performer=cached["artist"],
            duration=cached["duration"],
            caption=f"🎵 **{cached['title']}**\n👤 {cached['artist']}\n\n🤖 دانلود سریع از آرشیو",
            parse_mode="Markdown",
        )
        return

    # Download from YouTube
    status_msg = await callback.message.reply("⏳ در حال دریافت قطعه صوتی...")
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    track_path = None
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "ffmpeg_location": ffmpeg_exe,
            "outtmpl": str(DOWNLOADS_DIR / "cb_%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        url = f"https://www.youtube.com/watch?v={track_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise ValueError("Could not extract track.")

            filename = ydl.prepare_filename(info)
            track_path = Path(filename)

            if not track_path.exists():
                matches = list(DOWNLOADS_DIR.glob(f"cb_{track_id}.*"))
                if matches:
                    track_path = matches[0]

            raw_title = info.get("title") or "Music Track"
            raw_uploader = info.get("uploader") or info.get("channel") or "Artist"

            if " - " in raw_title:
                parts = raw_title.split(" - ", 1)
                artist_raw = parts[0].strip()
                title_clean = parts[1].strip()
            else:
                artist_raw = raw_uploader
                title_clean = raw_title

            # Replace commas with " & "
            artist_parts = [p.strip() for p in artist_raw.split(",") if p.strip()]
            artist_clean = " & ".join(artist_parts) if artist_parts else artist_raw

            duration = int(info.get("duration") or 0)

            sent_msg = await callback.message.reply_audio(
                audio=FSInputFile(track_path),
                title=title_clean,
                performer=artist_clean,
                duration=duration,
                caption=f"🎵 **{title_clean}**\n👤 **هنرمند:** {artist_clean}\n\n🤖 دانلود شده از ربات",
                parse_mode="Markdown",
            )
            await status_msg.delete()

            # Cache the file_id
            if sent_msg.audio and sent_msg.audio.file_id:
                save_cached_audio(
                    track_id=track_id,
                    file_id=sent_msg.audio.file_id,
                    title=title_clean,
                    artist=artist_clean,
                    duration=duration,
                )

    except Exception as e:
        logger.exception("Error in callback download: %s", e)
        await status_msg.edit_text("❌ خطا در آماده‌سازی قطعه صوتی.")
    finally:
        if track_path:
            safe_remove(track_path)
