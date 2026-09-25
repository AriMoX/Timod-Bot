import asyncio
import logging
from dataclasses import dataclass
from curl_cffi import requests
from urllib.parse import quote

logger = logging.getLogger(__name__)

@dataclass
class DeezerTrack:
    track_id: str
    title: str
    artist: str
    duration: int
    cover_url: str
    preview_url: str
    deezer_url: str

async def search_deezer(query: str, limit: int = 15) -> list[DeezerTrack]:
    """Search Deezer using the free public API and return tracks."""
    url = f"https://api.deezer.com/search/track?q={quote(query)}&limit={limit}"
    
    def fetch():
        # curl_cffi bypasses most generic blockers, though Deezer API is open
        resp = requests.get(url, impersonate="chrome110")
        resp.raise_for_status()
        return resp.json()
        
    try:
        data = await asyncio.to_thread(fetch)
        results = []
        if 'data' in data:
            for item in data['data']:
                try:
                    track = DeezerTrack(
                        track_id=str(item['id']),
                        title=item.get('title', 'Unknown Title'),
                        artist=item.get('artist', {}).get('name', 'Unknown Artist'),
                        duration=item.get('duration', 0),
                        cover_url=item.get('album', {}).get('cover_xl', ''),
                        preview_url=item.get('preview', ''),
                        deezer_url=item.get('link', f"https://www.deezer.com/track/{item['id']}")
                    )
                    results.append(track)
                except Exception as parse_e:
                    logger.warning("Error parsing Deezer track item: %s", parse_e)
        return results
    except Exception as e:
        logger.error("Deezer search failed for query '%s': %s", query, e)
        return []
