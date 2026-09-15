import asyncio
import logging
from dataclasses import dataclass
import yt_dlp

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    id: str
    title: str
    artist: str
    duration: int
    thumbnail: str | None
    url: str


def _search_music_sync(query: str, limit: int = 5) -> list[SearchResult]:
    """Fast synchronous metadata search using yt-dlp flat extraction."""
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    results = []
    search_query = f"ytsearch{limit}:{query}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(search_query, download=False)
            if not res or not res.get("entries"):
                return results

            for entry in res["entries"]:
                if not entry:
                    continue
                video_id = entry.get("id")
                if not video_id:
                    continue

                raw_title = entry.get("title") or "Unknown Track"
                raw_uploader = entry.get("uploader") or entry.get("channel") or "Artist"

                # Parse "Artist - Title" if present
                if " - " in raw_title:
                    parts = raw_title.split(" - ", 1)
                    artist_raw = parts[0].strip()
                    title_clean = parts[1].strip()
                else:
                    artist_raw = raw_uploader
                    title_clean = raw_title

                # Format artists: replace commas with " & "
                artist_parts = [p.strip() for p in artist_raw.split(",") if p.strip()]
                artist = " & ".join(artist_parts) if artist_parts else artist_raw

                duration = int(entry.get("duration") or 0)
                thumbnail = entry.get("thumbnail")
                page_url = f"https://www.youtube.com/watch?v={video_id}"

                results.append(
                    SearchResult(
                        id=video_id,
                        title=title_clean,
                        artist=artist,
                        duration=duration,
                        thumbnail=thumbnail,
                        url=page_url,
                    )
                )
    except Exception as e:
        logger.exception("Error during music search for query '%s': %s", query, e)

    return results


async def search_music(query: str, limit: int = 5) -> list[SearchResult]:
    """Asynchronously search music tracks in a worker thread."""
    return await asyncio.to_thread(_search_music_sync, query, limit)
