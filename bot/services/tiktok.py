import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


@dataclass
class TikTokVideo:
    title: str
    uploader: str
    duration: int
    file_path: Path
    width: int | None = None
    height: int | None = None


def _download_tiktok_sync(url: str) -> TikTokVideo:
    """Download TikTok video synchronously via yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    ydl_opts = {
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "tt_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Could not extract TikTok video information.")

        if "entries" in info:
            info = info["entries"][0]

        video_id = info.get("id")
        filename = ydl.prepare_filename(info)
        file_path = Path(filename)

        if not file_path.exists():
            matches = list(DOWNLOADS_DIR.glob(f"tt_{video_id}.*"))
            if matches:
                file_path = matches[0]
            else:
                raise FileNotFoundError(f"TikTok video file not found for ID: {video_id}")

        title = info.get("description") or info.get("title") or "TikTok Video"
        uploader = info.get("uploader") or info.get("uploader_id") or info.get("channel") or "TikTok"
        duration = int(info.get("duration") or 0)
        width = info.get("width")
        height = info.get("height")

        return TikTokVideo(
            title=title,
            uploader=uploader,
            duration=duration,
            file_path=file_path,
            width=width,
            height=height,
        )


async def download_tiktok(url: str) -> TikTokVideo:
    """Asynchronously download TikTok video in a worker thread."""
    return await asyncio.to_thread(_download_tiktok_sync, url)
