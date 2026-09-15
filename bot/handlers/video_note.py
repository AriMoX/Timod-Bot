import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from aiogram.enums import ChatAction

from bot.config import DOWNLOADS_DIR
from bot.services.video_converter import convert_to_video_note
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)

router = Router(name="video_note_router")

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram Bot API limit


@router.message(F.video)
async def handle_video_message(message: Message):
    """Handle regular video messages."""
    video = message.video
    if not video:
        return

    if video.file_size and video.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply("❌ حجم ویدیو بیشتر از حد مجاز (۵۰ مگابایت) است.")
        return

    await process_video(
        message=message,
        file_id=video.file_id,
        file_name=video.file_name or f"{video.file_unique_id}.mp4",
        duration=video.duration,
    )


@router.message(F.document)
async def handle_document_video(message: Message):
    """Handle videos sent as documents/files."""
    doc = message.document
    if not doc:
        return

    # Check if document is a video
    is_video = (doc.mime_type and doc.mime_type.startswith("video/")) or (
        doc.file_name and doc.file_name.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"))
    )

    if not is_video:
        return  # Ignore non-video documents so other handlers or normal flow isn't disturbed

    if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply("❌ حجم فایل بیشتر از حد مجاز (۵۰ مگابایت) است.")
        return

    await process_video(
        message=message,
        file_id=doc.file_id,
        file_name=doc.file_name or f"{doc.file_unique_id}.mp4",
        duration=None,
    )


async def process_video(message: Message, file_id: str, file_name: str, duration: int | None):
    """Downloads video, converts it to 1:1 circular video note, and replies."""
    status_msg = await message.reply("⏳ در حال دریافت ویدیو و تبدیل به ویدیو مسیج...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.RECORD_VIDEO_NOTE)

    input_path: Path | None = None
    output_path: Path | None = None

    try:
        # Determine extension
        ext = Path(file_name).suffix or ".mp4"
        input_path = DOWNLOADS_DIR / f"in_{message.message_id}_{file_id[:10]}{ext}"

        # Download from Telegram
        await message.bot.download(file=file_id, destination=input_path)

        # Convert to 1:1 Video Note
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)
        output_path = await convert_to_video_note(input_path=input_path)

        # Send Video Note
        note_duration = min(duration or 60, 60)
        await message.reply_video_note(
            video_note=FSInputFile(output_path),
            duration=note_duration,
            length=480,
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Failed to convert video note for message %s: %s", message.message_id, e)
        await status_msg.edit_text(
            "❌ متأسفانه در تبدیل این ویدیو به ویدیو مسیج خطایی رخ داد.\n"
            "لطفاً مطمئن شوید فایل یک ویدیوی سالم است و دوباره امتحان کنید."
        )
    finally:
        safe_remove(input_path, output_path)
