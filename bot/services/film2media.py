import re
import uuid
import time
import logging
import asyncio
import urllib.parse
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.f2mc.top"

# Short ID Store for Telegram callback_data (max 64 bytes)
class F2MLinkStore:
    _data: dict[str, dict] = {}
    _timestamps: dict[str, float] = {}

    @classmethod
    def save_link(cls, item: dict) -> str:
        """Store link details and return an 8-character short key."""
        short_id = uuid.uuid4().hex[:8]
        cls._data[short_id] = item
        cls._timestamps[short_id] = time.time()
        cls._cleanup()
        return short_id

    @classmethod
    def get_link(cls, short_id: str) -> Optional[dict]:
        return cls._data.get(short_id)

    @classmethod
    def _cleanup(cls):
        now = time.time()
        expired = [k for k, t in cls._timestamps.items() if now - t > 7200]
        for k in expired:
            cls._data.pop(k, None)
            cls._timestamps.pop(k, None)


def _format_size(bytes_num: int) -> str:
    """Format bytes into readable MB or GB string."""
    mb = bytes_num / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


async def fetch_link_size(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Fetch Content-Length via HTTP HEAD request."""
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=2.5), allow_redirects=True) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > 100000:
                return _format_size(int(cl))
    except Exception:
        pass
    return None


async def search_f2m(query: str) -> list[dict]:
    """Search movies and series on Film2Media."""
    query = query.strip()
    if not query:
        return []

    search_url = f"{BASE_URL}/?s={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/",
    }

    results = []
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
            async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                if resp.status != 200:
                    logger.warning("F2M search returned HTTP %s for %s", resp.status, query)
                    return []
                html = await resp.text(errors="ignore")

        articles = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
        seen_urls = set()

        for art in articles:
            valid_links = re.findall(r'<a\s+[^>]*href=["\'](https://www\.f2mc\.top/[^"\']+)["\'][^>]*>(.*?)</a>', art, re.DOTALL)
            page_url = None
            raw_title = ""
            for h, t in valid_links:
                if not any(x in h for x in ["genres", "category", "tag", "wp-content", "#", "page/"]):
                    page_url = h
                    raw_title = t
                    break

            if not page_url or page_url in seen_urls:
                continue
            seen_urls.add(page_url)

            clean_title = " ".join(re.sub(r"<[^>]+>", " ", raw_title).split()).strip()
            img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', art)
            poster = img_m.group(1) if img_m else None

            snippet = " ".join(re.sub(r"<[^>]+>", " ", art).split())
            year_m = re.search(r"\b(19\d\d|20\d\d)\b", snippet)
            year = year_m.group(1) if year_m else ""

            imdb_m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", snippet)
            rating = imdb_m.group(1) if imdb_m else ""
            is_series = "/series/" in page_url or "سریال" in snippet or "فصل" in snippet

            results.append({
                "title": clean_title or "فیلم / سریال",
                "url": page_url,
                "poster": poster,
                "year": year,
                "rating": rating,
                "is_series": is_series,
                "snippet": snippet[:150],
            })

    except Exception as e:
        logger.exception("Error searching Film2Media for %s: %s", query, e)

    return results


async def get_movie_details(url: str) -> Optional[dict]:
    """Fetch and parse movie or series detail page with download links."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/",
    }

    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")

        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
        title = " ".join(re.sub(r"<[^>]+>", " ", title_m.group(1)).split()).strip() if title_m else "فیلم / سریال"

        poster_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*(?:poster|thumb|wp-post-image)[^"\']*["\']', html, re.IGNORECASE)
        if not poster_m:
            poster_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        poster = poster_m.group(1) if poster_m else None

        story = ""
        story_m = re.search(r'<div[^>]*class=["\'][^"\']*(?:story|summary|plot|entry-content)[^"\']*["\'][^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
        if story_m:
            story = " ".join(re.sub(r"<[^>]+>", " ", story_m.group(1)).split()).strip()
            if len(story) > 350:
                story = story[:345] + "..."

        is_series = "/series/" in url or "سریال" in title or "فصل" in html

        movie_downloads = []
        dl_matches = re.findall(r'(<li[^>]*>(?:(?!<li).)*?href=["\']([^"\']+\.(?:mkv|mp4)[^"\']*)["\'].*?</li>)', html, re.DOTALL | re.IGNORECASE)

        seen_urls = set()
        for li_html, media_url in dl_matches:
            if media_url in seen_urls:
                continue
            seen_urls.add(media_url)

            q_m = re.search(r'کیفیت\s*:\s*</span>\s*<span[^>]*dir=["\']ltr["\'][^>]*>([^<]+)</span>', li_html, re.IGNORECASE)
            if not q_m:
                q_m = re.search(r'کیفیت\s*:\s*([^<]+)', li_html, re.IGNORECASE)
            quality = q_m.group(1).strip() if q_m else "کیفیت اصلی"

            enc_m = re.search(r'انکودر\s*:\s*</span>\s*([^<]+)', li_html, re.IGNORECASE)
            encoder = enc_m.group(1).strip() if enc_m else ""
            if encoder.lower() in ("unknown", "نامشخص", ""):
                encoder = ""
            else:
                encoder = f"({encoder})"

            is_dub = "DUB" in media_url or "dubbed" in media_url.lower() or "دوبله" in li_html
            type_str = "دوبله فارسی" if is_dub else "زیرنویس فارسی"

            movie_downloads.append({
                "quality": quality,
                "encoder": encoder,
                "type": type_str,
                "url": media_url,
                "size": None,
            })

        series_seasons = {}
        if is_series:
            all_media_links = re.findall(r'href=["\']([^"\']+\.(?:mkv|mp4)[^"\']*)["\']', html)
            for m_url in all_media_links:
                ep_match = re.search(r'[./]S(\d+)E(\d+)', m_url, re.IGNORECASE)
                if ep_match:
                    s_num = int(ep_match.group(1))
                    e_num = int(ep_match.group(2))
                    s_key = f"فصل {s_num}"
                    if s_key not in series_seasons:
                        series_seasons[s_key] = {}
                    
                    q_tag = "720p"
                    if "1080p" in m_url:
                        q_tag = "1080p"
                    elif "480p" in m_url:
                        q_tag = "480p"
                    elif "2160p" in m_url or "4k" in m_url.lower():
                        q_tag = "4K"

                    if q_tag not in series_seasons[s_key]:
                        series_seasons[s_key][q_tag] = []
                    
                    series_seasons[s_key][q_tag].append({
                        "episode": e_num,
                        "url": m_url,
                        "size": None,
                    })

        # Pre-fetch sizes for the top movie downloads concurrently (up to 8 items, 2.5s max)
        if movie_downloads:
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as size_session:
                    tasks = [fetch_link_size(size_session, item["url"]) for item in movie_downloads[:8]]
                    sizes = await asyncio.gather(*tasks, return_exceptions=True)
                    for item, sz in zip(movie_downloads[:8], sizes):
                        if isinstance(sz, str):
                            item["size"] = sz
            except Exception as se:
                logger.warning("Error pre-fetching link sizes: %s", se)

        # Register all download links into F2MLinkStore with short keys
        for item in movie_downloads:
            item["short_id"] = F2MLinkStore.save_link({
                "title": title,
                "quality": item["quality"],
                "encoder": item["encoder"],
                "type": item["type"],
                "url": item["url"],
                "size": item.get("size"),
            })

        return {
            "title": title,
            "url": url,
            "poster": poster,
            "story": story,
            "is_series": is_series,
            "downloads": movie_downloads,
            "series_seasons": series_seasons,
        }

    except Exception as e:
        logger.exception("Error getting movie details for %s: %s", url, e)
        return None
