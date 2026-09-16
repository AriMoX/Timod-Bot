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
    """Download matching audio and package with crystal clear 320kbps audio and HD artwork."""
    title, artist, meta_duration, cover_url, track_id = _get_spotify_metadata(url)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # 1. Download highest-resolution cover art thumbnail (640x640)
    thumb_path = None
    if cover_url:
        try:
            thumb_path = DOWNLOADS_DIR / f"spot_thumb_{track_id}.jpg"
            img_req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=8) as img_resp:
                thumb_path.write_bytes(img_resp.read())
            logger.info("Downloaded Spotify HD cover: %s (%s bytes)", thumb_path.name, thumb_path.stat().st_size)
        except Exception as e:
            logger.warning("Failed to download Spotify cover thumbnail: %s", e)
            thumb_path = None

    # Base yt-dlp configuration
    base_opts = {
        "format": "bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / f"raw_spot_{track_id}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "retries": 1,
        "js_runtimes": {"node": {}},
    }

    raw_file_path = None
    actual_duration = meta_duration
    all_errors = []

    # 2. Tier 1: Fast YouTube Search with Node.js JS solver (8s max)
    yt_query = f"ytsearch1:{artist} - {title}"
    logger.info("Searching YouTube for Spotify track: %s", yt_query)
    try:
        with yt_dlp.YoutubeDL(base_opts) as yt_ydl:
            info = yt_ydl.extract_info(yt_query, download=True)
            if info:
                entries = info.get("entries") or [info]
                for entry in entries:
                    if not entry:
                        continue
                    item_id = entry.get("id")
                    c_dur = entry.get("duration") or 0
                    matches = list(DOWNLOADS_DIR.glob(f"raw_spot_{track_id}_{item_id}.*"))
                    if matches and matches[0].exists():
                        raw_file_path = matches[0]
                        actual_duration = int(c_dur or meta_duration or 0)
                        logger.info("Found YouTube audio candidate: %s (%s bytes)", raw_file_path.name, raw_file_path.stat().st_size)
                        break
    except Exception as yt_err:
        logger.warning("YouTube query failed: %s", yt_err)
        all_errors.append(f"YouTube: {yt_err}")

    # 3. Tier 2: Smart SoundCloud Search with DRM preview skip & duration proximity
    if not raw_file_path or not raw_file_path.exists():
        sc_query = f"scsearch5:{artist} {title}"
        logger.info("Falling back to SoundCloud search: %s", sc_query)
        sc_search_opts = {
            **base_opts,
            "extract_flat": "in_playlist",
        }
        try:
            with yt_dlp.YoutubeDL(sc_search_opts) as sc_ydl:
                sc_info = sc_ydl.extract_info(sc_query, download=False)
                candidates = sc_info.get("entries") or [sc_info]

                # Filter out short 30s previews (SoundCloud Go+ DRM snippets)
                valid_candidates = []
                for entry in candidates:
                    if not entry:
                        continue
                    c_dur = entry.get("duration") or 0
                    if meta_duration > 60 and c_dur <= 35:
                        logger.info("Skipping short SoundCloud DRM preview (%ss) for %s", c_dur, entry.get("id"))
                        continue
                    valid_candidates.append(entry)

                # Prioritize candidate closest in duration to the Spotify original track
                if meta_duration > 0:
                    valid_candidates.sort(key=lambda x: abs((x.get("duration") or 0) - meta_duration))

                for entry in valid_candidates:
                    cand_url = entry.get("webpage_url") or entry.get("url")
                    if not cand_url:
                        continue
                    c_dur = entry.get("duration") or 0
                    try:
                        logger.info("Trying SoundCloud candidate: %s (%ss)", entry.get("title"), c_dur)
                        with yt_dlp.YoutubeDL(base_opts) as sc_dl:
                            sc_dl.extract_info(cand_url, download=True)
                        cand_id = entry.get("id")
                        matches = list(DOWNLOADS_DIR.glob(f"raw_spot_{track_id}_{cand_id}.*"))
                        if matches and matches[0].exists():
                            raw_file_path = matches[0]
                            actual_duration = int(c_dur or meta_duration or 0)
                            logger.info("Downloaded SoundCloud candidate: %s (%s bytes)", raw_file_path.name, raw_file_path.stat().st_size)
                            break
                    except Exception as cand_err:
                        logger.warning("SoundCloud candidate failed (%s), trying next", cand_err)
                        all_errors.append(f"SC candidate {entry.get('id')}: {cand_err}")
                        continue
        except Exception as sc_err:
            logger.warning("SoundCloud search '%s' failed: %s", sc_query, sc_err)
            all_errors.append(f"SoundCloud search: {sc_err}")

    if not raw_file_path or not raw_file_path.exists():
        err_msg = " | ".join(all_errors)
        raise ValueError(f"امکان یافتن یا دانلود فایل صوتی این قطعه وجود ندارد: '{artist} - {title}'. ({err_msg})")

    # 4. Master 320kbps MP3 encoding + HD Cover Art embedding (ID3v2.3)
    final_audio_path = DOWNLOADS_DIR / f"spot_{track_id}.mp3"
    ffmpeg_cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(raw_file_path),
    ]
    if thumb_path and thumb_path.exists():
        ffmpeg_cmd.extend([
            "-i", str(thumb_path),
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            "-map", "0:a:0",
            "-map", "1:v:0",
            "-c:v", "copy",
            "-id3v2_version", "3",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            str(final_audio_path),
        ])
    else:
        ffmpeg_cmd.extend([
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            "-map", "0:a:0",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            str(final_audio_path),
        ])

    try:
        subprocess.run(
            ffmpeg_cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        safe_remove(raw_file_path)
        output_file = final_audio_path
        logger.info("High quality 320kbps MP3 produced with embedded cover: %s (%s bytes)", output_file.name, output_file.stat().st_size)
    except Exception as conv_err:
        logger.warning("FFmpeg 320k conversion failed (%s), using raw audio file", conv_err)
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

