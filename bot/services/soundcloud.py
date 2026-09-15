import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import yt_dlp

from bot.config import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


@dataclass
class SoundcloudTrack:
    title: str
    artist: str
    duration: int
    file_path: Path
    thumbnail_url: str | None = None


def _download_track_sync(url: str) -> SoundcloudTrack:
    """Synchronous worker that runs inside yt-dlp to download the track."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Could not extract track information from SoundCloud.")

        if "entries" in info:
            info = info["entries"][0]

        filename = ydl.prepare_filename(info)
        file_path = Path(filename)

        if not file_path.exists():
            # Check if ext differed (e.g., .m4a / .mp3)
            matching = list(DOWNLOADS_DIR.glob(f"{info.get('id')}.*"))
            if matching:
                file_path = matching[0]
            else:
                raise FileNotFoundError(f"Downloaded file not found for ID: {info.get('id')}")

        title = info.get("title") or "SoundCloud Track"
        uploader = info.get("uploader") or info.get("artist") or "SoundCloud"

        # If title contains "Artists - Title", extract actual artists
        if " - " in title:
            title_parts = title.split(" - ", 1)
            artist_raw = title_parts[0].strip()
        else:
            artist_raw = uploader

        # Replace commas between artists with " & "
        artist_parts = [p.strip() for p in artist_raw.split(",") if p.strip()]
        artist = " & ".join(artist_parts) if artist_parts else artist_raw

        duration = int(info.get("duration") or 0)
        thumbnail = info.get("thumbnail")

        return SoundcloudTrack(
            title=title,
            artist=artist,
            duration=duration,
            file_path=file_path,
            thumbnail_url=thumbnail,
        )


async def download_soundcloud(url: str) -> SoundcloudTrack:
    """Download SoundCloud audio asynchronously in a thread pool."""
    return await asyncio.to_thread(_download_track_sync, url)
