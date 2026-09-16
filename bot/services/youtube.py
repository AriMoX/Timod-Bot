import os
import asyncio
import logging
import subprocess
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
from bot.utils.cleanup import safe_remove

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


def _sanitize_cookies(cookie_content: str) -> str:
    """
    Sanitizes Netscape cookies by removing volatile session-binding tokens
    (SIDTS, SIDCC, YSC) which trigger Google's 'The page needs to be reloaded' error
    when used on a different IP or machine than where they were exported.
    """
    lines = cookie_content.replace("\r\n", "\n").replace("\\n", "\n").replace("\\t", "\t").splitlines()
    clean_lines = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            clean_lines.append(line)
            continue
        # Drop volatile tokens that trigger session reload challenges
        if any(bad_token in line for bad_token in ["SIDTS", "SIDCC", "YSC"]):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines).strip() + "\n"


def _prepare_youtube_cookie_file() -> str | None:
    """Returns path to cookie file if configured via file or env var."""
    raw_cookies = os.getenv("YOUTUBE_COOKIES_TEXT") or YOUTUBE_COOKIES_TEXT
    if raw_cookies:
        sanitized = _sanitize_cookies(raw_cookies)
        cookie_path = DOWNLOADS_DIR / "yt_cookies.txt"
        cookie_path.write_text(sanitized, encoding="utf-8")
        return str(cookie_path)

    if YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists():
        cookie_path = Path(YOUTUBE_COOKIE_FILE)
        sanitized = _sanitize_cookies(cookie_path.read_text(encoding="utf-8", errors="ignore"))
        clean_file = DOWNLOADS_DIR / "yt_cookies.txt"
        clean_file.write_text(sanitized, encoding="utf-8")
        return str(clean_file)

    # Check for cookies.txt in root directory or downloads directory
    for candidate in [DOWNLOADS_DIR / "yt_cookies.txt", DOWNLOADS_DIR / "cookies.txt", DOWNLOADS_DIR.parent / "cookies.txt"]:
        if candidate.exists() and candidate.stat().st_size > 50:
            sanitized = _sanitize_cookies(candidate.read_text(encoding="utf-8", errors="ignore"))
            clean_file = DOWNLOADS_DIR / "yt_cookies.txt"
            clean_file.write_text(sanitized, encoding="utf-8")
            return str(clean_file)

    return None


def _download_youtube_sync(url: str) -> YouTubeVideo:
    """Synchronously download YouTube video or Shorts using yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Resilient format selector: picks best video and audio up to 1080p,
    # and remuxes/merges them seamlessly to MP4 format for Telegram playback
    ydl_opts = {
        "format": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "yt_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "js_runtimes": {"node": {}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
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

            # Ensure MP4 container for native Telegram video player
            if file_path.suffix.lower() != ".mp4":
                mp4_path = file_path.with_suffix(".mp4")
                try:
                    subprocess.run(
                        [ffmpeg_exe, "-y", "-i", str(file_path), "-c", "copy", str(mp4_path)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=60,
                    )
                    safe_remove(file_path)
                    file_path = mp4_path
                except Exception as remux_err:
                    logger.warning("Failed to remux %s to mp4: %s", file_path, remux_err)

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

