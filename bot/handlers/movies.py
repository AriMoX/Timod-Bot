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
        "💡 <i>دکمه‌های منوی اصلی و انصراف همیشه در پایین صفحه در دسترس شما هستند.</i>"
    )
    await message.answer(prompt, reply_markup=MAIN_MENU_KEYBOARD, parse_mode="HTML")


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
    await message.answer(prompt, reply_markup=MAIN_MENU_KEYBOARD, parse_mode="HTML")
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
        await message.answer(
            "🎬 لطفاً نام فیلم یا سریال مورد نظر خود را ارسال کنید:\n*(مثلاً: Inception یا بتمن)*",
            reply_markup=MAIN_MENU_KEYBOARD,
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
        await message.answer(
            "🎵 لطفاً نام آهنگ یا خواننده مورد نظر خود را ارسال کنید:\n*(مثلاً: Without Me یا شادمهر)*",
            reply_markup=MAIN_MENU_KEYBOARD,
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

    await message.answer(
        f"❓ می‌خواهید عبارت «<b>{html.escape(text)}</b>» را در کدام بخش جستجو کنید؟",
        reply_markup=choice_markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("choice_movie:"))
async def handle_choice_movie(callback: CallbackQuery):
    query_id = callback.data.removeprefix("choice_movie:")
    query = _PENDING_QUERIES.pop(query_id, None)
    if not query and callback.message and callback.message.text:
        m = re.search(r"«(.+?)»", callback.message.text)
        if m:
            query = m.group(1).strip()

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
    if not query and callback.message and callback.message.text:
        m = re.search(r"«(.+?)»", callback.message.text)
        if m:
            query = m.group(1).strip()

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
    status_msg = await message.answer(
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
            await message.answer(
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

        keyboard_buttons.append([
            InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
            InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
        ])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await message.answer("\n".join(text_lines), reply_markup=markup, parse_mode="HTML")

    except Exception as e:
        logger.exception("Error executing movie search for %s: %s", query, e)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(f"❌ خطا در جستجوی فیلم: {e}")


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
            status_msg = await callback.message.answer(f"⏳ در حال استخراج کاور آرت و لینک‌های دانلود برای <b>{html.escape(recovered_title)}</b>...", parse_mode="HTML")
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
    status_msg = await callback.message.answer("⏳ در حال دریافت کاور آرت و استخراج لینک‌های دانلود مستقیم...")

    await _show_movie_details(callback.message, item["url"], status_msg, search_item=item)


PAGE_SIZE = 6


def build_paginated_keyboard(
    menu_id: str,
    items: list[dict],
    page: int = 1,
    page_size: int = PAGE_SIZE,
    all_id: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build an InlineKeyboardMarkup for a list of items with pagination controls."""
    total_items = len(items)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = items[start_idx:end_idx]

    rows = []
    for it in page_items:
        rows.append([
            InlineKeyboardButton(text=it["text"], callback_data=it["callback_data"])
        ])

    if all_id:
        rows.append([
            InlineKeyboardButton(text="📋 ارسال تمام لینک‌ها به صورت یکجا", callback_data=f"f2m_all:{all_id}")
        ])

    if total_pages > 1:
        pag_row = []
        if page < total_pages:
            pag_row.append(
                InlineKeyboardButton(
                    text="◀️ صفحه بعد",
                    callback_data=f"f2m_page:{menu_id}:{page + 1}",
                )
            )

        pag_row.append(
            InlineKeyboardButton(
                text=f"📄 {page} از {total_pages}",
                callback_data="noop",
            )
        )

        if page > 1:
            pag_row.append(
                InlineKeyboardButton(
                    text="صفحه قبل ▶️",
                    callback_data=f"f2m_page:{menu_id}:{page - 1}",
                )
            )
        rows.append(pag_row)

    rows.append([
        InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_movie_details(
    message: Message,
    movie_url: str,
    edit_msg: Message | None = None,
    search_item: Optional[dict] = None,
    initial_page: int = 1,
):
    """Fetch movie page, parse direct download links with sizes, send cover photo and direct buttons."""
    search_poster = search_item.get("poster") if search_item else None
    details = await get_movie_details(movie_url, search_poster=search_poster)
    if not details:
        err_text = "❌ متأسفانه در دریافت اطلاعات و لینک‌های دانلود این اثر خطایی رخ داد."
        if edit_msg:
            await edit_msg.edit_text(err_text)
        else:
            await message.answer(err_text)
        return

    title = details.get("title", "فیلم / سریال")
    story = details.get("story", "")
    poster_url = details.get("poster")
    downloads = details.get("downloads", [])
    is_series = details.get("is_series", False)
    series_seasons = details.get("series_seasons", {})

    items = []
    all_id = None

    if downloads and not is_series:
        for d in downloads:
            short_id = d.get("short_id")
            size_str = f" | {d['size']}" if d.get("size") else ""
            type_icon = "🎙" if "دوبله" in d.get("type", "") else "📝"
            btn_label = f"{type_icon} {d['quality']} [{d['type']}]{size_str}"
            if len(btn_label) > 46:
                btn_label = f"{type_icon} {d['quality']} [{d['type'][:12]}]{size_str}"

            items.append({
                "text": btn_label,
                "callback_data": f"f2m_dl:{short_id}",
            })

        all_id = F2MLinkStore.save_link({
            "title": title,
            "movie_url": movie_url,
            "downloads": downloads,
        })

    elif is_series and series_seasons:
        def _season_num(s_name: str) -> int:
            m = re.search(r'\d+', s_name)
            return int(m.group(0)) if m else 999

        sorted_seasons = sorted(series_seasons.items(), key=lambda x: _season_num(x[0]))

        def _q_sort_key(key_str: str) -> tuple:
            is_dub = "دوبله" in key_str
            q_order = {"4k": 1, "2160p": 1, "1080p": 2, "720p": 3, "480p": 4}
            q_score = 10
            for q_name, score in q_order.items():
                if q_name in key_str.lower():
                    q_score = score
                    break
            # Dubbed first (0), then Subtitled (1), sorted by quality
            return (0 if is_dub else 1, q_score)

        for s_name, q_dict in sorted_seasons:
            sorted_qualities = sorted(
                q_dict.items(),
                key=lambda x: _q_sort_key(x[0])
            )
            for q_key, ep_list in sorted_qualities:
                if ep_list:
                    ep_list.sort(key=lambda x: x.get("episode", 0))
                    sample_ep = ep_list[0]
                    q_tag = sample_ep.get("quality", q_key.split(" - ")[0])
                    version_str = sample_ep.get("version", "دوبله فارسی" if "دوبله" in q_key else "زیرنویس فارسی")
                    is_dub = sample_ep.get("is_dub", "دوبله" in version_str)
                    type_icon = "🎙" if is_dub else "📝"

                    item_page = (len(items) // PAGE_SIZE) + 1
                    season_id = F2MLinkStore.save_link({
                        "title": title,
                        "movie_url": movie_url,
                        "season_name": s_name,
                        "quality": q_tag,
                        "version": version_str,
                        "is_dub": is_dub,
                        "episodes": ep_list,
                        "page": item_page,
                    })
                    items.append({
                        "text": f"📁 {s_name} ({q_tag} | {type_icon} {version_str}) - {len(ep_list)} قسمت",
                        "callback_data": f"f2m_season:{season_id}",
                    })

    # Caption preparation with strict length limit (< 1000 chars)
    caption_lines = [
        f"🎬 <b>{html.escape(title)}</b>\n",
    ]
    if story:
        caption_lines.append(f"📝 <b>خلاصه داستان:</b>\n{html.escape(story)}\n")
    caption_lines.append("━━━━━━━━━━━━━━━━━━━━")
    if is_series:
        caption_lines.append("📥 <b>فصل و کیفیت مورد نظر خود را برای مشاهده قسمت‌ها انتخاب کنید:</b>")
    else:
        caption_lines.append("📥 <b>کیفیت مورد نظر خود را برای دریافت لینک دانلود انتخاب کنید:</b>")

    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    if total_pages > 1:
        caption_lines.append(f"💡 <i>(دارای {total_pages} صفحه - با دکمه‌های زیر ورق بزنید)</i>")

    caption_text = "\n".join(caption_lines)
    if len(caption_text) > 1000:
        avail = 1000 - len(title) - 200
        if avail > 60:
            short_story = story[:avail] + "..."
            caption_lines[1] = f"📝 <b>خلاصه داستان:</b>\n{html.escape(short_story)}\n"
            caption_text = "\n".join(caption_lines)
        else:
            caption_text = f"🎬 <b>{html.escape(title)}</b>\n\n📥 <b>کیفیت یا فصل مورد نظر خود را انتخاب کنید:</b>"

    if not items:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ در حال حاضر لینکی برای این اثر موجود نیست", callback_data="noop")],
            [
                InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
                InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
            ]
        ])
    else:
        menu_id = F2MLinkStore.save_link({
            "type": "paginated_menu",
            "items": items,
            "all_id": all_id,
        })
        markup = build_paginated_keyboard(
            menu_id=menu_id,
            items=items,
            page=initial_page,
            page_size=PAGE_SIZE,
            all_id=all_id,
        )

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
            await message.answer(caption_text, reply_markup=markup, parse_mode="HTML")
    else:
        await message.answer(caption_text, reply_markup=markup, parse_mode="HTML")


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
    is_dub = "دوبله" in type_str
    type_icon = "🎙" if is_dub else "📝"
    msg_text = (
        f"📥 <b>لینک دانلود مستقیم:</b>\n\n"
        f"🎬 <b>{html.escape(title)}</b>\n"
        f"▫️ <b>نوع محتوا:</b> {type_icon} <b>{html.escape(type_str)}</b>\n"
        f"▫️ <b>کیفیت:</b> <code>{html.escape(quality)}{html.escape(enc_text)}</code>\n"
        f"▫️ <b>حجم فایل:</b> <code>{html.escape(size_str)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>لینک مستقیم ({type_icon} {html.escape(type_str)}):</b>\n"
        f"<code>{url}</code>\n\n"
        f"💡 <i>روی دکمه زیر کلیک کنید تا دانلود مستقیماً در دانلود منیجر شما آغاز شود:</i>"
    )

    btn_label = f"⬇️ شروع دانلود ({type_icon} {type_str} | {size_str})" if item.get("size") else f"⬇️ شروع دانلود ({type_icon} {type_str})"
    nav_buttons = [
        [
            InlineKeyboardButton(text=btn_label, url=url),
        ]
    ]
    if item.get("movie_url"):
        nav_buttons.append([
            InlineKeyboardButton(text="🔙 بازگشت به لیست کیفیت‌ها", callback_data=f"f2m_back_to:{short_id}"),
        ])
    nav_buttons.append([
        InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
    ])
    download_markup = InlineKeyboardMarkup(inline_keyboard=nav_buttons)

    await callback.message.answer(
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
        "💡 <i>نوع هر نسخه (🎙 دوبله فارسی یا 📝 زیرنویس فارسی) در کنار کیفیت درج شده است:</i>\n",
    ]

    for d in downloads:
        size_str = f" ({d['size']})" if d.get("size") else ""
        type_icon = "🎙" if "دوبله" in d.get("type", "") else "📝"
        lines.append(
            f"{type_icon} <b>کیفیت {html.escape(d['quality'])} 【{html.escape(d['type'])}】{size_str}:</b>\n"
            f"<code>{d['url']}</code>\n"
        )

    nav_buttons = []
    if item.get("movie_url"):
        nav_buttons.append([
            InlineKeyboardButton(text="🔙 بازگشت به مشخصات اثر", callback_data=f"f2m_back_to:{all_id}"),
        ])
    nav_buttons.append([
        InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
    ])
    nav_markup = InlineKeyboardMarkup(inline_keyboard=nav_buttons)

    full_text = "\n".join(lines)
    if len(full_text) > 3900:
        chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
        for idx, chunk in enumerate(chunks):
            m = nav_markup if idx == len(chunks) - 1 else None
            await callback.message.answer(chunk, reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await callback.message.answer(full_text, reply_markup=nav_markup, parse_mode="HTML", disable_web_page_preview=True)


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
    version_str = item.get("version", "دوبله فارسی" if item.get("is_dub") else "زیرنویس فارسی")
    is_dub = item.get("is_dub", "دوبله" in version_str)
    type_icon = "🎙" if is_dub else "📝"
    episodes = item.get("episodes", [])

    lines = [
        f"📺 <b>{html.escape(title)}</b>\n"
        f"📁 <b>{html.escape(s_name)} - کیفیت {html.escape(q_tag)} ({type_icon} {html.escape(version_str)}) - {len(episodes)} قسمت:</b>\n\n"
        f"▫️ <b>نوع محتوا:</b> {type_icon} <b>{html.escape(version_str)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>لینک‌های مستقیم قسمت‌ها (جهت کپی در دانلود منیجر):</b>\n",
    ]

    for ep in episodes:
        lines.append(
            f"🔹 <b>قسمت {ep['episode']} ({type_icon} {html.escape(version_str)}):</b>\n"
            f"<code>{ep['url']}</code>\n"
        )

    nav_buttons = []
    if item.get("movie_url"):
        nav_buttons.append([
            InlineKeyboardButton(text="🔙 بازگشت به لیست فصل‌ها", callback_data=f"f2m_back_to:{season_id}"),
        ])
    nav_buttons.append([
        InlineKeyboardButton(text="🔍 جستجوی یک اثر دیگر", callback_data="f2m_new_search"),
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
    ])
    nav_markup = InlineKeyboardMarkup(inline_keyboard=nav_buttons)

    full_text = "\n".join(lines)
    if len(full_text) > 3900:
        chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
        for idx, chunk in enumerate(chunks):
            m = nav_markup if idx == len(chunks) - 1 else None
            await callback.message.answer(chunk, reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await callback.message.answer(full_text, reply_markup=nav_markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "f2m_main_menu")
async def handle_f2m_main_menu(callback: CallbackQuery, state: FSMContext):
    """Handle inline button 'منوی اصلی': return to main menu and restore bottom reply keyboard."""
    await state.clear()
    await callback.answer("🏠 بازگشت به منوی اصلی")
    await callback.message.answer(
        "🏠 <b>منوی اصلی ربات:</b>\n\n"
        "یکی از گزینه‌های زیر را برای جستجو یا دانلود انتخاب کنید:",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "f2m_new_search")
async def handle_f2m_new_search(callback: CallbackQuery, state: FSMContext):
    """Handle inline button 'جستجوی یک اثر دیگر': prompt user for a new movie or series title."""
    await state.set_state(SearchStates.waiting_for_movie)
    await callback.answer("🎬 جستجوی اثر جدید")
    await callback.message.answer(
        "🎬 <b>جستجوی فیلم و سریال (فیلم‌تو‌مدیا):</b>\n\n"
        "لطفاً نام فیلم یا سریال جدید را به فارسی یا انگلیسی ارسال کنید:\n"
        "*(به عنوان مثال: <code>Inception</code> یا <code>بتمن</code> یا <code>Slow Horses</code> یا <code>Breaking Bad</code>)*\n\n"
        "💡 <i>دکمه‌های منوی اصلی و انصراف همیشه در پایین صفحه در دسترس شما هستند.</i>",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "f2m_new_search_music")
async def handle_f2m_new_search_music(callback: CallbackQuery, state: FSMContext):
    """Handle inline button 'جستجوی یک آهنگ دیگر': prompt user for a new music query."""
    await state.set_state(SearchStates.waiting_for_music)
    await callback.answer("🎵 جستجوی آهنگ جدید")
    await callback.message.answer(
        "🎵 <b>جستجو و دانلود موزیک (اسپاتیفای):</b>\n\n"
        "لطفاً نام آهنگ یا خواننده مورد نظر خود را ارسال کنید:\n"
        "*(به عنوان مثال: <code>Without Me</code> یا <code>شایع</code>)*\n\n"
        "💡 <i>دکمه‌های منوی اصلی و انصراف همیشه در پایین صفحه در دسترس شما هستند.</i>",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("f2m_page:") | F.data.startswith("f2m_spage:"))
async def handle_f2m_page(callback: CallbackQuery):
    """Handle flipping pages for series or qualities without re-sending the whole message."""
    raw = callback.data
    prefix = "f2m_spage:" if raw.startswith("f2m_spage:") else "f2m_page:"
    parts = raw.removeprefix(prefix).split(":")
    if len(parts) != 2:
        await callback.answer()
        return

    menu_id, page_str = parts[0], parts[1]
    try:
        page = int(page_str)
    except ValueError:
        page = 1

    menu_data = F2MLinkStore.get_link(menu_id)
    if not menu_data or "items" not in menu_data:
        await callback.answer("⚠️ اطلاعات این صفحه منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    items = menu_data["items"]
    all_id = menu_data.get("all_id")
    new_markup = build_paginated_keyboard(
        menu_id=menu_id,
        items=items,
        page=page,
        page_size=PAGE_SIZE,
        all_id=all_id,
    )

    try:
        await callback.message.edit_reply_markup(reply_markup=new_markup)
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        await callback.answer(f"📄 صفحه {page} از {total_pages}")
    except Exception as e:
        logger.warning("Error editing page reply markup: %s", e)
        await callback.answer()


@router.callback_query(F.data.startswith("f2m_back_to:"))
async def handle_f2m_back_to(callback: CallbackQuery):
    """Handle returning back to the movie details / seasons list on the exact same page."""
    ref_id = callback.data.removeprefix("f2m_back_to:")
    item = F2MLinkStore.get_link(ref_id)
    movie_url = item.get("movie_url") if item else None

    if not movie_url:
        await callback.answer("⚠️ اطلاعات این اثر یافت نشد. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    initial_page = item.get("page", 1)
    await callback.answer("⏳ در حال بازگشت به مشخصات اثر...")
    status_msg = await callback.message.answer("⏳ در حال بازگشت به لیست کیفیت‌ها...")
    await _show_movie_details(callback.message, movie_url, status_msg, search_item=item, initial_page=initial_page)


# -------------------------------------------------------------
# 5. Music Execution Engine (Spotify / Audio)
# -------------------------------------------------------------
async def _execute_music_search(message: Message, query: str):
    """Search music catalog and deliver the audio with download buttons."""
    status_msg = await message.answer(
        f"🎵 در حال جستجوی قطعه «<b>{html.escape(query)}</b>»...",
        reply_markup=MAIN_MENU_KEYBOARD,
        parse_mode="HTML",
    )
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        tracks = await search_spotify(query, limit=5)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if not tracks:
            await message.answer(
                f"❌ موزیکی برای عبارت «<b>{html.escape(query)}</b>» یافت نشد.\n\n"
                "💡 <i>نکته: نام قطعه یا خواننده را بررسی و مجدداً امتحان کنید.</i>",
                parse_mode="HTML",
            )
            return

        # Prepare instant interactive list of found tracks
        text_lines = [
            f"🎵 <b>نتایج جستجو برای «{html.escape(query)}»:</b>\n",
            "لطفاً قطعه مورد نظر خود را برای دانلود مستقیم انتخاب کنید:\n",
        ]
        
        more_buttons = []
        for idx, trk in enumerate(tracks, start=1):
            t_id = trk.track_id or uuid.uuid4().hex[:8]
            _MUSIC_CACHE[t_id] = trk
            
            # Format text in the message
            duration_str = ""
            if trk.duration:
                m, s = divmod(trk.duration, 60)
                duration_str = f" [{m}:{s:02d}]"
            text_lines.append(f"{idx}️⃣ <b>{html.escape(trk.title)}</b> - {html.escape(trk.artist)}{duration_str}")
            
            # Button for this track
            btn_txt = f"🎵 {idx}. {trk.title} - {trk.artist}"
            if len(btn_txt) > 40:
                btn_txt = btn_txt[:37] + "..."
            more_buttons.append([InlineKeyboardButton(text=btn_txt, callback_data=f"music_dl:{t_id}")])

        more_buttons.append([
            InlineKeyboardButton(text="🔍 جستجوی یک آهنگ دیگر", callback_data="f2m_new_search_music"),
            InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="f2m_main_menu"),
        ])

        markup = InlineKeyboardMarkup(inline_keyboard=more_buttons)
        await message.answer("\n".join(text_lines), reply_markup=markup, parse_mode="HTML")

        # Background pre-cache the top 3 tracks to make download instant if they click them!
        import asyncio
        asyncio.create_task(get_or_prepare_spotify_mp3(tracks[0]))
        if len(tracks) > 1:
            asyncio.create_task(get_or_prepare_spotify_mp3(tracks[1]))
        if len(tracks) > 2:
            asyncio.create_task(get_or_prepare_spotify_mp3(tracks[2]))

    except Exception as e:
        logger.exception("Error in _execute_music_search for %s: %s", query, e)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(f"❌ خطا در جستجوی موزیک: {e}")


@router.callback_query(F.data.startswith("music_dl:"))
async def handle_music_callback_download(callback: CallbackQuery):
    """Handle user clicking on one of the other music search results."""
    track_id = callback.data.removeprefix("music_dl:")
    meta = _MUSIC_CACHE.get(track_id) or get_cached_track_meta(track_id)

    if not meta:
        await callback.answer("⚠️ اطلاعات این موزیک منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer("⏳ در حال آماده‌سازی و ارسال فایل صوتی...")
    status_msg = await callback.message.answer(f"⏳ در حال دانلود و آماده‌سازی <b>{html.escape(meta.title)}</b> (320kbps)...", parse_mode="HTML")

    try:
        audio_path = await get_or_prepare_spotify_mp3(meta)
        if audio_path and audio_path.exists() and audio_path.stat().st_size > 100000:
            await callback.message.answer_audio(
                audio=FSInputFile(audio_path),
                title=meta.title,
                performer=meta.artist,
                duration=meta.duration,
                caption=None,
                parse_mode="HTML",
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ متأسفانه در دانلود این فایل صوتی خطایی رخ داد.")
    except Exception as e:
        logger.exception("Error downloading track from callback: %s", e)
        await status_msg.edit_text(f"❌ خطا در ارسال موزیک: {e}")
