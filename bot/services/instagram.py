import asyncio
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp
from yt_dlp.extractor.instagram import InstagramIE

from bot.config import DOWNLOADS_DIR, INSTAGRAM_SESSIONID, INSTAGRAM_COOKIE_FILE

logger = logging.getLogger(__name__)

# Patch InstagramIE to prevent raising error on photo posts (yt-dlp defaults to video-only check)
_orig_raise_no_formats = InstagramIE.raise_no_formats


def _safe_raise_no_formats(self, msg, expected=False, video_id=None):
    if "no video" in str(msg).lower():
        return
    _orig_raise_no_formats(self, msg, expected, video_id)


InstagramIE.raise_no_formats = _safe_raise_no_formats


class InstagramLoginRequiredError(Exception):
    """Raised when Instagram blocks anonymous access and requires a cookie/login."""
    pass


@dataclass
class InstagramItem:
    media_type: str  # "video" or "photo"
    file_path: Path
    duration: int = 0
    width: int | None = None
    height: int | None = None


@dataclass
class InstagramMedia:
    title: str
    uploader: str
    items: list[InstagramItem]

    @property
    def file_path(self) -> Path:
        return self.items[0].file_path if self.items else Path("")

    @property
    def duration(self) -> int:
        return self.items[0].duration if self.items else 0

    @property
    def width(self) -> int | None:
        return self.items[0].width if self.items else None

    @property
    def height(self) -> int | None:
        return self.items[0].height if self.items else None


def _download_file(url: str, dest_path: Path) -> bool:
    """Download a direct photo/video URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.instagram.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            dest_path.write_bytes(resp.read())
        return dest_path.exists() and dest_path.stat().st_size > 0
    except Exception as e:
        logger.warning("Failed to download file from %s: %s", url[:60], e)
        return False


def _prepare_cookie_file() -> str | None:
    """Generates a proper Netscape cookie file if INSTAGRAM_SESSIONID is set."""
    if INSTAGRAM_COOKIE_FILE and Path(INSTAGRAM_COOKIE_FILE).exists():
        return str(INSTAGRAM_COOKIE_FILE)
    if INSTAGRAM_SESSIONID:
        cookie_path = DOWNLOADS_DIR / "ig_session_cookies.txt"
        content = (
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{INSTAGRAM_SESSIONID.strip()}\n"
        )
        cookie_path.write_text(content, encoding="utf-8")
        return str(cookie_path)
    return None


def resolve_instagram_url(url: str) -> str:
    """Follow redirects for Instagram share and short links (e.g. share/..., ig.me)."""
    if "/share/" in url or "ig.me" in url:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.url
        except Exception as e:
            logger.warning("Could not resolve redirect for %s: %s", url, e)
    return url


def _download_instagram_sync(url: str) -> InstagramMedia:
    """Download Instagram video(s), photo(s), or carousel posts via yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    clean_url = resolve_instagram_url(url)

    # 1. Base options for yt-dlp
    ydl_opts = {
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "ig_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }

    # Apply Instagram authentication cookies if configured
    cookie_file = _prepare_cookie_file()
    if cookie_file:
        logger.info("Using Instagram cookies file: %s", cookie_file)
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First extract metadata to inspect items
            info = ydl.extract_info(clean_url, download=False)
            if not info:
                raise ValueError("Could not extract Instagram media information.")

            # Get list of entries (carousel or single item)
            raw_entries = info.get("entries")
            if raw_entries:
                entries = [e for e in raw_entries if e]
            else:
                entries = [info]

            post_title = info.get("description") or info.get("title") or "Instagram Media"
            post_uploader = info.get("uploader") or info.get("channel") or "Instagram"

            items: list[InstagramItem] = []

            for idx, entry in enumerate(entries):
                entry_id = entry.get("id") or f"{info.get('id', 'media')}_{idx}"
                formats = entry.get("formats") or []
                has_video = any(f.get("vcodec") and f.get("vcodec") != "none" for f in formats)

                if has_video:
                    # Download video with yt-dlp
                    v_opts = dict(ydl_opts)
                    v_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                    v_opts["outtmpl"] = str(DOWNLOADS_DIR / f"ig_{entry_id}.%(ext)s")

                    with yt_dlp.YoutubeDL(v_opts) as v_ydl:
                        v_info = v_ydl.process_ie_result(entry, download=True)
                        v_filename = v_ydl.prepare_filename(v_info)
                        v_path = Path(v_filename)

                    if not v_path.exists():
                        matches = list(DOWNLOADS_DIR.glob(f"ig_{entry_id}.*"))
                        if matches:
                            v_path = matches[0]

                    if v_path.exists():
                        items.append(
                            InstagramItem(
                                media_type="video",
                                file_path=v_path,
                                duration=int(entry.get("duration") or 0),
                                width=entry.get("width"),
                                height=entry.get("height"),
                            )
                        )
                else:
                    # It's a photo! Find the best thumbnail / image candidate
                    thumbs = entry.get("thumbnails") or []
                    best_url = None
                    width = None
                    height = None

                    if thumbs:
                        best_thumb = sorted(
                            thumbs,
                            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
                            reverse=True,
                        )[0]
                        best_url = best_thumb.get("url")
                        width = best_thumb.get("width")
                        height = best_thumb.get("height")

                    if not best_url:
                        best_url = entry.get("url")

                    if best_url:
                        img_path = DOWNLOADS_DIR / f"ig_{entry_id}.jpg"
                        if _download_file(best_url, img_path):
                            items.append(
                                InstagramItem(
                                    media_type="photo",
                                    file_path=img_path,
                                    width=width,
                                    height=height,
                                )
                            )

            if not items:
                raise ValueError("هیچ ویدیو یا عکسی در این پست اینستاگرام یافت نشد.")

            return InstagramMedia(
                title=post_title,
                uploader=post_uploader,
                items=items,
            )

    except yt_dlp.utils.DownloadError as e:
        err_text = str(e)
        if any(keyword in err_text.lower() for keyword in ["empty media response", "not granting access", "login", "authentication"]):
            raise InstagramLoginRequiredError(
                "Instagram requires authentication (cookies) for this media."
            ) from e
        raise


async def download_instagram(url: str) -> InstagramMedia:
    """Asynchronously download Instagram media (photos/videos) in a worker thread."""
    return await asyncio.to_thread(_download_instagram_sync, url)
