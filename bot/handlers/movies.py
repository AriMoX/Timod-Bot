import re
import html
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ChatAction

from bot.services.film2media import (
    search_f2m,
    get_movie_details,
    F2MLinkStore,
)

logger = logging.getLogger(__name__)

router = Router(name="movies_router")

# Cache search results short keys for callback buttons
_SEARCH_CACHE: dict[str, dict] = {}


def _is_url(text: str) -> bool:
    return bool(re.search(r"https?://", text, re.IGNORECASE))


@router.message(Command("movie"))
@router.message(Command("film"))
@router.message(Command("serial"))
async def cmd_movie_search(message: Message):
    """Handle /movie <name> or /film <name> command."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "🎬 <b>راهنمای جستجوی فیلم و سریال:</b>\n\n"
            "لطفاً نام فیلم یا سریال مورد نظر خود را بعد از دستور بنویسید:\n"
            "مثال:\n"
            "• <code>/movie Inception</code>\n"
            "• <code>/movie بتمن</code>\n"
            "• <code>/serial Slow Horses</code>\n\n"
            "یا کافیست نام فیلم را مستقیماً در چت بفرستید!",
            parse_mode="HTML",
        )
        return

    query = parts[1].strip()
    await _execute_f2m_search(message, query)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_movie_search(message: Message):
    """Auto-search Film2Media when user sends a movie or series title (not a URL)."""
    text = (message.text or "").strip()
    # Ignore if text contains URL or is too short or too long
    if _is_url(text) or len(text) < 2 or len(text) > 100:
        return

    # If message starts with prefixes like "فیلم", "سریال", "دانلود"
    clean_query = text
    for prefix in ["دانلود فیلم و سریال", "دانلود فیلم", "دانلود سریال", "فیلم", "سریال"]:
        if clean_query.startswith(prefix):
            clean_query = clean_query[len(prefix):].strip()
            break

    query = clean_query if clean_query else text
    await _execute_f2m_search(message, query)


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

        # If only 1 result found, display its details immediately
        if len(results) == 1:
            await status_msg.edit_text("⏳ در حال دریافت کیفیت‌ها و لینک‌های دانلود...")
            await _show_movie_details(message, results[0]["url"], status_msg)
            return

        # Multiple results: show inline buttons list
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

    await callback.answer("⏳ در حال دریافت لینک‌های دانلود و محاسبه حجم‌ها...")
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

    # 1. Movie Downloads
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

    # 2. TV Series with Seasons
    elif is_series and series_seasons:
        caption_lines.append("\n📺 <b>فصل‌های موجود سریال:</b>")
        for s_name, q_dict in series_seasons.items():
            for q_tag, ep_list in q_dict.items():
                first_ep = ep_list[0] if ep_list else None
                if first_ep:
                    # Save a batch item for downloading first ep or batch
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
        # If poster available, try sending as photo
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
    size_str = item.get("size") or "در لینک موجود است"
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
