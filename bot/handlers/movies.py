import re
import html
import uuid
import logging
import asyncio
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    BufferedInputFile,
)
from aiogram.enums import ChatAction

from bot.states import SearchStates
from bot.keyboards import MAIN_MENU_KEYBOARD, CANCEL_KEYBOARD
from bot.services.film2media import (
    search_f2m,
    get_movie_details,
    download_poster_bytes,
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


MOVIE_BUTTON_TEXTS = {
    "🎬 جستجوی فیلم و سریال",
    "جستجوی فیلم و سریال",
    "جست و جوی فیلم و سریال",
    "🎬 جست و جوی فیلم و سریال",
    "فیلم و سریال",
}

MUSIC_BUTTON_TEXTS = {
    "🎵 جستجوی موزیک",
    "جستجوی موزیک",
    "جست و جوی موزیک",
    "🎵 جست و جوی موزیک",
    "موزیک",
    "جستجوی آهنگ",
    "جست و جوی آهنگ",
}

CANCEL_BUTTON_TEXTS = {
    "❌ انصراف / بازگشت به منوی اصلی",
    "انصراف",
    "لغو",
    "بازگشت",
    "منوی اصلی",
}


# -------------------------------------------------------------
# 1. Main Menu Buttons & Commands Triggering FSM States
# -------------------------------------------------------------
@router.message(F.text.func(lambda t: bool(t and t.strip() in MOVIE_BUTTON_TEXTS)))
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


@router.message(F.text.func(lambda t: bool(t and t.strip() in MUSIC_BUTTON_TEXTS)))
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


@router.message(F.text.func(lambda t: bool(t and t.strip() in CANCEL_BUTTON_TEXTS)))
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
    if text in CANCEL_BUTTON_TEXTS:
        await state.clear()
        await message.answer("✅ به منوی اصلی بازگشتید:", reply_markup=MAIN_MENU_KEYBOARD)
        return

    if text in MUSIC_BUTTON_TEXTS:
        await start_music_search(message, state)
        return

    if text in MOVIE_BUTTON_TEXTS:
        await message.reply(
            "🎬 لطفاً نام فیلم یا سریال مورد نظر خود را ارسال کنید:\n*(مثلاً: Inception یا بتمن)*",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return

    await state.clear()
    await _execute_f2m_search(message, text)


@router.message(SearchStates.waiting_for_music, F.text & ~F.text.startswith("/"))
async def handle_music_query_input(message: Message, state: FSMContext):
    """Handle text input when specifically waiting for a music title."""
    text = message.text.strip()
    if text in CANCEL_BUTTON_TEXTS:
        await state.clear()
        await message.answer("✅ به منوی اصلی بازگشتید:", reply_markup=MAIN_MENU_KEYBOARD)
        return

    if text in MOVIE_BUTTON_TEXTS:
        await start_movie_search(message, state)
        return

    if text in MUSIC_BUTTON_TEXTS:
        await message.reply(
            "🎵 لطفاً نام آهنگ یا خواننده مورد نظر خود را ارسال کنید:\n*(مثلاً: Without Me یا شادمهر)*",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="HTML",
        )
        return

    await state.clear()
    await _execute_music_search(message, text)


# -------------------------------------------------------------
# 3. Fallback Handler for Raw Text when No State is Active
# -------------------------------------------------------------
@router.message(F.text & ~F.text.startswith("/"))
async def handle_unspecified_text(message: Message, state: FSMContext):
    """When user sends raw text without selecting a button, ask whether it is Movie or Music."""
    text = (message.text or "").strip()
    if text in MOVIE_BUTTON_TEXTS:
        await start_movie_search(message, state)
        return
    if text in MUSIC_BUTTON_TEXTS:
        await start_music_search(message, state)
        return
    if text in CANCEL_BUTTON_TEXTS:
        await handle_cancel_search(message, state)
        return

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
    """Execute search on Film2Media and ALWAYS display full results list with interactive buttons."""
    status_msg = await message.reply(
        f"🔎 در حال جستجوی «<b>{html.escape(query)}</b>» در فیلم‌تو‌مدیا...",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    try:
        results = await search_f2m(query)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if not results:
            await message.reply(
                f"❌ نتیجه‌ای برای «<b>{html.escape(query)}</b>» در سایت فیلم‌تو‌مدیا یافت نشد.\n\n"
                "💡 <i>نکته: نام فیلم یا سریال را به فارسی یا انگلیسی با املای دقیق‌تر جستجو کنید.</i>",
                parse_mode="HTML",
            )
            return

        # Always show all found results so user can choose their exact title
        keyboard_buttons = []
        text_lines = [
            f"🎬 <b>نتایج جستجو برای «{html.escape(query)}»:</b>\n",
            "لطفاً اثر مورد نظر خود را برای مشاهده کاور آرت و لینک‌های دانلود انتخاب کنید:\n",
        ]

        for idx, item in enumerate(results[:10], start=1):
            short_id = F2MLinkStore.save_link(item)
            year_str = f" ({item['year']})" if item.get("year") else ""
            rating_str = f" ⭐️ {item['rating']}" if item.get("rating") else ""
            type_str = "📺" if item.get("is_series") else "🎬"

            btn_text = f"{type_str} {idx}. {item['title']}{year_str}"
            if len(btn_text) > 42:
                btn_text = btn_text[:39] + "..."

            keyboard_buttons.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"f2m_sel:{short_id}")
            ])
            text_lines.append(f"{idx}️⃣ <b>{html.escape(item['title'])}</b>{year_str}{rating_str}")

        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await message.reply("\n".join(text_lines), reply_markup=markup, parse_mode="HTML")

    except Exception as e:
        logger.exception("Error executing movie search for %s: %s", query, e)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.reply(f"❌ خطا در جستجوی فیلم: {e}")


@router.callback_query(F.data.startswith("f2m_sel:"))
async def handle_movie_selection(callback: CallbackQuery):
    """Handle when user selects a movie from search results."""
    short_id = callback.data.removeprefix("f2m_sel:")
    item = F2MLinkStore.get_link(short_id)

    # If item is missing (e.g. from an old button before restart), smartly recover from button label!
    if not item or not item.get("url"):
        recovered_title = None
        try:
            if callback.message and callback.message.reply_markup:
                for row in callback.message.reply_markup.inline_keyboard:
                    for btn in row:
                        if btn.callback_data == callback.data:
                            cleaned = re.sub(r"^(?:🎬|📺)\s*\d+\.\s*", "", btn.text)
                            recovered_title = re.sub(r"\s*\(\d{4}\)$", "", cleaned).strip()
                            break
        except Exception:
            pass

        if recovered_title:
            await callback.answer("⏳ در حال بازیابی و دریافت اطلاعات اثر...")
            status_msg = await callback.message.reply(f"⏳ در حال استخراج کاور آرت و لینک‌های دانلود برای <b>{html.escape(recovered_title)}</b>...", parse_mode="HTML")
            try:
                results = await search_f2m(recovered_title)
                if results:
                    best_match = results[0]
                    await _show_movie_details(callback.message, best_match["url"], status_msg, search_item=best_match)
                    return
            except Exception as e:
                logger.warning("Recovery search failed for %s: %s", recovered_title, e)
            try:
                await status_msg.delete()
            except Exception:
                pass

        await callback.answer("⚠️ اطلاعات این اثر منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer("⏳ در حال آماده‌سازی کاور و کیفیت‌ها...")
    status_msg = await callback.message.reply("⏳ در حال دریافت کاور آرت و استخراج لینک‌های دانلود مستقیم...")

    await _show_movie_details(callback.message, item["url"], status_msg, search_item=item)


async def _show_movie_details(
    message: Message,
    movie_url: str,
    edit_msg: Message | None = None,
    search_item: Optional[dict] = None,
):
    """Fetch movie page, parse direct download links with sizes, send cover photo and direct buttons."""
    search_poster = search_item.get("poster") if search_item else None
    details = await get_movie_details(movie_url, search_poster=search_poster)
    if not details:
        err_text = "❌ متأسفانه در دریافت اطلاعات و لینک‌های دانلود این اثر خطایی رخ داد."
        if edit_msg:
            await edit_msg.edit_text(err_text)
        else:
            await message.reply(err_text)
        return

    title = details.get("title", "فیلم / سریال")
    story = details.get("story", "")
    poster_url = details.get("poster")
    downloads = details.get("downloads", [])
    is_series = details.get("is_series", False)
    series_seasons = details.get("series_seasons", {})

    caption_lines = [
        f"🎬 <b>{html.escape(title)}</b>\n",
    ]
    if story:
        caption_lines.append(f"📝 <b>خلاصه داستان:</b>\n{html.escape(story)}\n")
    caption_lines.append("━━━━━━━━━━━━━━━━━━━━")
    caption_lines.append("📥 <b>کیفیت مورد نظر خود را برای دریافت لینک دانلود انتخاب کنید:</b>")

    caption_text = "\n".join(caption_lines)
    keyboard_rows = []

    if downloads and not is_series:
        for d in downloads:
            short_id = d.get("short_id")
            size_str = f" | {d['size']}" if d.get("size") else ""
            type_icon = "🎧" if "دوبله" in d.get("type", "") else "📝"
            btn_label = f"{type_icon} {d['quality']} ({d['type']}){size_str}"
            if len(btn_label) > 46:
                btn_label = f"{type_icon} {d['quality']}{size_str}"

            keyboard_rows.append([
                InlineKeyboardButton(text=btn_label, callback_data=f"f2m_dl:{short_id}")
            ])

        # Button to deliver all links in one single message
        all_id = F2MLinkStore.save_link({
            "title": title,
            "downloads": downloads,
        })
        keyboard_rows.append([
            InlineKeyboardButton(text="📋 ارسال تمام لینک‌ها به صورت یکجا", callback_data=f"f2m_all:{all_id}")
        ])

    elif is_series and series_seasons:
        for s_name, q_dict in series_seasons.items():
            for q_tag, ep_list in q_dict.items():
                if ep_list:
                    season_id = F2MLinkStore.save_link({
                        "title": title,
                        "season_name": s_name,
                        "quality": q_tag,
                        "episodes": ep_list,
                    })
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            text=f"📁 {s_name} ({q_tag}) - {len(ep_list)} قسمت",
                            callback_data=f"f2m_season:{season_id}",
                        )
                    ])

    if not keyboard_rows:
        keyboard_rows.append([
            InlineKeyboardButton(text="⚠️ در حال حاضر لینکی برای این اثر موجود نیست", callback_data="noop")
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    # Download poster image bytes directly so Telegram never fails
    poster_bytes = None
    if poster_url:
        poster_bytes = await download_poster_bytes(poster_url)

    if poster_bytes:
        if edit_msg:
            try:
                await edit_msg.delete()
            except Exception:
                pass
        photo_file = BufferedInputFile(poster_bytes, filename="cover.jpg")
        try:
            await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=photo_file,
                caption=caption_text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception as pe:
            logger.warning("Failed sending photo: %s", pe)

    # Fallback to text message if photo could not be sent
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

    await callback.answer("✅ لینک دانلود مستقیم آماده شد!")

    title = item.get("title", "فیلم / سریال")
    quality = item.get("quality", "کیفیت اصلی")
    encoder = item.get("encoder", "")
    type_str = item.get("type", "نسخه اصلی")
    size_str = item.get("size") or "مشخص در لینک"
    url = item.get("url", "")

    enc_text = f" {encoder}" if encoder else ""
    msg_text = (
        f"📥 <b>لینک دانلود مستقیم:</b>\n\n"
        f"🎬 <b>{html.escape(title)}</b>\n"
        f"▫️ <b>کیفیت:</b> <code>{html.escape(quality)}{html.escape(enc_text)}</code>\n"
        f"▫️ <b>نسخه:</b> {html.escape(type_str)}\n"
        f"▫️ <b>حجم:</b> <code>{html.escape(size_str)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>لینک مستقیم (جهت کپی در دانلود منیجر):</b>\n"
        f"<code>{url}</code>\n\n"
        f"💡 <i>روی دکمه زیر کلیک کنید تا دانلود مستقیماً در دانلود منیجر شما آغاز شود:</i>"
    )

    btn_label = f"⬇️ شروع دانلود مستقیم ({size_str})" if item.get("size") else "⬇️ شروع دانلود مستقیم"
    download_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=btn_label, url=url),
            ]
        ]
    )

    await callback.message.reply(
        text=msg_text,
        reply_markup=download_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f2m_all:"))
async def handle_movie_all_links(callback: CallbackQuery):
    """Handle user clicking 'ارسال تمام لینک‌ها به صورت یکجا': output all direct URLs."""
    all_id = callback.data.removeprefix("f2m_all:")
    item = F2MLinkStore.get_link(all_id)

    if not item or not item.get("downloads"):
        await callback.answer("⚠️ اطلاعات لینک‌ها منقضی شده است.", show_alert=True)
        return

    await callback.answer("📋 در حال ارسال تمام لینک‌ها...")
    title = item.get("title", "فیلم")
    downloads = item.get("downloads", [])

    lines = [
        f"🎬 <b>تمام لینک‌های دانلود مستقیم برای {html.escape(title)}:</b>\n",
        "💡 <i>می‌توانید هر یک از لینک‌های زیر را مستقیماً در دانلود منیجر (ADM یا IDM) کپی کنید:</i>\n",
    ]

    for d in downloads:
        size_str = f" ({d['size']})" if d.get("size") else ""
        type_icon = "🎧" if "دوبله" in d.get("type", "") else "📝"
        lines.append(
            f"{type_icon} <b>{html.escape(d['quality'])} - {html.escape(d['type'])}{size_str}:</b>\n"
            f"<code>{d['url']}</code>\n"
        )

    full_text = "\n".join(lines)
    if len(full_text) > 3900:
        chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
        for chunk in chunks:
            await callback.message.reply(chunk, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await callback.message.reply(full_text, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("f2m_season:"))
async def handle_series_season(callback: CallbackQuery):
    """Handle user clicking on a series season: deliver all episode direct download links."""
    season_id = callback.data.removeprefix("f2m_season:")
    item = F2MLinkStore.get_link(season_id)

    if not item or not item.get("episodes"):
        await callback.answer("⚠️ اطلاعات این فصل منقضی شده است.", show_alert=True)
        return

    await callback.answer("📁 در حال ارسال لینک‌های قسمت‌ها...")
    title = item.get("title", "سریال")
    s_name = item.get("season_name", "فصل")
    q_tag = item.get("quality", "کیفیت اصلی")
    episodes = item.get("episodes", [])

    lines = [
        f"📺 <b>{html.escape(title)}</b>\n"
        f"📁 <b>{html.escape(s_name)} - کیفیت {html.escape(q_tag)} ({len(episodes)} قسمت):</b>\n\n"
        "🔗 <b>لینک‌های مستقیم قسمت‌ها (جهت کپی در دانلود منیجر):</b>\n",
    ]

    for ep in episodes:
        lines.append(
            f"🔹 <b>قسمت {ep['episode']}:</b>\n"
            f"<code>{ep['url']}</code>\n"
        )

    full_text = "\n".join(lines)
    if len(full_text) > 3900:
        chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
        for chunk in chunks:
            await callback.message.reply(chunk, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await callback.message.reply(full_text, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    await callback.answer()


# -------------------------------------------------------------
# 5. Music Execution Engine (Spotify / Audio)
# -------------------------------------------------------------
async def _execute_music_search(message: Message, query: str):
    """Search music catalog and deliver the audio with download buttons."""
    status_msg = await message.reply(
        f"🎵 در حال جستجوی قطعه «<b>{html.escape(query)}</b>»...",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        tracks = await search_spotify(query, limit=5)
        if not tracks:
            try:
                await status_msg.delete()
            except Exception:
                pass
            await message.reply(
                f"❌ موزیکی برای عبارت «<b>{html.escape(query)}</b>» یافت نشد.\n\n"
                "💡 <i>نکته: نام قطعه یا خواننده را بررسی و مجدداً امتحان کنید.</i>",
                parse_mode="HTML",
            )
            return

        top_track = tracks[0]

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

            try:
                await status_msg.delete()
            except Exception:
                pass

            await message.reply_audio(
                audio=FSInputFile(audio_path),
                title=top_track.title,
                performer=top_track.artist,
                duration=top_track.duration,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML",
            )
        else:
            try:
                await status_msg.delete()
            except Exception:
                pass
            await message.reply("❌ متأسفانه در دانلود فایل صوتی این قطعه خطایی رخ داد. لطفاً قطعه دیگری را انتخاب کنید.")

    except Exception as e:
        logger.exception("Error in _execute_music_search for %s: %s", query, e)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.reply(f"❌ خطا در جستجوی موزیک: {e}")


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
