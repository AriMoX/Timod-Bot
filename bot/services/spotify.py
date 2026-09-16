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


@dataclass
class SpotifyTrackMetadata:
    title: str
    artist: str
    duration: int
    cover_url: str | None
    track_id: str


@dataclass
class SpotifyAlbum:
    name: str
    artist: str
    cover_url: str | None
    tracks: list[SpotifyTrackMetadata]
    album_id: str
    is_playlist: bool = False


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


def _clean_title(title: str) -> str:
    """Removes video/audio tags and feat brackets from song title for cleaner search matching."""
    cleaned = re.sub(
        r"\s*[\(\[](?:official|audio|video|lyrics|hd|4k|remastered|explicit)[\)\]]",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s*[\(\[](?:feat|ft)\.?\s+[^)\]]+[\)\]]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or title


def _generate_search_queries(artist: str, title: str) -> list[str]:
    """Generates clean, prioritized search queries to maximize match rate on music platforms."""
    clean_t = _clean_title(title)

    # Extract primary artist (first name before &, comma, feat, ft)
    primary_artist = re.split(r"[,&]|\bfeat\b|\bft\b", artist, flags=re.IGNORECASE)[0].strip()

    # Clean all artists (replace & and comma with space)
    clean_all_artists = re.sub(r"[,&]|\bfeat\b|\bft\b", " ", artist).strip()
    clean_all_artists = re.sub(r"\s+", " ", clean_all_artists)

    queries = []
    # 1. Primary artist + clean title (Highest accuracy on SoundCloud / YouTube)
    if primary_artist:
        queries.append(f"{primary_artist} {clean_t}")
        queries.append(f"{clean_t} {primary_artist}")

    # 2. All artists + clean title
    if clean_all_artists and clean_all_artists.lower() != primary_artist.lower():
        queries.append(f"{clean_all_artists} {clean_t}")

    # 3. Clean title alone
    if clean_t:
        queries.append(clean_t)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for q in queries:
        q_norm = q.lower().strip()
        if q_norm and q_norm not in seen:
            seen.add(q_norm)
            deduped.append(q)

    return deduped


def _get_spotify_metadata(url_or_id: str) -> SpotifyTrackMetadata:
    """
    Extracts title, artist, duration (seconds), cover art URL, and unique track ID from Spotify track URL or ID.
    Uses ultra-fast Spotify embed metadata with oEmbed fallback.
    """
    raw_str = (url_or_id or "").strip()
    if re.match(r"^[A-Za-z0-9]{15,30}$", raw_str):
        track_id = raw_str
        clean_url = f"https://open.spotify.com/track/{track_id}"
    else:
        clean_url = resolve_spotify_url(raw_str)
        track_id_match = re.search(r"/track/([A-Za-z0-9]+)", clean_url)
        track_id = track_id_match.group(1) if track_id_match else "track"

    title = "Spotify Track"
    artist = "Unknown Artist"
    duration = 0
    cover_url = None

    # 1. Primary: Ultra-lightweight Spotify embed page (~10KB JSON)
    try:
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
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
                    best_img = sorted(
                        imgs,
                        key=lambda x: (x.get("maxWidth") or 0) * (x.get("maxHeight") or 0),
                        reverse=True
                    )[0]
                    cover_url = best_img.get("url")
            return SpotifyTrackMetadata(title=title, artist=artist, duration=duration, cover_url=cover_url, track_id=track_id)
    except Exception as e:
        logger.warning("Failed to fetch Spotify track embed metadata: %s", e)

    # 2. Fallback: Spotify oEmbed endpoint
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={clean_url}"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("title"):
                title = data["title"]
            if data.get("thumbnail_url"):
                cover_url = data["thumbnail_url"]
    except Exception as e:
        logger.warning("Failed to fetch Spotify oEmbed: %s", e)

    return SpotifyTrackMetadata(title=title, artist=artist, duration=duration, cover_url=cover_url, track_id=track_id)


get_spotify_track_metadata = _get_spotify_metadata


def _get_spotify_album_sync(url: str) -> SpotifyAlbum:
    """Synchronously extract album or playlist metadata and all tracks from Spotify embed page."""
    clean_url = resolve_spotify_url(url)
    m = re.search(r"/(album|playlist)/([A-Za-z0-9]+)", clean_url)
    if not m:
        raise ValueError("لینک وارد شده مربوط به آلبوم یا پلی‌لیست معتبر اسپاتیفای نیست.")

    entity_type = m.group(1)
    entity_id = m.group(2)
    is_playlist = entity_type == "playlist"

    embed_url = f"https://open.spotify.com/embed/{entity_type}/{entity_id}"
    req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")

    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
    if not match:
        raise ValueError("اطلاعات آلبوم در صفحه اسپاتیفای یافت نشد.")

    data = json.loads(match.group(1))
    entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})

    album_name = entity.get("name") or ("پلی‌لیست اسپاتیفای" if is_playlist else "آلبوم اسپاتیفای")
    album_artist = entity.get("subtitle") or "Various Artists"

    # Cover image
    cover_url = None
    visual = entity.get("visualIdentity", {})
    if visual and "image" in visual:
        imgs = visual["image"]
        if imgs:
            best_img = sorted(
                imgs,
                key=lambda x: (x.get("maxWidth") or 0) * (x.get("maxHeight") or 0),
                reverse=True
            )[0]
            cover_url = best_img.get("url")

    # Track list
    raw_tracks = entity.get("trackList", [])
    tracks: list[SpotifyTrackMetadata] = []
    for item in raw_tracks:
        if not item:
            continue
        t_uri = item.get("uri") or ""
        t_id = t_uri.split(":")[-1] if ":" in t_uri else f"track_{len(tracks)}"
        t_title = item.get("title") or "Unknown Track"
        t_artist = item.get("subtitle") or album_artist
        t_duration = int((item.get("duration") or 0) / 1000)

        tracks.append(
            SpotifyTrackMetadata(
                title=t_title,
                artist=t_artist,
                duration=t_duration,
                cover_url=cover_url,
                track_id=t_id,
            )
        )

    return SpotifyAlbum(
        name=album_name,
        artist=album_artist,
        cover_url=cover_url,
        tracks=tracks,
        album_id=entity_id,
        is_playlist=is_playlist,
    )


async def get_spotify_album(url: str) -> SpotifyAlbum:
    """Asynchronously retrieve Spotify album / playlist metadata."""
    return await asyncio.to_thread(_get_spotify_album_sync, url)


def _download_spotify_track_meta_sync(meta: SpotifyTrackMetadata, fallback_cover: str | None = None) -> SpotifyTrack:
    """Synchronously download and encode a track based on its metadata."""
    title = meta.title
    artist = meta.artist
    meta_duration = meta.duration
    cover_url = meta.cover_url or fallback_cover
    track_id = meta.track_id
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # 1. Download cover art thumbnail
    thumb_path = None
    if cover_url:
        try:
            thumb_path = DOWNLOADS_DIR / f"spot_thumb_{track_id}.jpg"
            img_req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=8) as img_resp:
                thumb_path.write_bytes(img_resp.read())
            logger.info("Downloaded Spotify cover: %s (%s bytes)", thumb_path.name, thumb_path.stat().st_size)
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

    queries = _generate_search_queries(artist, title)
    logger.info("Generated %s search queries for '%s - %s': %s", len(queries), artist, title, queries)

    # 2. Priority Tier 1: Smart SoundCloud search with multi-query variations & duration proximity
    sc_search_opts = {
        **base_opts,
        "extract_flat": "in_playlist",
    }

    for q in queries:
        sc_query = f"scsearch5:{q}"
        logger.info("Searching SoundCloud with query: %s", sc_query)
        try:
            with yt_dlp.YoutubeDL(sc_search_opts) as sc_ydl:
                sc_info = sc_ydl.extract_info(sc_query, download=False)
                candidates = sc_info.get("entries") or [sc_info]

                valid_candidates = []
                for entry in candidates:
                    if not entry:
                        continue
                    c_dur = entry.get("duration") or 0
                    if meta_duration > 60 and c_dur <= 35:
                        continue
                    valid_candidates.append(entry)

                if meta_duration > 0:
                    valid_candidates.sort(key=lambda x: abs((x.get("duration") or 0) - meta_duration))

                for entry in valid_candidates:
                    c_dur = entry.get("duration") or 0
                    cand_url = entry.get("webpage_url") or entry.get("url")
                    if not cand_url:
                        continue
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
                        all_errors.append(f"SC candidate {entry.get('id')}: {cand_err}")
                        continue

            if raw_file_path and raw_file_path.exists():
                break
        except Exception as sc_err:
            all_errors.append(f"SC search '{q}': {sc_err}")

    # 3. Priority Tier 2: YouTube Search fallback
    if not raw_file_path or not raw_file_path.exists():
        yt_queries = [f"ytsearch1:{artist} - {title}"]
        primary_artist = re.split(r"[,&]|\bfeat\b|\bft\b", artist)[0].strip()
        if primary_artist and primary_artist != artist:
            yt_queries.append(f"ytsearch1:{primary_artist} - {title}")

        for yt_q in yt_queries:
            logger.info("Searching YouTube for Spotify track fallback: %s", yt_q)
            try:
                with yt_dlp.YoutubeDL(base_opts) as yt_ydl:
                    info = yt_ydl.extract_info(yt_q, download=True)
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
                if raw_file_path and raw_file_path.exists():
                    break
            except Exception as yt_err:
                all_errors.append(f"YouTube query '{yt_q}': {yt_err}")

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
        logger.info("Produced 320kbps MP3 with embedded cover: %s (%s bytes)", output_file.name, output_file.stat().st_size)
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


async def download_spotify_track_meta(meta: SpotifyTrackMetadata, fallback_cover: str | None = None) -> SpotifyTrack:
    """Asynchronously download track using its metadata."""
    return await asyncio.to_thread(_download_spotify_track_meta_sync, meta, fallback_cover)


def _download_spotify_sync(url: str) -> SpotifyTrack:
    """Download single Spotify track."""
    meta = _get_spotify_metadata(url)
    return _download_spotify_track_meta_sync(meta)


async def download_spotify(url: str) -> SpotifyTrack:
    """Asynchronously download Spotify track in a worker thread."""
    return await asyncio.to_thread(_download_spotify_sync, url)


# ---------------------------------------------------------------------------
# Spotify Web API Catalog Search & Direct MP3 Serving
# ---------------------------------------------------------------------------

_spotify_token_cache = {
    "token": None,
    "expires_at": 0,
}


def _get_spotify_api_token() -> str | None:
    """Get Spotify Web API access token via Client Credentials if configured."""
    from bot.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None

    import time
    now = time.time()
    if _spotify_token_cache["token"] and _spotify_token_cache["expires_at"] > now + 60:
        return _spotify_token_cache["token"]

    import base64
    auth_header = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    token_url = "https://accounts.spotify.com/api/token"
    req = urllib.request.Request(
        token_url,
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token = data.get("access_token")
            expires_in = data.get("expires_in") or 3600
            if token:
                _spotify_token_cache["token"] = token
                _spotify_token_cache["expires_at"] = now + expires_in
                return token
    except Exception as e:
        logger.warning("Failed to obtain Spotify access token: %s", e)
    return None


_INLINE_TRACK_CACHE: dict[str, SpotifyTrackMetadata] = {}
_active_prep_tasks: dict[str, asyncio.Task] = {}


def get_cached_track_meta(track_id: str) -> SpotifyTrackMetadata | None:
    """Retrieve in-memory cached track metadata from recent searches."""
    return _INLINE_TRACK_CACHE.get(track_id)


def _cache_track_meta(meta: SpotifyTrackMetadata):
    """Store track metadata in fast LRU-like memory cache."""
    if meta and meta.track_id:
        if len(_INLINE_TRACK_CACHE) > 500:
            for k in list(_INLINE_TRACK_CACHE.keys())[:100]:
                _INLINE_TRACK_CACHE.pop(k, None)
        _INLINE_TRACK_CACHE[meta.track_id] = meta


def _search_spotify_sync(query: str, limit: int = 10) -> list[SpotifyTrackMetadata]:
    """
    Search music catalog with intelligent fallbacks:
    1. Official Spotify Web API (if SPOTIFY_CLIENT_ID / SECRET configured)
    2. Apple Music / iTunes Catalog API (direct studio metadata, 600x600 covers, free, unblocked)
    3. Deezer API (direct studio catalog)
    4. YouTube Music fallback
    """
    clean_q = query.strip()
    if not clean_q:
        return []

    # Tier 1: Spotify Web API via Client Credentials
    token = _get_spotify_api_token()
    if token:
        try:
            import urllib.parse
            s_url = f"https://api.spotify.com/v1/search?type=track&limit={limit}&q={urllib.parse.quote(clean_q)}"
            req = urllib.request.Request(
                s_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Mozilla/5.0",
                }
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("tracks", {}).get("items", [])
                results = []
                for t in items:
                    if not t:
                        continue
                    t_id = t.get("id")
                    title = t.get("name") or "Unknown"
                    artists = " & ".join([a["name"] for a in t.get("artists", []) if a.get("name")])
                    dur = int((t.get("duration_ms") or 0) / 1000)
                    imgs = t.get("album", {}).get("images", [])
                    cover = imgs[0].get("url") if imgs else None
                    m = SpotifyTrackMetadata(
                        title=title,
                        artist=artists,
                        duration=dur,
                        cover_url=cover,
                        track_id=t_id,
                    )
                    results.append(m)
                    _cache_track_meta(m)
                if results:
                    return results
        except Exception as e:
            logger.warning("Spotify API search failed: %s", e)

    # Tier 2: Apple Music / iTunes Store Catalog (clean studio metadata, 600x600 covers)
    try:
        import urllib.parse
        itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(clean_q)}&entity=song&limit={limit}"
        it_req = urllib.request.Request(itunes_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(it_req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("results", []):
                t_name = item.get("trackName")
                a_name = item.get("artistName")
                if not t_name:
                    continue
                raw_art = item.get("artworkUrl100") or ""
                cover_url = raw_art.replace("100x100bb", "600x600bb") if raw_art else None
                dur = int((item.get("trackTimeMillis") or 0) / 1000)
                t_id = f"it_{item.get('trackId')}"
                m = SpotifyTrackMetadata(
                    title=t_name,
                    artist=a_name or "Artist",
                    duration=dur,
                    cover_url=cover_url,
                    track_id=t_id,
                )
                results.append(m)
                _cache_track_meta(m)
            if results:
                return results
    except Exception as ie:
        logger.warning("iTunes search fallback failed: %s", ie)

    # Tier 3: Deezer Studio Catalog
    try:
        import urllib.parse
        deezer_url = f"https://api.deezer.com/search?q={urllib.parse.quote(clean_q)}&limit={limit}"
        dz_req = urllib.request.Request(deezer_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(dz_req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("data", []):
                t_name = item.get("title")
                a_name = item.get("artist", {}).get("name")
                if not t_name:
                    continue
                album = item.get("album", {})
                cover_url = album.get("cover_big") or album.get("cover_medium")
                dur = item.get("duration") or 0
                t_id = f"dz_{item.get('id')}"
                m = SpotifyTrackMetadata(
                    title=t_name,
                    artist=a_name or "Artist",
                    duration=dur,
                    cover_url=cover_url,
                    track_id=t_id,
                )
                results.append(m)
                _cache_track_meta(m)
            if results:
                return results
    except Exception as de:
        logger.warning("Deezer search fallback failed: %s", de)

    # Tier 4: Fast music search fallback
    from bot.services.music_search import _search_music_sync
    flat_results = _search_music_sync(clean_q, limit=limit)
    results = []
    for r in flat_results:
        m = SpotifyTrackMetadata(
            title=r.title,
            artist=r.artist,
            duration=r.duration,
            cover_url=r.thumbnail,
            track_id=f"yt_{r.id}",
        )
        results.append(m)
        _cache_track_meta(m)
    return results


async def search_spotify(query: str, limit: int = 10) -> list[SpotifyTrackMetadata]:
    """Asynchronously search Spotify tracks."""
    return await asyncio.to_thread(_search_spotify_sync, query, limit)


def get_or_prepare_spotify_mp3_sync(meta: SpotifyTrackMetadata) -> Path:
    """Ensure high-quality 320k MP3 file exists for the given track metadata."""
    final_path = DOWNLOADS_DIR / f"sp_{meta.track_id}.mp3"
    if final_path.exists() and final_path.stat().st_size > 50000:
        return final_path

    alt_path = DOWNLOADS_DIR / f"spot_{meta.track_id}.mp3"
    if alt_path.exists() and alt_path.stat().st_size > 50000:
        import shutil
        shutil.copy2(alt_path, final_path)
        return final_path

    track = _download_spotify_track_meta_sync(meta)
    if track.file_path.exists() and track.file_path != final_path:
        import shutil
        shutil.copy2(track.file_path, final_path)
    return final_path


async def get_or_prepare_spotify_mp3(meta: SpotifyTrackMetadata) -> Path:
    """Asynchronously ensure high-quality 320k MP3 exists without duplicate concurrent downloads."""
    key = meta.track_id
    if key in _active_prep_tasks and not _active_prep_tasks[key].done():
        return await _active_prep_tasks[key]

    task = asyncio.create_task(asyncio.to_thread(get_or_prepare_spotify_mp3_sync, meta))
    _active_prep_tasks[key] = task
    try:
        return await task
    finally:
        _active_prep_tasks.pop(key, None)



