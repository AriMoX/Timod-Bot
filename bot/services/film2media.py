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

import sqlite3
import json
from bot.config import BASE_DIR

DB_PATH = BASE_DIR / "bot_database.db"

def _init_link_store_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS f2m_links (
                    short_id TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_f2m_links_created ON f2m_links (created_at)")
            conn.commit()
    except Exception as e:
        logger.warning("Error initializing f2m_links table: %s", e)

_init_link_store_db()

# Short ID Store for Telegram callback_data (persisted in SQLite + cached in memory)
class F2MLinkStore:
    _cache: dict[str, dict] = {}

    @classmethod
    def save_link(cls, item: dict) -> str:
        """Store link details in memory and persistent SQLite database."""
        short_id = uuid.uuid4().hex[:8]
        cls._cache[short_id] = item
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO f2m_links (short_id, data_json, created_at) VALUES (?, ?, ?)",
                    (short_id, json.dumps(item, ensure_ascii=False), time.time())
                )
                conn.commit()
        except Exception as e:
            logger.warning("Error persisting f2m_link to SQLite: %s", e)
        return short_id

    @classmethod
    def get_link(cls, short_id: str) -> Optional[dict]:
        """Fetch link from in-memory cache or SQLite database."""
        if short_id in cls._cache:
            return cls._cache[short_id]
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute("SELECT data_json FROM f2m_links WHERE short_id = ?", (short_id,))
                row = cur.fetchone()
                if row and row[0]:
                    item = json.loads(row[0])
                    cls._cache[short_id] = item
                    return item
        except Exception as e:
            logger.warning("Error retrieving f2m_link from SQLite: %s", e)
        return None


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


def _rank_results(results: list[dict], query: str) -> list[dict]:
    """Smartly rank search results so exact and best title matches appear at the top."""
    q_clean = query.lower().strip()
    q_words = set(re.findall(r'[\w\u0600-\u06FF]+', q_clean))

    def score(item: dict) -> int:
        title = item.get("title", "").lower().strip()
        t_words = set(re.findall(r'[\w\u0600-\u06FF]+', title))
        pts = 0

        # Exact match or title without trailing year
        clean_t = re.sub(r'\s*\(\d{4}\)$', '', title).strip()
        clean_t = re.sub(r'\s+\d{4}$', '', clean_t).strip()
        if clean_t == q_clean or title == q_clean:
            pts += 200
        elif clean_t.startswith(q_clean) or title.startswith(q_clean):
            pts += 100
        elif q_clean in title:
            pts += 50

        # Overlapping words
        overlap = len(q_words.intersection(t_words))
        pts += overlap * 15

        # Series vs Movie intent preference
        if any(w in q_clean for w in ['سریال', 'series', 'فصل']) and item.get("is_series"):
            pts += 40
        if any(w in q_clean for w in ['فیلم', 'movie']) and not item.get("is_series"):
            pts += 40

        return pts

    return sorted(results, key=score, reverse=True)


async def search_f2m(query: str) -> list[dict]:
    """Search movies and series on Film2Media with &type=both and smart ranking."""
    query = query.strip()
    if not query:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/",
    }

    async def _fetch_articles(search_term: str) -> list[dict]:
        search_url = f"{BASE_URL}/?s={urllib.parse.quote(search_term)}&type=both"
        items = []
        try:
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=20.0, connect=8.0, sock_read=15.0)) as resp:
                    if resp.status != 200:
                        logger.warning("F2M search returned HTTP %s for %s", resp.status, search_term)
                        return []
                    html = await resp.text(errors="ignore")

            articles = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
            seen_urls = set()

            for art in articles:
                link_m = re.search(r'<a[^>]+href=["\'](https://www\.f2mc\.top/[^"\']+)["\'][^>]*class=["\'][^"\']*stretched-link[^"\']*["\']', art, re.I)
                if not link_m:
                    link_m = re.search(r'class=["\'][^"\']*stretched-link[^"\']*["\'][^>]*href=["\'](https://www\.f2mc\.top/[^"\']+)["\']', art, re.I)
                if not link_m:
                    valid_links = re.findall(r'<a\s+[^>]*href=["\'](https://www\.f2mc\.top/[^"\']+)["\'][^>]*>(.*?)</a>', art, re.DOTALL)
                    for h, t in valid_links:
                        if not any(x in h for x in ["genres", "category", "tag", "wp-content", "#", "page/"]):
                            link_m = type("Obj", (), {"group": lambda self, n: h})()
                            break

                if not link_m:
                    continue

                page_url = link_m.group(1)
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)

                h2_m = re.search(r'<h2[^>]*class=["\'][^"\']*entry-title[^"\']*["\'][^>]*>(.*?)</h2>', art, re.DOTALL | re.I)
                if not h2_m:
                    h2_m = re.search(r'<h2[^>]*>(.*?)</h2>', art, re.DOTALL | re.I)
                clean_title = " ".join(re.sub(r"<[^>]+>", " ", h2_m.group(1)).split()).strip() if h2_m else ""

                img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', art)
                poster = img_m.group(1) if img_m else None

                year_m = re.search(r'#icon-calendar.*?(\b(?:19\d\d|20\d\d)\b)', art, re.DOTALL)
                if not year_m:
                    year_m = re.search(r"\b(19\d\d|20\d\d)\b", art)
                year = year_m.group(1) if year_m else ""

                imdb_m = re.search(r'#icon-imdb.*?<strong[^>]*>([\d\.]+)</strong>', art, re.DOTALL)
                if not imdb_m:
                    imdb_m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", art)
                rating = imdb_m.group(1) if imdb_m else ""

                is_series = "/series/" in page_url or "سریال" in art or "سریال" in clean_title

                items.append({
                    "title": clean_title or "فیلم / سریال",
                    "url": page_url,
                    "poster": poster,
                    "year": year,
                    "rating": rating,
                    "is_series": is_series,
                })
        except Exception as e:
            logger.exception("Error searching Film2Media for %s: %s", search_term, e)
        return items

    results = await _fetch_articles(query)

    clean_q = re.sub(r"^(?:فیلم|سریال|فصل)\s+", "", query, flags=re.I).strip()
    if not results and clean_q and clean_q != query:
        results = await _fetch_articles(clean_q)

    if results:
        results = _rank_results(results, query)

    return results


async def download_poster_bytes(poster_url: str) -> Optional[bytes]:
    """Download poster image bytes directly to bypass Telegram server fetch blocks."""
    if not poster_url or not poster_url.startswith("http"):
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": f"{BASE_URL}/",
        }
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
            async with session.get(poster_url, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 1000:
                        return data
    except Exception as e:
        logger.warning("Error downloading poster bytes from %s: %s", poster_url, e)
    return None


_DETAILS_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600  # 1 hour


async def get_movie_details(url: str, search_poster: Optional[str] = None) -> Optional[dict]:
    """Fetch and parse movie or series detail page with download links."""
    now = time.time()
    if url in _DETAILS_CACHE:
        cached_time, cached_data = _DETAILS_CACHE[url]
        if now - cached_time < _CACHE_TTL:
            res_copy = dict(cached_data)
            if search_poster and not res_copy.get("poster"):
                res_copy["poster"] = search_poster
            return res_copy

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/",
    }

    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=45.0, connect=10.0, sock_read=35.0)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")

        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
        title = " ".join(re.sub(r"<[^>]+>", " ", title_m.group(1)).split()).strip() if title_m else "فیلم / سریال"

        # Poster detection: check multiple sources
        poster = None
        poster_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*(?:poster|thumb|wp-post-image|attachment)[^"\']*["\']', html, re.IGNORECASE)
        if poster_m:
            poster = poster_m.group(1)

        if not poster:
            og_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if og_m and "public_html" not in og_m.group(1) and any(og_m.group(1).lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                poster = og_m.group(1)

        if not poster:
            imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)
            for img in imgs:
                if "wp-content/uploads" in img and any(img.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]) and not any(bad in img.lower() for bad in ["player", "icon", "logo"]):
                    poster = img
                    break

        if not poster and search_poster:
            poster = search_poster

        story = ""
        story_m = re.search(r'<div[^>]*class=["\'][^"\']*(?:story|summary|plot|entry-content)[^"\']*["\'][^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
        if story_m:
            story = " ".join(re.sub(r"<[^>]+>", " ", story_m.group(1)).split()).strip()
            if len(story) > 350:
                story = story[:345] + "..."

        # Direct media files
        all_media_links = re.findall(r'href=["\']([^"\']+\.(?:mkv|mp4)[^"\']*)["\']', html)

        # Detect series accurately: /series/ in URL or S01E01 media links
        has_episodes = any(bool(re.search(r'[./]S\d+E\d+', u, re.I)) for u in all_media_links)
        is_series = "/series/" in url or ("سریال" in title and has_episodes) or has_episodes

        movie_downloads = []
        seen_urls = set()

        # 1. Parse from structured <li> elements
        dl_matches = re.findall(r'(<li[^>]*>(?:(?!<li).)*?href=["\']([^"\']+\.(?:mkv|mp4)[^"\']*)["\'].*?</li>)', html, re.DOTALL | re.IGNORECASE)
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

        # 2. If movie_downloads is empty and it is NOT a series, parse all media links directly
        if not movie_downloads and not is_series:
            for m_url in all_media_links:
                if m_url in seen_urls:
                    continue
                seen_urls.add(m_url)

                # Infer quality from filename
                q = "720p"
                if "1080p" in m_url:
                    q = "1080p BluRay" if "bluray" in m_url.lower() else "1080p WEB-DL"
                elif "720p" in m_url:
                    q = "720p BluRay" if "bluray" in m_url.lower() else "720p WEB-DL"
                elif "480p" in m_url:
                    q = "480p"
                elif "2160p" in m_url or "4k" in m_url.lower():
                    q = "4K 2160p"

                is_dub = "DUB" in m_url or "dubbed" in m_url.lower()
                t_str = "دوبله فارسی" if is_dub else "زیرنویس فارسی"

                movie_downloads.append({
                    "quality": q,
                    "encoder": "",
                    "type": t_str,
                    "url": m_url,
                    "size": None,
                })

        # Series seasons and episodes
        series_seasons = {}
        if is_series:
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

                    is_dub = bool(re.search(r'(?:[._/-]dub(?:bed)?|/dub/|دوبله)', m_url, re.IGNORECASE))
                    version_str = "دوبله فارسی" if is_dub else "زیرنویس فارسی"
                    q_key = f"{q_tag} - {version_str}"

                    if q_key not in series_seasons[s_key]:
                        series_seasons[s_key][q_key] = []

                    series_seasons[s_key][q_key].append({
                        "episode": e_num,
                        "url": m_url,
                        "size": None,
                        "quality": q_tag,
                        "version": version_str,
                        "is_dub": is_dub,
                    })

            # Sort episodes numerically within each season and quality
            for s_k, q_map in series_seasons.items():
                for q_t, ep_arr in q_map.items():
                    ep_arr.sort(key=lambda x: x.get("episode", 0))

        # Pre-fetch sizes for movie downloads concurrently
        if movie_downloads:
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), headers=headers) as size_session:
                    tasks = [fetch_link_size(size_session, item["url"]) for item in movie_downloads[:12]]
                    sizes = await asyncio.gather(*tasks, return_exceptions=True)
                    for item, sz in zip(movie_downloads[:12], sizes):
                        if isinstance(sz, str):
                            item["size"] = sz
            except Exception as se:
                logger.warning("Error pre-fetching link sizes: %s", se)

        # Register all download links into F2MLinkStore with short keys
        for item in movie_downloads:
            item["short_id"] = F2MLinkStore.save_link({
                "title": title,
                "movie_url": url,
                "quality": item["quality"],
                "encoder": item["encoder"],
                "type": item["type"],
                "url": item["url"],
                "size": item.get("size"),
            })

        result = {
            "title": title,
            "url": url,
            "poster": poster,
            "story": story,
            "is_series": is_series,
            "downloads": movie_downloads,
            "series_seasons": series_seasons,
        }
        _DETAILS_CACHE[url] = (time.time(), result)
        return result

    except Exception as e:
        logger.exception("Error getting movie details for %s: %s", url, e)
        return None
