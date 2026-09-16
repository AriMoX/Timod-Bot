import asyncio
import json
import logging
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import imageio_ffmpeg
import yt_dlp

from bot.config import DOWNLOADS_DIR
from bot.utils.cleanup import safe_remove

logger = logging.getLogger(__name__)


@dataclass
class SpotifyTrack:
    title: str
    artist: str
    duration: int
    file_path: Path
    thumbnail_path: Path | None = None
    track_id: str = "track"


def resolve_spotify_url(url: str) -> str:
    """Resolve redirects for short spotify.link or share links."""
    if "spotify.link" in url or "/intl-" in url:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resolved = resp.geturl()
                logger.info("Resolved Spotify URL to: %s", resolved)
                return resolved
        except Exception as e:
            logger.warning("Failed to resolve Spotify redirect for %s: %s", url, e)
    return url


def _get_spotify_metadata(url: str) -> tuple[str, str, int, str | None, str]:
    """
    Extracts title, artist, duration (seconds), cover art URL, and unique track ID from Spotify.
    Uses ultra-fast Spotify embed metadata with oEmbed fallback and picks the largest resolution cover.
    """
    clean_url = resolve_spotify_url(url)
    track_id_match = re.search(r"/track/([A-Za-z0-9]+)", clean_url)
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
                    # Pick the largest image resolution available (640x640)
                    best_img = sorted(
                        imgs,
                        key=lambda x: (x.get("maxWidth") or 0) * (x.get("maxHeight") or 0),
                        reverse=True
                    )[0]
                    cover_url = best_img.get("url")
            return title, artist, duration, cover_url, track_id
    except Exception as e:
        logger.warning("Failed to fetch Spotify embed metadata: %s", e)

    # 2. Fallback: Spotify oEmbed endpoint
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={clean_url}"
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
    """Download matching audio and convert to 320kbps MP3 with embedded HD artwork."""
    title, artist, meta_duration, cover_url, track_id = _get_spotify_metadata(url)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # 1. Download highest-resolution cover art thumbnail
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

    # 2. Multi-tier fast search queries
    search_queries = [
        f"ytsearch1:{artist} - {title}",
        f"ytsearch1:{artist} {title} audio",
        f"scsearch1:{artist} {title}",
    ]

    raw_output_template = str(DOWNLOADS_DIR / f"raw_spot_{track_id}_%(id)s.%(ext)s")
    dl_opts = {
        "format": "bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": raw_output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 12,
        "retries": 2,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"]
            }
        },
    }

    last_error = None
    raw_file_path = None
    actual_duration = meta_duration

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
                f_path = Path(filename)

                if not f_path.exists():
                    matches = list(DOWNLOADS_DIR.glob(f"raw_spot_{track_id}_{item_id}.*"))
                    if matches:
                        f_path = matches[0]
                    else:
                        continue

                raw_file_path = f_path
                actual_duration = int(dl_info.get("duration") or meta_duration or 0)
                logger.info("Downloaded raw stream: %s (%s bytes)", raw_file_path.name, raw_file_path.stat().st_size)
                break
        except Exception as q_err:
            logger.warning("Query '%s' failed: %s", q, q_err)
            last_error = q_err

    if not raw_file_path or not raw_file_path.exists():
        raise ValueError(f"امکان یافتن یا دانلود فایل صوتی این قطعه وجود ندارد: '{artist} - {title}'. ({last_error})")

    # 3. High-Quality 320kbps MP3 conversion with embedded artwork & metadata
    final_mp3_path = DOWNLOADS_DIR / f"spot_{track_id}_hq.mp3"
    ffmpeg_cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(raw_file_path),
    ]

    if thumb_path and thumb_path.exists():
        ffmpeg_cmd.extend([
            "-i", str(thumb_path),
            "-map", "0:a",
            "-map", "1:0",
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            "-id3v2_version", "3",
            "-metadata:s:v", 'title="Album cover"',
            "-metadata:s:v", 'comment="Cover (front)"',
        ])
    else:
        ffmpeg_cmd.extend([
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            "-id3v2_version", "3",
        ])

    ffmpeg_cmd.extend([
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        str(final_mp3_path),
    ])

    try:
        subprocess.run(
            ffmpeg_cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        # Remove raw download file now that 320k MP3 is ready
        safe_remove(raw_file_path)
        output_file = final_mp3_path
        logger.info("Successfully converted to 320kbps MP3 with artwork: %s", output_file.name)
    except Exception as conv_err:
        logger.warning("FFmpeg 320k conversion failed (%s), falling back to raw audio stream", conv_err)
        output_file = raw_file_path

    return SpotifyTrack(
        title=title,
        artist=artist,
        duration=actual_duration,
        file_path=output_file,
        thumbnail_path=thumb_path,
        track_id=track_id,
    )


async def download_spotify(url: str) -> SpotifyTrack:
    """Asynchronously download Spotify track in a worker thread."""
    return await asyncio.to_thread(_download_spotify_sync, url)
