import asyncio
import json
import logging
import re
import urllib.parse
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


def _extract_pin_id(url: str) -> str | None:
    """Extract numeric Pin ID from Pinterest URL."""
    m = re.search(r"/pin/(\d+)", url)
    return m.group(1) if m else None


def _download_stream(media_url: str, output_path: Path) -> bool:
    """Fast streaming download of direct photo or video URL."""
    try:
        req = urllib.request.Request(
            media_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.pinterest.com/",
            }
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            with open(output_path, "wb") as f:
                while chunk := resp.read(64 * 1024):
                    f.write(chunk)
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception as e:
        logger.warning("Stream download failed for %s: %s", media_url[:60], e)
        return False


def _get_pin_resource_data(pin_id: str) -> dict | None:
    """Fetch structured metadata from Pinterest PinResource endpoint."""
    endpoint = "https://www.pinterest.com/resource/PinResource/get/"
    query = {
        "data": json.dumps({
            "options": {
                "field_set_key": "unauth_react_main_pin",
                "id": pin_id
            }
        })
    }
    full_url = f"{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Pinterest-PWS-Handler": "www/[username].js",
            "Accept": "application/json, text/javascript, */*, q=0.01"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("resource_response", {}).get("data")
    except Exception as e:
        logger.warning("Failed to query Pinterest PinResource for ID %s: %s", pin_id, e)
        return None


def _find_best_video(data: dict) -> tuple[str | None, int, int | None, int | None]:
    """Find best MP4 URL, duration (seconds), width, height from Pin data."""
    videos = data.get("videos")
    if isinstance(videos, dict) and "video_list" in videos:
        v_list = videos["video_list"]
        best_v = None
        best_res = -1
        for _, v_info in v_list.items():
            if not isinstance(v_info, dict):
                continue
            url = v_info.get("url")
            if not url or "m3u8" in url:
                continue
            w = v_info.get("width") or 0
            h = v_info.get("height") or 0
            res = w * h
            if res > best_res:
                best_res = res
                best_v = v_info
        if best_v and best_v.get("url"):
            duration = int((best_v.get("duration") or 0) / 1000)
            return best_v["url"], duration, best_v.get("width"), best_v.get("height")

    story = data.get("story_pin_data")
    if isinstance(story, dict):
        pages = story.get("pages", [])
        for p in pages:
            for b in p.get("blocks", []):
                vid = b.get("video", {})
                v_list = vid.get("video_list", {})
                for _, v_info in v_list.items():
                    if isinstance(v_info, dict) and v_info.get("url") and "m3u8" not in v_info["url"]:
                        duration = int((v_info.get("duration") or 0) / 1000)
                        return v_info["url"], duration, v_info.get("width"), v_info.get("height")

    return None, 0, None, None


def _download_pinterest_sync(url: str) -> PinterestMedia:
    """Download Pinterest video or high-res photo."""
    target_url = _resolve_url(url)
    pin_id = _extract_pin_id(target_url)

    # 1. Primary Method: Query Pinterest PinResource API (Fastest & most reliable)
    if pin_id:
        data = _get_pin_resource_data(pin_id)
        if data:
            title = (
                data.get("title")
                or data.get("grid_title")
                or data.get("closeup_unified_description")
                or data.get("description")
                or "Pinterest Media"
            ).strip()

            uploader = (
                data.get("closeup_attribution", {}).get("full_name")
                or data.get("pinner", {}).get("username")
                or "Pinterest"
            )

            # Check video
            video_url, duration, width, height = _find_best_video(data)
            if video_url:
                out_vid = DOWNLOADS_DIR / f"pin_{pin_id}.mp4"
                if _download_stream(video_url, out_vid):
                    return PinterestMedia(
                        media_type="video",
                        title=title,
                        uploader=uploader,
                        file_path=out_vid,
                        duration=duration,
                        width=width,
                        height=height,
                    )

            # Check image
            images = data.get("images", {})
            if isinstance(images, dict) and images:
                best_img_url = (
                    images.get("orig", {}).get("url")
                    or images.get("736x", {}).get("url")
                    or images.get("564x", {}).get("url")
                    or (list(images.values())[-1].get("url") if images else None)
                )
                if best_img_url:
                    out_img = DOWNLOADS_DIR / f"pin_{pin_id}.jpg"
                    if _download_stream(best_img_url, out_img):
                        return PinterestMedia(
                            media_type="photo",
                            title=title,
                            uploader=uploader,
                            file_path=out_img,
                            width=images.get("orig", {}).get("width"),
                            height=images.get("orig", {}).get("height"),
                        )

    # 2. Secondary Method: Pinterest Public oEmbed API (Guaranteed access, 0% datacenter blocks)
    try:
        oembed_url = f"https://www.pinterest.com/oembed.json?url={target_url}"
        oe_req = urllib.request.Request(
            oembed_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(oe_req, timeout=10) as oe_resp:
            oe_data = json.loads(oe_resp.read().decode("utf-8"))
            oe_thumb = oe_data.get("thumbnail_url")
            oe_author = oe_data.get("author_name") or "Pinterest"
            oe_title = (oe_data.get("title") or "").strip()
            
            p_id = pin_id or "photo"
            if oe_thumb:
                # Convert 236x/736x to originals for crystal clear quality
                orig_thumb = re.sub(r"/\d+x/", "/originals/", oe_thumb)
                out_img = DOWNLOADS_DIR / f"pin_{p_id}.jpg"
                for cand_url in [orig_thumb, oe_thumb]:
                    if _download_stream(cand_url, out_img):
                        return PinterestMedia(
                            media_type="photo",
                            title=oe_title,
                            uploader=oe_author,
                            file_path=out_img,
                            width=oe_data.get("width"),
                            height=oe_data.get("height"),
                        )
    except Exception as oe_err:
        logger.info("Pinterest oEmbed method failed: %s", oe_err)

    # 3. Tertiary Method: Attempt download using yt-dlp
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
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

                v_id = info.get("id") or (pin_id or "media")
                filename = ydl.prepare_filename(info)
                file_path = Path(filename)

                if not file_path.exists():
                    matches = list(DOWNLOADS_DIR.glob(f"pin_{v_id}.*"))
                    if matches:
                        file_path = matches[0]

                if file_path.exists():
                    is_video = file_path.suffix.lower() in [".mp4", ".mov", ".mkv", ".webm"]
                    title = info.get("description") or info.get("title") or "Pinterest Media"
                    uploader = info.get("uploader") or "Pinterest"
                    duration = int(info.get("duration") or 0)

                    return PinterestMedia(
                        media_type="video" if is_video else "photo",
                        title=title,
                        uploader=uploader,
                        duration=duration,
                        file_path=file_path,
                        width=info.get("width"),
                        height=info.get("height"),
                    )
    except Exception as e:
        logger.info("yt-dlp fallback failed for Pinterest: %s", e)

    raise ValueError("امکان دریافت این محتوا از پینترست وجود ندارد یا پین خصوصی/حذف شده است.")


async def download_pinterest(url: str) -> PinterestMedia:
    """Asynchronously download Pinterest video/photo in a worker thread."""
    return await asyncio.to_thread(_download_pinterest_sync, url)
