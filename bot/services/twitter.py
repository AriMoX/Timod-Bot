import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


@dataclass
class TwitterVideo:
    title: str
    uploader: str
    duration: int
    file_path: Path
    width: int | None = None
    height: int | None = None


def _download_twitter_sync(url: str) -> TwitterVideo:
    """Download Twitter / X video synchronously via yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "tw_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Could not extract tweet information.")

        if "entries" in info:
            info = info["entries"][0]

        video_id = info.get("id")
        filename = ydl.prepare_filename(info)
        file_path = Path(filename)

        # Check if actual file extension changed during remuxing (e.g. .mp4)
        if not file_path.exists():
            matches = list(DOWNLOADS_DIR.glob(f"tw_{video_id}.*"))
            if matches:
                file_path = matches[0]
            else:
                raise FileNotFoundError(f"Twitter video file not found for ID: {video_id}")

        title = info.get("description") or info.get("title") or "Twitter Video"
        uploader = info.get("uploader") or info.get("uploader_id") or "Twitter"
        duration = int(info.get("duration") or 0)
        width = info.get("width")
        height = info.get("height")

        return TwitterVideo(
            title=title,
            uploader=uploader,
            duration=duration,
            file_path=file_path,
            width=width,
            height=height,
        )


async def download_twitter(url: str) -> TwitterVideo:
    """Asynchronously download a Twitter / X video in a thread."""
    return await asyncio.to_thread(_download_twitter_sync, url)
