import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB limit


@dataclass
class YouTubeVideo:
    title: str
    uploader: str
    duration: int
    file_path: Path
    width: int | None = None
    height: int | None = None


def _download_youtube_sync(url: str) -> YouTubeVideo:
    """Synchronously download YouTube video or Shorts using yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Prefer MP4 container with max quality under 50MB for Telegram compatibility
    ydl_opts = {
        "format": "bestvideo[filesize<=40M][ext=mp4]+bestaudio[filesize<=10M][ext=m4a]/best[filesize<=49M][ext=mp4]/best[ext=mp4]/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "yt_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Could not extract YouTube video information.")

        if "entries" in info:
            info = info["entries"][0]

        video_id = info.get("id")
        filename = ydl.prepare_filename(info)
        file_path = Path(filename)

        if not file_path.exists():
            matches = list(DOWNLOADS_DIR.glob(f"yt_{video_id}.*"))
            if matches:
                file_path = matches[0]
            else:
                raise FileNotFoundError(f"YouTube video file not found for ID: {video_id}")

        # Check Telegram 50MB upload limit
        if file_path.stat().st_size > TELEGRAM_MAX_BYTES:
            raise ValueError(
                f"حجم این ویدیو ({file_path.stat().st_size / (1024*1024):.1f} مگابایت) بیشتر از سقف مجاز تلگرام (۵۰ مگابایت) است."
            )

        title = info.get("title") or "YouTube Video"
        uploader = info.get("uploader") or info.get("channel") or "YouTube"
        duration = int(info.get("duration") or 0)
        width = info.get("width")
        height = info.get("height")

        return YouTubeVideo(
            title=title,
            uploader=uploader,
            duration=duration,
            file_path=file_path,
            width=width,
            height=height,
        )


async def download_youtube(url: str) -> YouTubeVideo:
    """Asynchronously download YouTube video/Shorts in a worker thread."""
    return await asyncio.to_thread(_download_youtube_sync, url)
