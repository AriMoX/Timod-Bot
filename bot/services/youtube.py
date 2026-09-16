import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import (
    DOWNLOADS_DIR,
    PROXY_URL,
    YOUTUBE_COOKIE_FILE,
    YOUTUBE_COOKIES_TEXT,
)

logger = logging.getLogger(__name__)

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB limit


class YouTubeBotDetectionError(Exception):
    """Raised when YouTube blocks datacenter IP access and requires a cookie or proxy."""
    pass


@dataclass
class YouTubeVideo:
    title: str
    uploader: str
    duration: int
    file_path: Path
    width: int | None = None
    height: int | None = None


def _prepare_youtube_cookie_file() -> str | None:
    """Returns path to cookie file if configured via file or env var."""
    if YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists():
        return str(YOUTUBE_COOKIE_FILE)

    if YOUTUBE_COOKIES_TEXT:
        cookie_path = DOWNLOADS_DIR / "yt_cookies.txt"
        cookie_path.write_text(YOUTUBE_COOKIES_TEXT.strip(), encoding="utf-8")
        return str(cookie_path)

    # Check for cookies.txt in root directory
    root_cookies = DOWNLOADS_DIR.parent / "cookies.txt"
    if root_cookies.exists():
        return str(root_cookies)

    return None


def _download_youtube_sync(url: str) -> YouTubeVideo:
    """Synchronously download YouTube video or Shorts using yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Prefer MP4 container with max quality under 50MB for Telegram compatibility
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "yt_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "js_runtimes": {"node": {}},
    }

    cookie_file = _prepare_youtube_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
        logger.info("Using YouTube cookie file: %s", cookie_file)

    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
        logger.info("Using proxy for YouTube: %s", PROXY_URL)

    try:
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

    except yt_dlp.utils.DownloadError as de:
        err_str = str(de).lower()
        if any(kw in err_str for kw in ["sign in to confirm", "bot", "failed to extract any player response", "429"]):
            raise YouTubeBotDetectionError("YouTube bot protection blocked access on datacenter IP") from de
        raise


async def download_youtube(url: str) -> YouTubeVideo:
    """Asynchronously download YouTube video/Shorts in a worker thread."""
    return await asyncio.to_thread(_download_youtube_sync, url)

