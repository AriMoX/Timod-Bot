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


def _get_spotify_metadata(url: str) -> tuple[str, str, str | None, str]:
    """
    Extracts title, artist, cover art URL, and unique track ID from Spotify.
    Uses public Spotify oEmbed and Open Graph metadata without requiring API keys.
    """
    track_id_match = re.search(r"/track/([A-Za-z0-9]+)", url)
    track_id = track_id_match.group(1) if track_id_match else "track"

    title = "Spotify Track"
    artist = "Unknown Artist"
    cover_url = None

    # 1. Fetch Spotify oEmbed
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={url}"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("title"):
                title = data["title"]
            if data.get("thumbnail_url"):
                cover_url = data["thumbnail_url"]
    except Exception as e:
        logger.warning("Failed to fetch Spotify oEmbed: %s", e)

    # 2. Fetch track page HTML for accurate artist name
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Check og:description (format: "Artist · Album · Song · Year")
        og_desc = re.search(r'property="og:description"\s+content="([^"]+)"', html)
        if og_desc:
            parts = [p.strip() for p in og_desc.group(1).split("·")]
            if parts and parts[0]:
                artist = parts[0]
        else:
            # Fallback title tag: "Title - song and lyrics by Artist | Spotify"
            title_tag = re.search(r"<title>(.*?)</title>", html)
            if title_tag:
                m = re.search(r"song and lyrics by (.*?)\s*\|", title_tag.group(1))
                if m:
                    artist = m.group(1).strip()
    except Exception as e:
        logger.warning("Failed to fetch Spotify HTML metadata: %s", e)

    # Format multiple artists: replace commas with " & "
    artist_parts = [p.strip() for p in re.split(r"\s*,\s*", artist) if p.strip()]
    formatted_artist = " & ".join(artist_parts) if artist_parts else artist

    return title, formatted_artist, cover_url, track_id


def _download_spotify_sync(url: str) -> SpotifyTrack:
    """Download the matching track audio via multi-candidate search and return SpotifyTrack."""
    title, artist, cover_url, track_id = _get_spotify_metadata(url)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Download cover art thumbnail if available
    thumb_path = None
    if cover_url:
        try:
            thumb_path = DOWNLOADS_DIR / f"spot_thumb_{track_id}.jpg"
            img_req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=10) as img_resp:
                thumb_path.write_bytes(img_resp.read())
        except Exception as e:
            logger.warning("Failed to download Spotify cover thumbnail: %s", e)
            thumb_path = None

    # Search queries to try in order (flat search first to avoid downloading unplayable candidates)
    search_queries = [
        f"ytsearch5:{artist} {title}",
        f"ytsearch5:{artist} {title} audio",
        f"scsearch3:{artist} {title}",
    ]

    search_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }

    dl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / "spot_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        },
    }

    last_error = None
    for q in search_queries:
        logger.info("Searching audio candidates for Spotify track: %s", q)
        try:
            with yt_dlp.YoutubeDL(search_opts) as s_ydl:
                s_res = s_ydl.extract_info(q, download=False)
                entries = s_res.get("entries") or []

            if not entries:
                continue

            with yt_dlp.YoutubeDL(dl_opts) as dl_ydl:
                for entry in entries:
                    if not entry:
                        continue
                    item_id = entry.get("id")
                    item_url = entry.get("url") or (f"https://www.youtube.com/watch?v={item_id}" if "ytsearch" in q else None)
                    if not item_url:
                        continue

                    try:
                        logger.info("Attempting download for candidate %s (%s)...", item_id, entry.get("title"))
                        dl_info = dl_ydl.extract_info(item_url, download=True)
                        filename = dl_ydl.prepare_filename(dl_info)
                        file_path = Path(filename)

                        if not file_path.exists():
                            matches = list(DOWNLOADS_DIR.glob(f"spot_{item_id}.*"))
                            if matches:
                                file_path = matches[0]
                            else:
                                continue

                        duration = int(dl_info.get("duration") or 0)
                        logger.info("Successfully downloaded track: %s", file_path.name)
                        return SpotifyTrack(
                            title=title,
                            artist=artist,
                            duration=duration,
                            file_path=file_path,
                            thumbnail_path=thumb_path,
                        )
                    except Exception as item_err:
                        logger.warning("Candidate %s failed: %s", item_id, item_err)
                        last_error = item_err
                        continue
        except Exception as q_err:
            logger.warning("Search query '%s' failed: %s", q, q_err)
            last_error = q_err

    raise ValueError(f"Could not find matching playable audio for '{artist} - {title}'. ({last_error})")


async def download_spotify(url: str) -> SpotifyTrack:
    """Asynchronously download Spotify track in a worker thread."""
    return await asyncio.to_thread(_download_spotify_sync, url)
