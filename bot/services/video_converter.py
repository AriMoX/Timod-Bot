import asyncio
import logging
from pathlib import Path
import imageio_ffmpeg

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


async def convert_to_video_note(input_path: Path, output_filename: str | None = None) -> Path:
    """
    Converts an input video file into a 1:1 square MP4 video note for Telegram.
    - Centers crop to 1:1 aspect ratio
    - Scales to 480x480
    - Encodes video as H.264 (yuv420p)
    - Encodes audio as AAC (if present)
    - Trims to max 60 seconds (Telegram limit)
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not output_filename:
        output_filename = f"note_{input_path.stem}.mp4"
    output_path = DOWNLOADS_DIR / output_filename

    # FFmpeg command arguments
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(input_path),
        "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480,setsar=1",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Running FFmpeg conversion for %s", input_path.name)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")
        logger.error("FFmpeg conversion failed: %s", err_msg)
        raise RuntimeError(f"FFmpeg conversion error (code {proc.returncode}): {err_msg[:400]}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise FileNotFoundError("Converted video note file was not created or is empty.")

    logger.info("Video note created successfully: %s (%d bytes)", output_path, output_path.stat().st_size)
    return output_path
