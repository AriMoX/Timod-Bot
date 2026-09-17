import re
import html
import uuid
import logging
import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.enums import ChatAction

from bot.states import SearchStates
from bot.keyboards import MAIN_MENU_KEYBOARD, CANCEL_KEYBOARD
from bot.services.film2media import (
    search_f2m,
    get_movie_details,
    F2MLinkStore,
)
from bot.services.spotify import (
    search_spotify,
    get_or_prepare_spotify_mp3,
    get_cached_track_meta,
    SpotifyTrackMetadata,
)
from bot.services.cache import get_cached_audio, save_cached_audio
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="movies_router")

# Temporary store for unassigned text queries awaiting user choice (movie vs music)
_PENDING_QUERIES: dict[str, str] = {}
# Temporary store for music search track objects
_MUSIC_CACHE: dict[str, SpotifyTrackMetadata] = {}


def _is_url(text: str) -> bool:
    return bool(re.search(r"https?://", text, re.IGNORECASE))


# -------------------------------------------------------------
# 1. Main Menu Buttons & Commands Triggering FSM States
# -------------------------------------------------------------
@router.message(F.text == "🎬 جستجوی فیلم و سریال")
@router.message(Command("movie"))
@router.message(Command("film"))
@router.message(Command("serial"))
async def start_movie_search(message: Message, state: FSMContext):
    """Start movie search flow: enter FSM state or handle command with argument."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)

    # If user ran /movie <name> directly with an argument
    if len(parts) >= 2 and parts[0].startswith("/") and parts[1].strip():
        query = parts[1].strip()
        await state.clear()
        await _execute_f2m_search(message, query)
        return

    # Enter waiting for movie state
    await state.set_state(SearchStates.waiting_for_movie)
    prompt = (
        "🎬 <b>جستجوی فیلم و سریال (فیلم‌تو‌مدیا):</b>\n\n"
        "لطفاً نام فیلم یا سریال مورد نظر خود را به فارسی یا انگلیسی ارسال کنید:\n"
        "*(به عنوان مثال: <code>Inception</code> یا <code>بتمن</code> یا <code>Slow Horses</code> یا <code>زخم کاری</code>)*\n\n"
        "برای لغو می‌توانید از دکمه «انصراف» زیر استفاده کنید."
    )
    await message.reply(prompt, reply_markup=CANCEL_KEYBOARD, parse_mode="HTML")


@router.message(F.text == "🎵 جستجوی موزیک")
@router.message(Command("music"))
@router.message(Command("song"))
async def start_music_search(message: Message, state: FSMContext):
    """Start music search flow: enter FSM state or handle command with argument."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)

    # If user ran /music <name> directly with an argument
    if len(parts) >= 2 and parts[0].startswith("/") and parts[1].strip():
        query = parts[1].strip()
        await state.clear()
        await _execute_music_search(message, query)
        return

    # Enter waiting for music state
    await state.set_state(SearchStates.waiting_for_music)
    prompt = (
        "🎵 <b>جستجو و دانلود موزیک:</b>\n\n"
        "لطفاً نام آهنگ یا خواننده مورد نظر خود را ارسال کنید:\n"
        "*(به عنوان مثال: <code>Eminem Without Me</code> یا <code>شایع</code> یا <code>The Weeknd</code>)*\n\n"
        "💡 <i>همچنین در هر گروه یا چتی با تایپ <code>@Timod27_Bot نام آهنگ</code> می‌توانید آهنگ‌ها را اینلاین جستجو و ارسال کنید!</i>"
    )
    inline_btn = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 جستجوی اینلاین در چت", switch_inline_query_current_chat=""),
            ]
        ]
    )
    await message.reply(prompt, reply_markup=CANCEL_KEYBOARD, parse_mode="HTML")
    await message.answer("یا روی دکمه زیر بزنید تا پنجره اینلاین باز شود:", reply_markup=inline_btn)


@router.message(F.text == "❌ انصراف / بازگشت به منوی اصلی")
@router.message(Command("cancel"))
async def handle_cancel_search(message: Message, state: FSMContext):
    """Cancel any active search state and return to main menu."""
    await state.clear()
    await message.answer("✅ عملیات لغو شد. منوی اصلی آماده است:", reply_markup=MAIN_MENU_KEYBOARD)


# -------------------------------------------------------------
# 2. State-Specific Input Handlers
# -------------------------------------------------------------
@router.message(SearchStates.waiting_for_movie, F.text & ~F.text.startswith("/"))
async def handle_movie_query_input(message: Message, state: FSMContext):
    """Handle text input when specifically waiting for a movie title."""
    text = message.text.strip()
    if text == "❌ انصراف / بازگشت به منوی اصلی":
        await state.clear()
        await message.answer("✅ به منوی اصلی بازگشتید:", reply_markup=MAIN_MENU_KEYBOARD)
        return

    await state.clear()
    await message.answer("منوی اصلی فعال شد.", reply_markup=MAIN_MENU_KEYBOARD)
    await _execute_f2m_search(message, text)


@router.message(SearchStates.waiting_for_music, F.text & ~F.text.startswith("/"))
async def handle_music_query_input(message: Message, state: FSMContext):
    """Handle text input when specifically waiting for a music title."""
    text = message.text.strip()
    if text == "❌ انصراف / بازگشت به منوی اصلی":
        await state.clear()
        await message.answer("✅ به منوی اصلی بازگشتید:", reply_markup=MAIN_MENU_KEYBOARD)
        return

    await state.clear()
    await message.answer("منوی اصلی فعال شد.", reply_markup=MAIN_MENU_KEYBOARD)
    await _execute_music_search(message, text)


# -------------------------------------------------------------
# 3. Fallback Handler for Raw Text when No State is Active
# -------------------------------------------------------------
@router.message(F.text & ~F.text.startswith("/"))
async def handle_unspecified_text(message: Message):
    """When user sends raw text without selecting a button, ask whether it is Movie or Music."""
    text = (message.text or "").strip()
    if _is_url(text) or len(text) < 2 or len(text) > 100:
        return

    # Store query with unique token
    query_id = uuid.uuid4().hex[:8]
    _PENDING_QUERIES[query_id] = text

    choice_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 جستجو به عنوان فیلم / سریال", callback_data=f"choice_movie:{query_id}"),
                InlineKeyboardButton(text="🎵 جستجو به عنوان موزیک", callback_data=f"choice_music:{query_id}"),
            ],
            [
                InlineKeyboardButton(text="❌ انصراف", callback_data=f"choice_cancel:{query_id}"),
            ]
        ]
    )

    await message.reply(
        f"❓ می‌خواهید عبارت «<b>{html.escape(text)}</b>» را در کدام بخش جستجو کنید؟",
        reply_markup=choice_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("choice_movie:"))
async def handle_choice_movie(callback: CallbackQuery):
    query_id = callback.data.removeprefix("choice_movie:")
    query = _PENDING_QUERIES.pop(query_id, None)
    if not query:
        await callback.answer("⚠️ این درخواست منقضی شده است.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _execute_f2m_search(callback.message, query)


@router.callback_query(F.data.startswith("choice_music:"))
async def handle_choice_music(callback: CallbackQuery):
    query_id = callback.data.removeprefix("choice_music:")
    query = _PENDING_QUERIES.pop(query_id, None)
    if not query:
        await callback.answer("⚠️ این درخواست منقضی شده است.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _execute_music_search(callback.message, query)


@router.callback_query(F.data.startswith("choice_cancel:"))
async def handle_choice_cancel(callback: CallbackQuery):
    query_id = callback.data.removeprefix("choice_cancel:")
    _PENDING_QUERIES.pop(query_id, None)
    await callback.answer("لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        pass


# -------------------------------------------------------------
# 4. Movie Execution Engine (Film2Media)
# -------------------------------------------------------------
async def _execute_f2m_search(message: Message, query: str):
    """Execute search on Film2Media and display results with interactive buttons."""
    status_msg = await message.reply(f"🔎 در حال جستجوی «<b>{html.escape(query)}</b>» در فیلم‌تو‌مدیا...", parse_mode="HTML")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    try:
        results = await search_f2m(query)
        if not results:
            await status_msg.edit_text(
                f"❌ نتیجه‌ای برای «<b>{html.escape(query)}</b>» در سایت فیلم‌تو‌مدیا یافت نشد.\n\n"
                "💡 <i>نکته: نام فیلم را به انگلیسی یا با املای دقیق‌تر جستجو کنید.</i>",
                parse_mode="HTML",
            )
            return

        if len(results) == 1:
            await status_msg.edit_text("⏳ در حال دریافت کیفیت‌ها و لینک‌های دانلود...")
            await _show_movie_details(message, results[0]["url"], status_msg)
            return

        keyboard_buttons = []
        text_lines = [
            f"🎬 <b>نتایج جستجو برای «{html.escape(query)}»:</b>\n",
            "لطفاً اثر مورد نظر خود را از لیست زیر انتخاب کنید:\n",
        ]

        for idx, item in enumerate(results[:8], start=1):
            short_id = F2MLinkStore.save_link(item)
            year_str = f" ({item['year']})" if item.get("year") else ""
            rating_str = f" ⭐️ {item['rating']}" if item.get("rating") else ""
            type_str = "📺" if item.get("is_series") else "🎬"

            btn_text = f"{type_str} {idx}. {item['title']}{year_str}"
            if len(btn_text) > 40:
                btn_text = btn_text[:37] + "..."

            keyboard_buttons.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"f2m_sel:{short_id}")
            ])
            text_lines.append(f"{idx}️⃣ <b>{html.escape(item['title'])}</b>{year_str}{rating_str}")

        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await status_msg.edit_text("\n".join(text_lines), reply_markup=markup, parse_mode="HTML")

    except Exception as e:
        logger.exception("Error executing movie search for %s: %s", query, e)
        await status_msg.edit_text(f"❌ خطا در جستجوی فیلم: {e}")


@router.callback_query(F.data.startswith("f2m_sel:"))
async def handle_movie_selection(callback: CallbackQuery):
    """Handle when user selects a movie from search results."""
    short_id = callback.data.removeprefix("f2m_sel:")
    item = F2MLinkStore.get_link(short_id)

    if not item or not item.get("url"):
        await callback.answer("⚠️ اطلاعات این اثر منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer("⏳ در حال دریافت لینک‌های دانلود...")
    try:
        await callback.message.edit_text("⏳ در حال دریافت کیفیت‌ها، مشخصات و محاسبه حجم فایل‌ها...")
    except Exception:
        pass

    await _show_movie_details(callback.message, item["url"], callback.message)


async def _show_movie_details(message: Message, movie_url: str, edit_msg: Message | None = None):
    """Fetch movie page, parse download links with sizes, and display inline buttons."""
    details = await get_movie_details(movie_url)
    if not details:
        err_text = "❌ متأسفانه در دریافت اطلاعات و لینک‌های دانلود این اثر خطایی رخ داد."
        if edit_msg:
            await edit_msg.edit_text(err_text)
        else:
            await message.reply(err_text)
        return

    title = details.get("title", "فیلم / سریال")
    story = details.get("story", "")
    poster = details.get("poster")
    downloads = details.get("downloads", [])
    is_series = details.get("is_series", False)
    series_seasons = details.get("series_seasons", {})

    caption_lines = [
        f"🎬 <b>{html.escape(title)}</b>\n",
    ]
    if story:
        caption_lines.append(f"📝 <b>خلاصه داستان:</b>\n{html.escape(story)}\n")
    caption_lines.append("━━━━━━━━━━━━━━━━━━━━")
    caption_lines.append("📥 <b>برای دریافت لینک، کیفیت و حجم مورد نظر را انتخاب کنید:</b>")

    caption_text = "\n".join(caption_lines)
    keyboard_rows = []

    if downloads and not is_series:
        for d in downloads:
            short_id = d.get("short_id")
            size_str = f" | {d['size']}" if d.get("size") else ""
            type_icon = "🎧" if "دوبله" in d.get("type", "") else "📝"
            btn_label = f"{type_icon} {d['quality']} ({d['type']}){size_str}"
            if len(btn_label) > 45:
                btn_label = f"{type_icon} {d['quality']}{size_str}"

            keyboard_rows.append([
                InlineKeyboardButton(text=btn_label, callback_data=f"f2m_dl:{short_id}")
            ])

    elif is_series and series_seasons:
        for s_name, q_dict in series_seasons.items():
            for q_tag, ep_list in q_dict.items():
                first_ep = ep_list[0] if ep_list else None
                if first_ep:
                    batch_id = F2MLinkStore.save_link({
                        "title": title,
                        "quality": f"{s_name} - {q_tag}",
                        "encoder": "",
                        "type": "سریال",
                        "url": first_ep["url"],
                        "size": None,
                    })
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            text=f"📁 {s_name} ({q_tag}) - قسمت 1 تا {len(ep_list)}",
                            callback_data=f"f2m_dl:{batch_id}",
                        )
                    ])

    if not keyboard_rows:
        keyboard_rows.append([
            InlineKeyboardButton(text="🔗 باز کردن صفحه دانلود در سایت", url=movie_url)
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        if poster and poster.startswith("http"):
            if edit_msg:
                try:
                    await edit_msg.delete()
                except Exception:
                    pass
            await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=poster,
                caption=caption_text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        else:
            if edit_msg:
                await edit_msg.edit_text(caption_text, reply_markup=markup, parse_mode="HTML")
            else:
                await message.reply(caption_text, reply_markup=markup, parse_mode="HTML")
    except Exception as pe:
        logger.warning("Failed to send with photo, falling back to text: %s", pe)
        if edit_msg:
            try:
                await edit_msg.edit_text(caption_text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                await message.reply(caption_text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.reply(caption_text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("f2m_dl:"))
async def handle_movie_download_click(callback: CallbackQuery):
    """Handle user clicking a quality button: deliver the direct download link."""
    short_id = callback.data.removeprefix("f2m_dl:")
    item = F2MLinkStore.get_link(short_id)

    if not item or not item.get("url"):
        await callback.answer("⚠️ لینک منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer("✅ لینک دانلود آماده شد!")

    title = item.get("title", "فیلم / سریال")
    quality = item.get("quality", "کیفیت اصلی")
    encoder = item.get("encoder", "")
    type_str = item.get("type", "نسخه اصلی")
    size_str = item.get("size") or "در فایل لینک موجود است"
    url = item.get("url", "")

    msg_text = (
        f"📥 <b>لینک دانلود مستقیم فیلم / سریال:</b>\n\n"
        f"🎬 <b>عنوان اثر:</b> {html.escape(title)}\n"
        f"📊 <b>کیفیت:</b> {html.escape(quality)} {html.escape(encoder)}\n"
        f"🏷 <b>نوع نسخه:</b> {html.escape(type_str)}\n"
        f"💾 <b>حجم فایل:</b> {html.escape(size_str)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>لینک دانلود مستقیم (جهت کپی در دانلود منیجر):</b>\n"
        f"<code>{url}</code>\n\n"
        f"💡 <i>راهنما: می‌توانید روی دکمه زیر کلیک کنید تا دانلود مستقیماً در مرورگر یا منیجر شما آغاز شود.</i>"
    )

    download_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬇️ شروع دانلود مستقیم", url=url),
            ]
        ]
    )

    await callback.message.reply(
        text=msg_text,
        reply_markup=download_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# -------------------------------------------------------------
# 5. Music Execution Engine (Spotify / Audio)
# -------------------------------------------------------------
async def _execute_music_search(message: Message, query: str):
    """Search music catalog and deliver the audio with download buttons."""
    status_msg = await message.reply(f"🎵 در حال جستجوی قطعه «<b>{html.escape(query)}</b>»...", parse_mode="HTML")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        tracks = await search_spotify(query, limit=5)
        if not tracks:
            await status_msg.edit_text(
                f"❌ موزیکی برای عبارت «<b>{html.escape(query)}</b>» یافت نشد.\n\n"
                "💡 <i>نکته: نام قطعه یا خواننده را بررسی و مجدداً امتحان کنید.</i>",
                parse_mode="HTML",
            )
            return

        top_track = tracks[0]
        await status_msg.edit_text(f"⏳ در حال دانلود و آماده‌سازی کیفیت عالی 320k برای <b>{html.escape(top_track.title)}</b>...", parse_mode="HTML")

        # Prepare audio file
        audio_path = None
        try:
            audio_path = await get_or_prepare_spotify_mp3(top_track)
        except Exception as pe:
            logger.warning("Failed preparing top track mp3: %s", pe)

        if audio_path and audio_path.exists() and audio_path.stat().st_size > 100000:
            caption = f"🎵 <b>{html.escape(top_track.title)}</b>\n👤 <b>هنرمند:</b> {html.escape(top_track.artist)}\n\n🤖 دانلود شده از ربات @Timod27_Bot"

            more_buttons = []
            # Add buttons for other tracks if found
            for idx, trk in enumerate(tracks[1:4], start=2):
                t_id = trk.track_id or uuid.uuid4().hex[:8]
                _MUSIC_CACHE[t_id] = trk
                btn_txt = f"🎵 {idx}. {trk.title} - {trk.artist}"
                if len(btn_txt) > 40:
                    btn_txt = btn_txt[:37] + "..."
                more_buttons.append([InlineKeyboardButton(text=btn_txt, callback_data=f"music_dl:{t_id}")])

            more_buttons.append([
                InlineKeyboardButton(text="🔍 جستجوی بیشتر در اینلاین", switch_inline_query_current_chat=query)
            ])

            markup = InlineKeyboardMarkup(inline_keyboard=more_buttons) if more_buttons else None

            await message.reply_audio(
                audio=FSInputFile(audio_path),
                title=top_track.title,
                performer=top_track.artist,
                duration=top_track.duration,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML",
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ متأسفانه در دانلود فایل صوتی این قطعه خطایی رخ داد. لطفاً قطعه دیگری را انتخاب کنید.")

    except Exception as e:
        logger.exception("Error in _execute_music_search for %s: %s", query, e)
        await status_msg.edit_text(f"❌ خطا در جستجوی موزیک: {e}")


@router.callback_query(F.data.startswith("music_dl:"))
async def handle_music_callback_download(callback: CallbackQuery):
    """Handle user clicking on one of the other music search results."""
    track_id = callback.data.removeprefix("music_dl:")
    meta = _MUSIC_CACHE.get(track_id) or get_cached_track_meta(track_id)

    if not meta:
        await callback.answer("⚠️ اطلاعات این موزیک منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer("⏳ در حال آماده‌سازی و ارسال فایل صوتی...")
    status_msg = await callback.message.reply(f"⏳ در حال دانلود و آماده‌سازی <b>{html.escape(meta.title)}</b> (320kbps)...", parse_mode="HTML")

    try:
        audio_path = await get_or_prepare_spotify_mp3(meta)
        if audio_path and audio_path.exists() and audio_path.stat().st_size > 100000:
            caption = f"🎵 <b>{html.escape(meta.title)}</b>\n👤 <b>هنرمند:</b> {html.escape(meta.artist)}\n\n🤖 دانلود شده از ربات @Timod27_Bot"
            await callback.message.reply_audio(
                audio=FSInputFile(audio_path),
                title=meta.title,
                performer=meta.artist,
                duration=meta.duration,
                caption=caption,
                parse_mode="HTML",
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ متأسفانه در دانلود این فایل صوتی خطایی رخ داد.")
    except Exception as e:
        logger.exception("Error downloading track from callback: %s", e)
        await status_msg.edit_text(f"❌ خطا در ارسال موزیک: {e}")
