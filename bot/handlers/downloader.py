import asyncio
import re
import html
import logging
from aiogram import Router, F
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.enums import ChatAction

from bot.services.soundcloud import download_soundcloud
from bot.services.twitter import download_twitter
from bot.services.instagram import download_instagram, InstagramLoginRequiredError
from bot.services.youtube import download_youtube, YouTubeBotDetectionError
from bot.services.tiktok import download_tiktok
from bot.services.pinterest import download_pinterest
from bot.services.spotify import (
    download_spotify,
    get_spotify_album,
    download_spotify_track_meta,
    resolve_spotify_url,
)
from bot.services.cache import get_cached_audio, save_cached_audio
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="downloader_router")


def _format_artists(artists: str) -> str:
    """Replaces commas between multiple artists with ' & '."""
    if not artists:
        return artists
    parts = [p.strip() for p in artists.split(",") if p.strip()]
    return " & ".join(parts) if parts else artists

# Regex to match SoundCloud links (full and short links, with or without query params)
SOUNDCLOUD_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:soundcloud\.com/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+|on\.soundcloud\.com/[A-Za-z0-9_\-]+)(?:\?[^\s]+)?)"
)

# Regex to match Twitter / X links
TWITTER_REGEX = re.compile(
    r"(https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/status/[0-9]+(?:\?[^\s]+)?)"
)

# Regex to match YouTube links (watch, shorts, youtu.be)
YOUTUBE_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=[A-Za-z0-9_\-]+|shorts/[A-Za-z0-9_\-]+)|youtu\.be/[A-Za-z0-9_\-]+)(?:\?[^\s]+)?)"
)

# Regex to match TikTok links (vm.tiktok, vt.tiktok, m.tiktok, web)
TIKTOK_REGEX = re.compile(
    r"(https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/(?:@[A-Za-z0-9_\.]+/video/[0-9]+|[A-Za-z0-9_\-]+|v/[0-9]+\.html)(?:\?[^\s]+)?)"
)
# Regex to match Spotify links (open.spotify.com, spotify.link)
SPOTIFY_REGEX = re.compile(
    r"(https?://(?:open\.spotify\.com/(?:track|album|playlist)/[A-Za-z0-9]+|spotify\.link/[A-Za-z0-9]+)(?:\?[^\s]+)?)"
)
# Regex to match Instagram links (reels, posts, tv, share, ig.me)
INSTAGRAM_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com|ig\.me)/[^\s]+)"
)
# Regex to match Pinterest links (pin.it, pinterest.com/pin/...)
PINTEREST_REGEX = re.compile(
    r"(https?://(?:[a-zA-Z0-9_\.]+\.)?pinterest\.[a-z\.]+/pin/[0-9]+[^\s]*|https?://pin\.it/[A-Za-z0-9_\-]+[^\s]*)"
)


@router.message(F.text.regexp(SOUNDCLOUD_REGEX))
async def handle_soundcloud(message: Message):
    match = SOUNDCLOUD_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود موزیک از ساندکلاد...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    track_path = None
    try:
        track = await download_soundcloud(url)
        track_path = track.file_path

        artist_formatted = _format_artists(track.artist)
        audio_file = FSInputFile(track.file_path)
        caption = (
            f"🎵 **{track.title}**\n"
            f"👤 **هنرمند:** {artist_formatted}\n\n"
            f"🤖 دانلود شده توسط ربات"
        )

        await message.reply_audio(
            audio=audio_file,
            title=track.title,
            performer=artist_formatted,
            duration=track.duration,
            caption=caption,
            parse_mode="Markdown",
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Error processing SoundCloud URL: %s", url)
        await status_msg.edit_text(
            f"❌ متأسفانه در دانلود این قطعه خطایی رخ داد.\n\n"
            f"لطفاً مطمئن شوید که لینک عمومی است و دوباره تلاش کنید."
        )
    finally:
        if track_path:
            safe_remove(track_path)


@router.message(F.text.regexp(TWITTER_REGEX))
async def handle_twitter(message: Message):
    match = TWITTER_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود ویدیو از توییتر (X)...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    video_path = None
    try:
        video = await download_twitter(url)
        video_path = video.file_path

        # Truncate description to fit Telegram caption limits (max 1024 chars)
        clean_title = video.title.strip()
        if len(clean_title) > 800:
            clean_title = clean_title[:797] + "..."

        caption = f"🎬 {clean_title}\n\n👤 @{video.uploader}\n🤖 دانلود شده توسط ربات"

        await message.reply_video(
            video=FSInputFile(video.file_path),
            caption=caption,
            duration=video.duration,
            width=video.width,
            height=video.height,
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Error processing Twitter URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود ویدیو از این توییت خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که توییت حاوی ویدیو/GIF است و لینک عمومی می‌باشد."
        )
    finally:
        if video_path:
            safe_remove(video_path)


async def handle_spotify_album(message: Message, url: str):
    status_msg = await message.reply("⏳ در حال دریافت لیست ترک‌های آلبوم / پلی‌لیست از اسپاتیفای...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        album = await get_spotify_album(url)
        if not album or not album.tracks:
            await status_msg.edit_text("❌ متأسفانه هیچ قطعه‌ای در این آلبوم یا پلی‌لیست یافت نشد.")
            return

        total_tracks = len(album.tracks)
        limit = min(total_tracks, 25)
        is_playlist = album.is_playlist
        type_str = "پلی‌لیست" if is_playlist else "آلبوم"

        album_title_esc = html.escape(album.name)
        artist_esc = html.escape(_format_artists(album.artist))

        await status_msg.edit_text(
            f"🎶 <b>{album_title_esc}</b>\n"
            f"👤 <b>هنرمند:</b> {artist_esc}\n"
            f"📦 <b>تعداد کل ترک‌ها:</b> {total_tracks} قطعه\n\n"
            f"⏳ در حال دانلود و ارسال {limit} ترک اول (لطفاً صبور باشید)...",
            parse_mode="HTML",
        )

        success_count = 0
        for idx, track_meta in enumerate(album.tracks[:limit], start=1):
            track_id = track_meta.track_id
            cache_key = f"spot_{track_id}" if track_id else None
            artist_formatted = _format_artists(track_meta.artist or album.artist)
            title_esc = html.escape(str(track_meta.title))
            t_artist_esc = html.escape(str(artist_formatted))
            caption = (
                f"🎵 <b>{title_esc}</b>\n"
                f"👤 <b>هنرمند:</b> {t_artist_esc}\n"
                f"⚡ <b>کیفیت:</b> 320kbps (Original HQ)\n"
                f"💿 <b>{type_str}:</b> {album_title_esc} ({idx}/{limit})\n\n"
                f"🤖 دانلود شده از اسپاتیفای"
            )

            # 1. Fast Cache Check
            if cache_key:
                cached = get_cached_audio(cache_key)
                if cached and cached.get("file_id"):
                    try:
                        await message.reply_audio(
                            audio=cached["file_id"],
                            title=track_meta.title,
                            performer=artist_formatted,
                            duration=cached.get("duration") or track_meta.duration,
                            caption=caption,
                            parse_mode="HTML",
                        )
                        success_count += 1
                        await asyncio.sleep(1.2)
                        continue
                    except Exception as ce:
                        logger.warning("Failed sending cached audio for %s: %s", track_meta.title, ce)

            # 2. Download and send
            track_path = None
            thumb_path = None
            try:
                track = await download_spotify_track_meta(track_meta, fallback_cover=album.cover_url)
                track_path = track.file_path
                thumb_path = track.thumbnail_path

                audio_file = FSInputFile(track.file_path)
                thumb_file = FSInputFile(track.thumbnail_path) if track.thumbnail_path and track.thumbnail_path.exists() else None

                sent_msg = await message.reply_audio(
                    audio=audio_file,
                    title=track.title,
                    performer=artist_formatted,
                    duration=track.duration,
                    thumbnail=thumb_file,
                    caption=caption,
                    parse_mode="HTML",
                )
                success_count += 1

                if sent_msg and sent_msg.audio and cache_key:
                    save_cached_audio(
                        track_id=cache_key,
                        file_id=sent_msg.audio.file_id,
                        title=track.title,
                        artist=artist_formatted,
                        duration=track.duration,
                    )
                await asyncio.sleep(1.5)
            except Exception as te:
                logger.warning("Failed downloading track %s from album: %s", track_meta.title, te)
            finally:
                safe_remove(track_path, thumb_path)

        if success_count > 0:
            await status_msg.edit_text(
                f"✅ <b>دانلود {type_str} به پایان رسید!</b>\n\n"
                f"🎶 <b>{album_title_esc}</b>\n"
                f"📊 <b>{success_count}</b> از <b>{limit}</b> ترک با موفقیت ارسال شد.",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                f"❌ متأسفانه امکان دانلود ترک‌های این {type_str} وجود نداشت.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.exception("Error processing Spotify album URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در پردازش این آلبوم یا پلی‌لیست اسپاتیفای خطایی رخ داد."
        )


@router.message(F.text.regexp(SPOTIFY_REGEX))
async def handle_spotify(message: Message):
    match = SPOTIFY_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    resolved_url = resolve_spotify_url(url)

    # Handle Album or Playlist
    if "/album/" in resolved_url or "/playlist/" in resolved_url:
        await handle_spotify_album(message, resolved_url)
        return

    # 1. Fast Cache Check: If already downloaded previously, send instantly (0.1s)
    track_id_match = re.search(r"/track/([A-Za-z0-9]+)", resolved_url)
    cache_key = f"spot_{track_id_match.group(1)}" if track_id_match else None

    if cache_key:
        cached = get_cached_audio(cache_key)
        if cached and cached.get("file_id"):
            artist_formatted = _format_artists(cached.get("artist") or "Unknown")
            title_esc = html.escape(str(cached.get("title") or "Track"))
            artist_esc = html.escape(str(artist_formatted))
            caption = (
                f"🎵 <b>{title_esc}</b>\n"
                f"👤 <b>هنرمند:</b> {artist_esc}\n\n"
                f"🤖 دانلود شده از اسپاتیفای (ارسال آنی از کَش)"
            )
            await message.reply_audio(
                audio=cached["file_id"],
                title=cached.get("title") or "Track",
                performer=artist_formatted,
                duration=cached.get("duration") or 0,
                caption=caption,
                parse_mode="HTML",
            )
            return

    status_msg = await message.reply("⏳ در حال دریافت مشخصات قطعه از اسپاتیفای و دانلود صوت...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    track_path = None
    thumb_path = None
    try:
        track = await download_spotify(resolved_url)
        track_path = track.file_path
        thumb_path = track.thumbnail_path

        artist_formatted = _format_artists(track.artist)
        audio_file = FSInputFile(track.file_path)
        thumb_file = FSInputFile(track.thumbnail_path) if track.thumbnail_path and track.thumbnail_path.exists() else None

        title_esc = html.escape(str(track.title))
        artist_esc = html.escape(str(artist_formatted))
        caption = (
            f"🎵 <b>{title_esc}</b>\n"
            f"👤 <b>هنرمند:</b> {artist_esc}\n"
            f"⚡ <b>کیفیت:</b> 320kbps (Original HQ)\n\n"
            f"🤖 دانلود شده از اسپاتیفای"
        )

        sent_msg = await message.reply_audio(
            audio=audio_file,
            title=track.title,
            performer=artist_formatted,
            duration=track.duration,
            thumbnail=thumb_file,
            caption=caption,
            parse_mode="HTML",
        )
        await status_msg.delete()

        # Cache Telegram file_id for future instant responses
        track_key = f"spot_{track.track_id}" if track.track_id else cache_key
        if sent_msg and sent_msg.audio and track_key:
            save_cached_audio(
                track_id=track_key,
                file_id=sent_msg.audio.file_id,
                title=track.title,
                artist=artist_formatted,
                duration=track.duration,
            )

    except Exception as e:
        logger.exception("Error processing Spotify URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود این قطعه از اسپاتیفای خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که لینک ترک معتبر است و دوباره امتحان کنید."
        )
    finally:
        safe_remove(track_path, thumb_path)


@router.message(F.text.regexp(INSTAGRAM_REGEX))
async def handle_instagram(message: Message):
    match = INSTAGRAM_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود از اینستاگرام...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    downloaded_items = []
    try:
        media = await download_instagram(url)
        downloaded_items = media.items

        if not downloaded_items:
            raise ValueError("هیچ فایل مدیا در این لینک یافت نشد.")

        clean_title = media.title.strip() if media.title else "اینستاگرام"
        if len(clean_title) > 600:
            clean_title = clean_title[:597] + "..."

        uploader_str = f"👤 @{media.uploader}\n" if media.uploader and media.uploader != "Instagram" else ""
        icon = "🎬" if downloaded_items[0].media_type == "video" else "🖼"
        caption = f"{icon} {clean_title}\n\n{uploader_str}🤖 دانلود شده توسط ربات"

        # 1. Single Media (Photo or Video)
        if len(downloaded_items) == 1:
            item = downloaded_items[0]
            if item.media_type == "video":
                await message.reply_video(
                    video=FSInputFile(item.file_path),
                    caption=caption,
                    duration=item.duration,
                    width=item.width,
                    height=item.height,
                )
            else:
                await message.reply_photo(
                    photo=FSInputFile(item.file_path),
                    caption=caption,
                )

        # 2. Multiple Media (Carousel / Album)
        else:
            chunk_size = 10
            for chunk_idx in range(0, len(downloaded_items), chunk_size):
                chunk = downloaded_items[chunk_idx : chunk_idx + chunk_size]
                media_group = []
                for idx, item in enumerate(chunk):
                    # Only the first media in the group gets the caption
                    item_caption = caption if (chunk_idx == 0 and idx == 0) else None
                    if item.media_type == "video":
                        media_group.append(
                            InputMediaVideo(
                                media=FSInputFile(item.file_path),
                                caption=item_caption,
                                duration=item.duration,
                                width=item.width,
                                height=item.height,
                            )
                        )
                    else:
                        media_group.append(
                            InputMediaPhoto(
                                media=FSInputFile(item.file_path),
                                caption=item_caption,
                            )
                        )
                await message.reply_media_group(media=media_group)

        await status_msg.delete()

    except InstagramLoginRequiredError:
        logger.warning("Instagram login required for URL: %s", url)
        await status_msg.edit_text(
            "⚠️ **اینستاگرام دسترسی به این مدیا را محدود کرده است.**\n\n"
            "اینستاگرام برای دانلود برخی ریلزها و پست‌ها نیاز به نشست کاربری (Cookie) دارد.\n\n"
            "💡 **راهکار دائمی:**\n"
            "می‌توانید مقدار `INSTAGRAM_SESSIONID` یا یک فایل `cookies.txt` را در تنظیمات ربات قرار دهید تا تمامی لینک‌ها بدون وقفه دانلود شوند."
        )
    except Exception as e:
        logger.exception("Error processing Instagram URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود این لینک اینستاگرام خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که صفحه/پست عمومی (Public) است و دوباره امتحان کنید."
        )
    finally:
        for item in downloaded_items:
            safe_remove(item.file_path)


@router.message(F.text.regexp(YOUTUBE_REGEX))
async def handle_youtube(message: Message):
    match = YOUTUBE_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود ویدیو از یوتیوب...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    video_path = None
    try:
        video = await download_youtube(url)
        video_path = video.file_path

        clean_title = html.escape(video.title.strip())
        if len(clean_title) > 800:
            clean_title = clean_title[:797] + "..."

        clean_uploader = html.escape(video.uploader)
        caption = f"🎬 <b>{clean_title}</b>\n\n📺 {clean_uploader}\n🤖 دانلود شده توسط ربات"

        await message.reply_video(
            video=FSInputFile(video.file_path),
            caption=caption,
            duration=video.duration,
            width=video.width,
            height=video.height,
            parse_mode="HTML",
        )
        await status_msg.delete()

    except YouTubeBotDetectionError:
        logger.warning("YouTube bot protection / login required for URL: %s", url)
        await status_msg.edit_text(
            "⚠️ <b>یوتیوب دسترسی مستقیم سرور به این مدیا را محدود کرده است.</b>\n\n"
            "گوگل/یوتیوب برای دانلود ویدیو از سرورهای ابری نیاز به کوکی مرورگر (Cookie) دارد.\n\n"
            "💡 <b>راهکار دائمی و آسان:</b>\n"
            "می‌توانید یک فایل <code>cookies.txt</code> (صادر شده از مرورگر) در تنظیمات ربات قرار دهید یا مقدار <code>YOUTUBE_COOKIES_TEXT</code> را در متغیرهای سرور قرار دهید تا تمام ویدیوها و شورت‌های یوتیوب با حداکثر سرعت دانلود شوند.",
            parse_mode="HTML",
        )
    except ValueError as ve:
        # Handles 50MB file size limit or other expected value errors
        logger.warning("YouTube download value error for %s: %s", url, ve)
        await status_msg.edit_text(f"⚠️ {html.escape(str(ve))}", parse_mode="HTML")
    except Exception as e:
        logger.exception("Error processing YouTube URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود این ویدیوی یوتیوب خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که ویدیو عمومی است و دوباره امتحان کنید."
        )
    finally:
        if video_path:
            safe_remove(video_path)


@router.message(F.text.regexp(TIKTOK_REGEX))
async def handle_tiktok(message: Message):
    match = TIKTOK_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود ویدیو از تیک‌تاک...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    video_path = None
    try:
        video = await download_tiktok(url)
        video_path = video.file_path

        clean_title = video.title.strip()
        if len(clean_title) > 800:
            clean_title = clean_title[:797] + "..."

        caption = f"🎬 {clean_title}\n\n👤 @{video.uploader}\n🤖 دانلود شده توسط ربات (بدون واترمارک)"

        await message.reply_video(
            video=FSInputFile(video.file_path),
            caption=caption,
            duration=video.duration,
            width=video.width,
            height=video.height,
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Error processing TikTok URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود این ویدیوی تیک‌تاک خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که ویدیو عمومی است و دوباره امتحان کنید."
        )
    finally:
        if video_path:
            safe_remove(video_path)


@router.message(F.text.regexp(PINTEREST_REGEX))
async def handle_pinterest(message: Message):
    match = PINTEREST_REGEX.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.reply("⏳ در حال دریافت و دانلود از پینترست...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    media_path = None
    try:
        media = await download_pinterest(url)
        media_path = media.file_path

        clean_title = media.title.strip() if media.title else ""
        if not clean_title or clean_title.lower() == "pinterest media":
            clean_title = "مدیا پینترست"
        elif len(clean_title) > 800:
            clean_title = clean_title[:797] + "..."

        uploader_str = f"\n👤 {media.uploader}" if media.uploader and media.uploader != "Pinterest" else ""
        caption = f"📌 {clean_title}{uploader_str}\n\n🤖 دانلود شده توسط ربات"

        if media.media_type == "video":
            await message.reply_video(
                video=FSInputFile(media.file_path),
                caption=caption,
                duration=media.duration,
                width=media.width,
                height=media.height,
            )
        else:
            await message.reply_photo(
                photo=FSInputFile(media.file_path),
                caption=caption,
            )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Error processing Pinterest URL: %s", url)
        await status_msg.edit_text(
            "❌ متأسفانه در دانلود این مدیا از پینترست خطایی رخ داد.\n\n"
            "لطفاً مطمئن شوید که پین معتبر و عمومی است و دوباره امتحان کنید."
        )
    finally:
        if media_path:
            safe_remove(media_path)
