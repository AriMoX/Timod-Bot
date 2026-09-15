import asyncio
import json
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
class SpotifyTrack:
    title: str
    artist: str
    duration: int
    file_path: Path
    thumbnail_path: Path | None = None
    track_id: str = "track"


def _get_spotify_metadata(url: str) -> tuple[str, str, int, str | None, str]:
    """
    Extracts title, artist, duration (seconds), cover art URL, and unique track ID from Spotify.
    Uses ultra-fast Spotify embed metadata (<300ms) with oEmbed fallback.
    """
    track_id_match = re.search(r"/track/([A-Za-z0-9]+)", url)
    track_id = track_id_match.group(1) if track_id_match else "track"

    title = "Spotify Track"
    artist = "Unknown Artist"
    duration = 0
    cover_url = None

    # 1. Primary: Ultra-lightweight Spotify embed page (~10KB JSON)
    try:
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if m:
            data = json.loads(m.group(1))
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            if entity.get("name"):
                title = entity["name"].strip()
            artists = [a.get("name").strip() for a in entity.get("artists", []) if a.get("name")]
            if artists:
                artist = " & ".join(artists)
            if entity.get("duration"):
                duration = int(entity["duration"] / 1000)

            visual = entity.get("visualIdentity", {})
            if visual and "image" in visual:
                imgs = visual["image"]
                if imgs:
                    cover_url = imgs[-1].get("url")
            return title, artist, duration, cover_url, track_id
    except Exception as e:
        logger.warning("Failed to fetch Spotify embed metadata: %s", e)

    # 2. Fallback: Spotify oEmbed endpoint
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={url}"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("title"):
                title = data["title"]
            if data.get("thumbnail_url"):
                cover_url = data["thumbnail_url"]
    except Exception as e:
        logger.warning("Failed to fetch Spotify oEmbed: %s", e)

    return title, artist, duration, cover_url, track_id


def _download_spotify_sync(url: str) -> SpotifyTrack:
    """Download the matching track audio via fast single-pass search and return SpotifyTrack."""
    title, artist, meta_duration, cover_url, track_id = _get_spotify_metadata(url)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Download cover art thumbnail if available
    thumb_path = None
    if cover_url:
        try:
            thumb_path = DOWNLOADS_DIR / f"spot_thumb_{track_id}.jpg"
            img_req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=8) as img_resp:
                thumb_path.write_bytes(img_resp.read())
        except Exception as e:
            logger.warning("Failed to download Spotify cover thumbnail: %s", e)
            thumb_path = None

    # Search candidates to try in order of precision and speed
    search_queries = [
        f"ytsearch1:{artist} - {title}",
        f"ytsearch1:{artist} {title} audio",
        f"scsearch1:{artist} {title}",
    ]

    dl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / f"spot_{track_id}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    last_error = None
    for q in search_queries:
        logger.info("Fast searching audio for Spotify track: %s", q)
        try:
            with yt_dlp.YoutubeDL(dl_opts) as dl_ydl:
                dl_info = dl_ydl.extract_info(q, download=True)
                if not dl_info:
                    continue

                if "entries" in dl_info:
                    entries = dl_info["entries"]
                    if not entries:
                        continue
                    dl_info = entries[0]

                item_id = dl_info.get("id")
                filename = dl_ydl.prepare_filename(dl_info)
                file_path = Path(filename)

                if not file_path.exists():
                    matches = list(DOWNLOADS_DIR.glob(f"spot_{track_id}_{item_id}.*"))
                    if matches:
                        file_path = matches[0]
                    else:
                        continue

                duration = int(dl_info.get("duration") or meta_duration or 0)
                logger.info("Successfully downloaded track: %s in audio format", file_path.name)
                return SpotifyTrack(
                    title=title,
                    artist=artist,
                    duration=duration,
                    file_path=file_path,
                    thumbnail_path=thumb_path,
                    track_id=track_id,
                )
        except Exception as q_err:
            logger.warning("Query '%s' failed: %s", q, q_err)
            last_error = q_err

    raise ValueError(f"امکان یافتن یا دانلود فایل صوتی این قطعه وجود ندارد: '{artist} - {title}'. ({last_error})")


async def download_spotify(url: str) -> SpotifyTrack:
    """Asynchronously download Spotify track in a worker thread."""
    return await asyncio.to_thread(_download_spotify_sync, url)
