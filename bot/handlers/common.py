import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, FSInputFile, CallbackQuery
from aiogram.enums import ChatAction
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR, ADMIN_ID
from bot.services.cache import get_cached_audio, save_cached_audio
from bot.services.channel_sub import is_user_subscribed
from bot.services.user_storage import get_all_users
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="common_router")

START_TEXT = (
    "👋 **سلام! به ربات همه‌کاره خوش آمدید.**\n\n"
    "این ربات یک پکیج کامل از ابزارهای رسانه‌ای را برای شما فراهم کرده است:\n\n"
    "🔍 **جستجوی اینلاین موزیک در چت‌ها (جدید 🌟):**\n"
    "• در هر چت یا گروهی کافیست تایپ کنید: `@Timod27_Bot نام آهنگ` تا لیست قطعات ظاهر شده و با یک کلیک ارسال شوند!\n\n"
    "🎥 **تبدیل ویدیو به ویدیو مسیج:**\n"
    "• هر ویدیویی بفرستی، تبدیل به **ویدیو مسیج دایره‌ای** می‌کنه!\n\n"
    "📥 **دانلودر شبکه‌های اجتماعی و موسیقی:**\n"
    "• **اسپاتیفای (Spotify)** - دانلود موزیک با کاور و متادیتا 🎧\n"
    "• **ساندکلاد (SoundCloud)** - دانلود موزیک با بالاترین کیفیت 🎵\n"
    "• **اینستاگرام (Instagram)** - دانلود ریلز و پست‌های ویدیویی 📸\n"
    "• **تیک‌تاک (TikTok)** - دانلود ویدیوهای بدون واترمارک 📱\n"
    "• **یوتیوب (YouTube & Shorts)** - دانلود ویدیو و شورتس 🔴\n"
    "• **پینترست (Pinterest)** - دانلود ویدیو و تصاویر اورجینال 📌\n"
    "• **توییتر (Twitter / X)** - دانلود ویدیو و GIF با کیفیت اصلی 🐦\n\n"
    "🚀 همین الان یک ویدیو یا لینک دلخواهت رو ارسال کن یا نام آهنگ رو به صورت اینلاین جستجو کن!"
)

HELP_TEXT = (
    "📖 **راهنمای جامع استفاده:**\n\n"
    "۱. **جستجوی اینلاین در چت‌ها:** در هر گروه یا پی‌وی بنویسید:\n"
    "   `@Timod27_Bot eminem without me`\n"
    "   سپس روی آهنگ دلخواه کلیک کنید تا ارسال شود.\n"
    "۲. **ویدیو مسیج:** یک فایل ویدیویی بفرستید تا نسخه دایره‌ای تحویل بگیرید.\n"
    "۳. **اسپاتیفای:** لینک ترک اسپاتیفای را ارسال کنید.\n"
    "۴. **ساندکلاد:** لینک قطعه ساندکلاد را بفرستید.\n"
    "۵. **اینستاگرام:** لینک ریلز یا پست اینستاگرام را ارسال کنید.\n"
    "۶. **تیک‌تاک:** لینک ویدیوی تیک‌تاک را ارسال کنید (بدون واترمارک).\n"
    "۷. **یوتیوب:** لینک ویدیو یا Shorts یوتیوب را بفرستید.\n"
    "۸. **پینترست:** لینک پین یا لینک کوتاه `pin.it` را ارسال کنید.\n"
    "۹. **توییتر (X):** لینک توییت حاوی ویدیو یا گیف را بفرستید."
)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep_link(message: Message, command: CommandObject):
    """Handle deep link from inline results: /start dl_<track_id>"""
    args = command.args or ""
    if not args.startswith("dl_"):
        await message.answer(START_TEXT, parse_mode="Markdown")
        return

    track_id = args.removeprefix("dl_")

    # 1. Check if cached already in Telegram
    cached = get_cached_audio(track_id)
    if cached:
        await message.reply_audio(
            audio=cached["file_id"],
            title=cached["title"],
            performer=cached["artist"],
            duration=cached["duration"],
            caption=f"🎵 **{cached['title']}**\n👤 {cached['artist']}\n\n🤖 دانلود شده از آرشیو سریع",
            parse_mode="Markdown",
        )
        return

    # 2. Download audio if not cached yet
    status_msg = await message.reply("⏳ در حال دریافت و آماده‌سازی قطعه صوتی...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    track_path = None
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "ffmpeg_location": ffmpeg_exe,
            "outtmpl": str(DOWNLOADS_DIR / "inline_%(id)s.%(ext)s"),
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
                matches = list(DOWNLOADS_DIR.glob(f"inline_{track_id}.*"))
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

            sent_msg = await message.reply_audio(
                audio=FSInputFile(track_path),
                title=title_clean,
                performer=artist_clean,
                duration=duration,
                caption=f"🎵 **{title_clean}**\n👤 **هنرمند:** {artist_clean}\n\n🤖 دانلود شده از ربات",
                parse_mode="Markdown",
            )
            await status_msg.delete()

            # Cache the file_id so future inline queries send it instantly
            if sent_msg.audio and sent_msg.audio.file_id:
                save_cached_audio(
                    track_id=track_id,
                    file_id=sent_msg.audio.file_id,
                    title=title_clean,
                    artist=artist_clean,
                    duration=duration,
                )

    except Exception as e:
        logger.exception("Error downloading track %s: %s", track_id, e)
        await status_msg.edit_text("❌ متأسفانه در آماده‌سازی این قطعه خطایی رخ داد.")
    finally:
        if track_path:
            safe_remove(track_path)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(START_TEXT, parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(Command("setinline"))
async def cmd_setinline_info(message: Message):
    guide = (
        "⚠️ **توجه مهم:**\n\n"
        "دستور `/setinline` را نباید در چت این ربات بفرستید!\n"
        "برای فعال‌سازی جستجوی اینلاین در تلگرام، باید به ربات رسمی تلگرام یعنی @BotFather بروید:\n\n"
        "۱️⃣ وارد @BotFather شوید.\n"
        "۲️⃣ دستور `/setinline` را ارسال کنید.\n"
        "۳️⃣ ربات `@Timod27_Bot` را انتخاب کنید.\n"
        "۴️⃣ یک متن کوتاه مثل `جستجوی موزیک...` یا `Search music...` تایپ کرده و بفرستید.\n"
        "*(دقت کنید: روی /empty نزنید چون غیرفعال می‌شود!)*\n\n"
        "✅ پس از دریافت پیام `Success!` از BotFather، قابلیت سرچ اینلاین در تمام چت‌ها و گروه‌ها فعال خواهد شد."
    )
    await message.answer(guide, parse_mode="Markdown")


@router.callback_query(F.data == "verify_subscription")
async def handle_verify_subscription(callback: CallbackQuery):
    """Verify if user joined the required channel and update UI."""
    user_id = callback.from_user.id
    subscribed = await is_user_subscribed(callback.bot, user_id)
    if subscribed:
        await callback.answer("✅ عضویت شما تایید شد! خوش آمدید.", show_alert=True)
        try:
            await callback.message.edit_text(START_TEXT, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(START_TEXT, parse_mode="Markdown")
    else:
        await callback.answer(
            "❌ شما هنوز در کانال @Timod27 عضو نشده‌اید!\n\n"
            "لطفاً ابتدا روی دکمه «عضویت در کانال» کلیک کرده و عضو شوید، سپس دوباره دکمه «تایید عضویت» را بزنید.",
            show_alert=True,
        )


@router.message(Command("users"))
@router.message(Command("stats"))
@router.message(Command("admin"))
async def cmd_users_list(message: Message):
    """Show list of all users who started and used the bot with Shamsi date & Tehran time (Admin only)."""
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔️ این دستور فقط مخصوص مالک ربات است.")
        return

    from bot.services.user_storage import get_all_users, format_users_report_chunks, to_shamsi_tehran
    users = get_all_users()
    chunks = format_users_report_chunks(users, ADMIN_ID)

    for chunk in chunks:
        await message.reply(chunk, parse_mode="HTML")

    # If there are users, also send a convenient downloadable text file
    if users:
        from aiogram.types import BufferedInputFile
        txt_lines = [
            f"گزارش کامل کاربران ربات @Timod27_Bot",
            f"تعداد کل کاربران: {len(users)} نفر",
            "=" * 50,
        ]
        for idx, u in enumerate(users, start=1):
            name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "بدون نام"
            uname = f"@{u.get('username')}" if u.get('username') else "ندارد"
            fs = to_shamsi_tehran(u.get('first_seen'))
            ls = to_shamsi_tehran(u.get('last_seen'))
            txt_lines.append(f"{idx}. {name} | آیدی: {u.get('user_id')} | یوزرنیم: {uname}")
            txt_lines.append(f"   اولین استارت: {fs}")
            txt_lines.append(f"   آخرین فعالیت: {ls}")
            txt_lines.append("-" * 40)

        file_bytes = "\n".join(txt_lines).encode("utf-8")
        doc = BufferedInputFile(file_bytes, filename="users_list_shamsi.txt")
        await message.reply_document(doc, caption="📁 فایل متنی کامل مشخصات کاربران ربات (تاریخ شمسی و ساعت تهران)")


@router.message(F.document)
async def handle_admin_document(message: Message):
    """Allow Admin to upload cookies.txt directly to the bot."""
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    doc = message.document
    filename = (doc.file_name or "").lower()
    caption = (message.caption or "").lower()

    if "cookie" in filename or "cookie" in caption or filename.endswith(".txt"):
        status_msg = await message.reply("⏳ در حال دریافت و فعال‌سازی فایل کوکی یوتیوب...")
        dest_path = DOWNLOADS_DIR / "admin_cookies.txt"
        yt_path = DOWNLOADS_DIR / "yt_cookies.txt"
        try:
            await message.bot.download(doc, destination=dest_path)
            from bot.services.youtube import _sanitize_cookies
            content = dest_path.read_text(encoding="utf-8", errors="ignore")
            sanitized = _sanitize_cookies(content)
            dest_path.write_text(sanitized, encoding="utf-8")
            yt_path.write_text(sanitized, encoding="utf-8")

            await status_msg.edit_text(
                "✅ <b>فایل کوکی یوتیوب با موفقیت ذخیره و فعال شد!</b>\n\n"
                "تمامی نشست‌های حساس پاکسازی شده و کوکی جدید با بالاترین اولویت در ربات فعال گردید.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Failed to process uploaded cookie file")
            await status_msg.edit_text(f"❌ خطا در پردازش فایل کوکی: {e}")




