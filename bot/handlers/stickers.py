import os
import time
import asyncio
from pathlib import Path
from PIL import Image
import imageio_ffmpeg

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

from bot.config import DOWNLOADS_DIR
from bot.handlers.video_note import process_video

router = Router()

@router.message(F.photo | F.video | F.animation | F.document | F.video_note)
async def handle_media_for_sticker(message: Message):
    # Ignore commands, text, or specific audio/voice types
    if message.voice or message.audio:
        return
        
    if message.document:
        mime = message.document.mime_type or ""
        if not (mime.startswith("image/") or mime.startswith("video/")):
            return
            
    btns = []
    
    if message.photo or message.animation or message.video_note:
        btns.append([InlineKeyboardButton(text="🎨 تبدیل به استیکر", callback_data="make_sticker")])
    elif message.video or message.document:
        btns.append([
            InlineKeyboardButton(text="🔄 ویدیو مسیج (دایره‌ای)", callback_data="make_video_note"),
            InlineKeyboardButton(text="🎨 تبدیل به استیکر", callback_data="make_sticker")
        ])
        
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await message.reply("✅ فایل شما دریافت شد! چه عملیاتی انجام دهم؟", reply_markup=kb)


@router.callback_query(F.data == "make_sticker")
async def process_make_sticker(call: CallbackQuery):
    msg = call.message.reply_to_message
    if not msg:
        await call.answer("❌ پیام اصلی پیدا نشد! لطفاً فایل را دوباره ارسال کنید.", show_alert=True)
        return
        
    await call.message.edit_text("⏳ در حال آنالیز و تبدیل فایل به استیکر، لطفاً چند لحظه صبر کنید...")
    
    in_path = None
    out_path = None
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            is_video = False
        elif msg.video:
            file_id = msg.video.file_id
            is_video = True
        elif msg.animation:
            file_id = msg.animation.file_id
            is_video = True
        elif msg.video_note:
            file_id = msg.video_note.file_id
            is_video = True
        elif msg.document:
            file_id = msg.document.file_id
            mime = msg.document.mime_type or ""
            is_video = mime.startswith("video/")
        else:
            await call.message.edit_text("❌ فرمت فایل ناشناخته است.")
            return

        file = await call.bot.get_file(file_id)
        if file.file_size > 15 * 1024 * 1024:
            await call.message.edit_text("❌ حجم فایل برای تبدیل به استیکر بیش از حد مجاز است (حداکثر ۱۵ مگابایت).")
            return
            
        ext = ".mp4" if is_video else ".jpg"
        in_path = DOWNLOADS_DIR / f"in_stk_{time.time()}{ext}"
        out_path = DOWNLOADS_DIR / f"out_stk_{time.time()}"
        out_path = out_path.with_suffix(".webm" if is_video else ".webp")
        
        await call.bot.download_file(file.file_path, str(in_path))
        
        if not is_video:
            # --- CONVERT IMAGE TO WEBP ---
            with Image.open(in_path) as img:
                img = img.convert("RGBA")
                w, h = img.size
                if w > h:
                    new_w = 512
                    new_h = int(h * (512 / w))
                else:
                    new_h = 512
                    new_w = int(w * (512 / h))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                img.save(out_path, format="WEBP", quality=100)
                
            await call.message.answer_sticker(sticker=FSInputFile(out_path))
            
        else:
            # --- CONVERT VIDEO TO WEBM VP9 ---
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe, "-y",
                "-i", str(in_path),
                "-t", "3", # Telegram video stickers MUST be max 3 seconds
                "-vf", "scale='if(gt(iw,ih),512,-1)':'if(gt(iw,ih),-1,512)',fps=30",
                "-c:v", "libvpx-vp9",
                "-crf", "30",
                "-b:v", "0",
                "-an", # No audio
                str(out_path)
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            
            if out_path.exists():
                try:
                    await call.message.answer_sticker(sticker=FSInputFile(out_path))
                except Exception as e:
                    # If it exceeds 256KB or has wrong dimensions, send as regular file
                    await call.message.answer_document(
                        document=FSInputFile(out_path), 
                        caption="⚠️ متأسفانه حجم این ویدیو استیکر کمی از لیمیت تلگرام (۲۵۶ کیلوبایت) بیشتر شد، اما می‌توانید آن را به عنوان فایل WEBM ذخیره کنید."
                    )
            else:
                await call.message.edit_text("❌ خطا در تبدیل ویدیو به استیکر.")
                return
                
        await call.message.delete()
        
    except Exception as e:
        await call.message.edit_text(f"❌ خطا در ساخت استیکر:\n{str(e)[:100]}")
    finally:
        for p in (in_path, out_path):
            if p and p.exists():
                try: p.unlink()
                except: pass

@router.callback_query(F.data == "make_video_note")
async def process_make_video_note(call: CallbackQuery):
    msg = call.message.reply_to_message
    if not msg:
        await call.answer("❌ پیام اصلی پیدا نشد!", show_alert=True)
        return
        
    await call.message.delete()
    
    if msg.video:
        file_id = msg.video.file_id
        file_name = msg.video.file_name or f"{msg.video.file_unique_id}.mp4"
        duration = msg.video.duration
    elif msg.document:
        file_id = msg.document.file_id
        file_name = msg.document.file_name or f"{msg.document.file_unique_id}.mp4"
        duration = None
    else:
        await call.answer("فایل نامعتبر.", show_alert=True)
        return
        
    await process_video(
        message=msg,
        file_id=file_id,
        file_name=file_name,
        duration=duration
    )

