import asyncio
import logging
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


@dataclass
class PinterestMedia:
    media_type: str  # "video" or "photo"
    title: str
    uploader: str
    file_path: Path
    duration: int | None = None
    width: int | None = None
    height: int | None = None


def _resolve_url(url: str) -> str:
    """Resolve redirect for short pin.it links."""
    if "pin.it" in url:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resolved = resp.geturl()
                logger.info("Resolved pin.it to: %s", resolved)
                return resolved
        except Exception as e:
            logger.warning("Failed to resolve pin.it redirect: %s", e)
    return url


def _download_pinterest_sync(url: str) -> PinterestMedia:
    """Download Pinterest video or original photo."""
    target_url = _resolve_url(url)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # 1. Attempt download using yt-dlp (works for video pins)
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "pin_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if info:
                if "entries" in info:
                    info = info["entries"][0]

                video_id = info.get("id")
                filename = ydl.prepare_filename(info)
                file_path = Path(filename)

                if not file_path.exists():
                    matches = list(DOWNLOADS_DIR.glob(f"pin_{video_id}.*"))
                    if matches:
                        file_path = matches[0]

                if file_path.exists():
                    is_video = file_path.suffix.lower() in [".mp4", ".mov", ".mkv", ".webm"]
                    title = info.get("description") or info.get("title") or "Pinterest Media"
                    uploader = info.get("uploader") or "Pinterest"
                    duration = int(info.get("duration") or 0)
                    width = info.get("width")
                    height = info.get("height")

                    return PinterestMedia(
                        media_type="video" if is_video else "photo",
                        title=title,
                        uploader=uploader,
                        duration=duration,
                        file_path=file_path,
                        width=width,
                        height=height,
                    )
    except Exception as e:
        logger.info("yt-dlp could not extract video from Pinterest, falling back to image scraping: %s", e)

    # 2. Fallback: Extract high-resolution image from Pinterest web page
    return _scrape_pinterest_image(target_url)


def _scrape_pinterest_image(url: str) -> PinterestMedia:
    """Scrape original high-res image from Pinterest HTML."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    # Extract og:image
    image_match = re.search(r'property="og:image"\s+content="([^"]+)"', html) or re.search(
        r'content="([^"]+)"\s+property="og:image"', html
    )
    if not image_match:
        raise ValueError("Could not find any video or image in this Pinterest link.")

    image_url = image_match.group(1)

    # Convert 736x or other thumbnails to originals for highest quality
    orig_url = re.sub(r"/\d+x/", "/originals/", image_url)

    # Extract title
    title_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    title = title_match.group(1) if title_match else "Pinterest Photo"

    # Extract pin ID from URL
    pin_id_match = re.search(r"/pin/(\d+)", url)
    pin_id = pin_id_match.group(1) if pin_id_match else "image"
    output_path = DOWNLOADS_DIR / f"pin_{pin_id}.jpg"

    # Download the image (try original first, fallback to og:image)
    for dl_url in [orig_url, image_url]:
        try:
            img_req = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=10) as img_resp:
                output_path.write_bytes(img_resp.read())
            break
        except Exception:
            continue

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise FileNotFoundError("Failed to download Pinterest image.")

    return PinterestMedia(
        media_type="photo",
        title=title,
        uploader="Pinterest",
        file_path=output_path,
    )


async def download_pinterest(url: str) -> PinterestMedia:
    """Asynchronously download Pinterest video/photo in a worker thread."""
    return await asyncio.to_thread(_download_pinterest_sync, url)
