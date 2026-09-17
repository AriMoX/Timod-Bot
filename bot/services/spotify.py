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
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    release_date: str | None = None
    track_number: str | int | None = None
    disc_number: str | int | None = None


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
            rel_date = None
            rel_obj = entity.get("releaseDate")
            if isinstance(rel_obj, dict) and rel_obj.get("isoString"):
                rel_date = rel_obj["isoString"][:10]
            elif isinstance(rel_obj, str):
                rel_date = rel_obj[:10]

            return SpotifyTrackMetadata(
                title=title,
                artist=artist,
                duration=duration,
                cover_url=cover_url,
                track_id=track_id,
                release_date=rel_date,
            )
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
    for idx, item in enumerate(raw_tracks, start=1):
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
                album=album_name,
                album_artist=album_artist,
                track_number=str(idx),
                disc_number="1",
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


def _enrich_track_metadata(meta: SpotifyTrackMetadata) -> SpotifyTrackMetadata:
    """
    Enrich track metadata with complete album, album_artist, genre, release_date,
    track_number, and disc_number for high-fidelity ID3 tagging.
    """
    if meta.album and meta.genre and meta.release_date and meta.album_artist and meta.track_number and meta.cover_url:
        return meta

    album = meta.album
    album_artist = meta.album_artist
    genre = meta.genre
    release_date = meta.release_date
    track_number = meta.track_number
    disc_number = meta.disc_number
    cover_url = meta.cover_url

    # 1. Deezer direct track lookup if ID starts with dz_
    if meta.track_id and meta.track_id.startswith("dz_") and len(meta.track_id) > 3:
        dz_id = meta.track_id[3:]
        try:
            req = urllib.request.Request(f"https://api.deezer.com/track/{dz_id}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as r:
                t = json.loads(r.read().decode("utf-8"))
                album = album or t.get("album", {}).get("title")
                album_artist = album_artist or t.get("album", {}).get("artist", {}).get("name") or t.get("artist", {}).get("name")
                release_date = release_date or t.get("release_date")
                track_number = track_number or t.get("track_position")
                disc_number = disc_number or t.get("disk_number")
                if not cover_url:
                    al = t.get("album", {})
                    cover_url = al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium")
                al_id = t.get("album", {}).get("id")
                if al_id and not genre:
                    try:
                        al_req = urllib.request.Request(f"https://api.deezer.com/album/{al_id}", headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(al_req, timeout=2) as al_r:
                            al_data = json.loads(al_r.read().decode("utf-8"))
                            genres = [g["name"] for g in al_data.get("genres", {}).get("data", []) if g.get("name")]
                            if genres:
                                genre = ", ".join(genres)
                    except Exception:
                        pass
        except Exception as de:
            logger.debug("Deezer track lookup failed: %s", de)

    # 2. Deezer search by artist + clean title
    if not album or not genre or not release_date or not cover_url:
        try:
            import urllib.parse
            clean_t = _clean_title(meta.title)
            q = f"{meta.artist} {clean_t}".strip()
            url = f"https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read().decode("utf-8"))
                items = d.get("data", [])
                if items:
                    t = items[0]
                    dz_id = t.get("id")
                    if dz_id:
                        t_req = urllib.request.Request(f"https://api.deezer.com/track/{dz_id}", headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(t_req, timeout=3) as tr:
                            t_info = json.loads(tr.read().decode("utf-8"))
                            album = album or t_info.get("album", {}).get("title")
                            album_artist = album_artist or t_info.get("album", {}).get("artist", {}).get("name") or t_info.get("artist", {}).get("name")
                            release_date = release_date or t_info.get("release_date")
                            track_number = track_number or t_info.get("track_position")
                            disc_number = disc_number or t_info.get("disk_number")
                            if not cover_url:
                                al = t_info.get("album", {})
                                cover_url = al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium")
                            al_id = t_info.get("album", {}).get("id")
                            if al_id and not genre:
                                try:
                                    al_req = urllib.request.Request(f"https://api.deezer.com/album/{al_id}", headers={"User-Agent": "Mozilla/5.0"})
                                    with urllib.request.urlopen(al_req, timeout=2) as al_r:
                                        al_data = json.loads(al_r.read().decode("utf-8"))
                                        genres = [g["name"] for g in al_data.get("genres", {}).get("data", []) if g.get("name")]
                                        if genres:
                                            genre = ", ".join(genres)
                                except Exception:
                                    pass
        except Exception as se:
            logger.debug("Deezer search enrichment failed: %s", se)

    # 3. iTunes search fallback
    if not album or not genre or not release_date or not cover_url:
        try:
            import urllib.parse
            clean_t = _clean_title(meta.title)
            q = f"{meta.artist} {clean_t}".strip()
            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}&entity=song&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read().decode("utf-8"))
                results = d.get("results", [])
                if results:
                    it = results[0]
                    album = album or it.get("collectionName")
                    album_artist = album_artist or it.get("artistName")
                    genre = genre or it.get("primaryGenreName")
                    raw_date = it.get("releaseDate")
                    if raw_date and not release_date:
                        release_date = raw_date[:10]
                    track_number = track_number or it.get("trackNumber")
                    disc_number = disc_number or it.get("discNumber")
                    if not cover_url:
                        raw_art = it.get("artworkUrl100") or ""
                        cover_url = raw_art.replace("100x100bb", "600x600bb") if raw_art else None
        except Exception as ie:
            logger.debug("iTunes search enrichment failed: %s", ie)

    # 4. Intelligent defaults ensuring NO field is ever empty in Tag Editor
    primary_artist = re.split(r"[,&]|\bfeat\b|\bft\b", meta.artist, flags=re.IGNORECASE)[0].strip() or meta.artist
    if not album:
        album = f"{meta.title} - Single"
    if not album_artist:
        album_artist = primary_artist
    if not genre:
        rap_keywords = ["rap", "trap", "hip hop", "diss", "beat", "vinak", "flame", "shayea", "hichkas", "yas", "hooshmand", "pishro", "zedbazi", "khalse", "leito", "chvrsi", "catchybeatz"]
        combined_str = f"{meta.artist} {meta.title}".lower()
        if any(kw in combined_str for kw in rap_keywords):
            genre = "Hip-Hop/Rap"
        else:
            genre = "Pop"
    if not release_date:
        release_date = "2024-01-01"
    if not track_number:
        track_number = "1/1"
    if not disc_number:
        disc_number = "1/1"

    enriched_meta = SpotifyTrackMetadata(
        title=meta.title,
        artist=meta.artist,
        duration=meta.duration,
        cover_url=cover_url,
        track_id=meta.track_id,
        album=album,
        album_artist=album_artist,
        genre=genre,
        release_date=str(release_date),
        track_number=str(track_number),
        disc_number=str(disc_number),
    )
    _cache_track_meta(enriched_meta)
    return enriched_meta


def _download_spotify_track_meta_sync(meta: SpotifyTrackMetadata, fallback_cover: str | None = None) -> SpotifyTrack:
    """Synchronously download and encode a track based on its metadata with full ID3v2.3 tags."""
    meta = _enrich_track_metadata(meta)
    title = meta.title
    artist = meta.artist
    album = meta.album or f"{title} - Single"
    album_artist = meta.album_artist or artist
    genre = meta.genre or "Pop"
    release_date = meta.release_date or "2024-01-01"
    track_number = str(meta.track_number or "1/1")
    disc_number = str(meta.disc_number or "1/1")
    meta_duration = meta.duration
    cover_url = meta.cover_url or fallback_cover
    track_id = meta.track_id
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    year_match = re.search(r"\b(19\d\d|20\d\d)\b", str(release_date))
    year_val = year_match.group(1) if year_match else "2024"

    meta_args = [
        "-id3v2_version", "3",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album={album}",
        "-metadata", f"album_artist={album_artist}",
        "-metadata", f"genre={genre}",
        "-metadata", f"date={release_date}",
        "-metadata", f"year={year_val}",
        "-metadata", f"track={track_number}",
        "-metadata", f"disc={disc_number}",
    ]

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

    # Base yt-dlp configuration (prefer fast audio stream 140/m4a)
    base_opts = {
        "format": "140/bestaudio[ext=m4a]/bestaudio/best",
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

    # Step 1: Multi-Candidate Non-DRM Full Audio Download (SoundCloud primary 1-2s)
    queries = _generate_search_queries(artist, title)
    logger.info("Starting fast full audio search for '%s - %s': %s", artist, title, queries[:2])

    sc_search_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 5,
    }

    cand_dl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "ffmpeg_location": ffmpeg_exe,
        "outtmpl": str(DOWNLOADS_DIR / f"raw_spot_{track_id}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "retries": 1,
    }

    for q in queries[:2]:
        sc_query = f"scsearch5:{q}"
        logger.info("Searching SoundCloud for clean candidates: %s", sc_query)
        try:
            with yt_dlp.YoutubeDL(sc_search_opts) as sc_ydl:
                sc_info = sc_ydl.extract_info(sc_query, download=False)
                candidates = sc_info.get("entries") or [sc_info]

                for entry in candidates:
                    if not entry:
                        continue
                    cand_url = entry.get("webpage_url") or entry.get("url")
                    c_title = entry.get("title") or ""
                    c_dur = entry.get("duration") or 0
                    c_id = entry.get("id")

                    # Skip DRM Go+ tracks (sub_high_tier) or tiny snippets (< 50s)
                    if entry.get("monetization_model") == "sub_high_tier":
                        logger.info("Skipping DRM Go+ track: %s", c_title)
                        continue
                    if meta_duration > 40:
                        if c_dur > 0 and (c_dur < 50 or abs(c_dur - meta_duration) > 50):
                            logger.info("Skipping candidate with duration mismatch: %ss (expected ~%ss)", c_dur, meta_duration)
                            continue
                    elif c_dur > 0 and c_dur < 50:
                        continue

                    try:
                        logger.info("Downloading clean audio candidate: %s (%ss) -> %s", c_title, c_dur, cand_url)
                        with yt_dlp.YoutubeDL(cand_dl_opts) as sc_dl:
                            c_info = sc_dl.extract_info(cand_url, download=True)
                        matches = list(DOWNLOADS_DIR.glob(f"raw_spot_{track_id}_{c_id}.*"))
                        if matches and matches[0].exists() and matches[0].stat().st_size > 500000:
                            raw_file_path = matches[0]
                            actual_duration = int(c_dur or (c_info.get("duration") if c_info else 0) or meta_duration or 0)
                            logger.info("Successfully fetched full track: %s (%s bytes)", raw_file_path.name, raw_file_path.stat().st_size)
                            break
                    except Exception as cand_err:
                        all_errors.append(f"Candidate {c_id}: {cand_err}")
                        continue

            if raw_file_path and raw_file_path.exists():
                break
        except Exception as sc_err:
            all_errors.append(f"SC query '{q}': {sc_err}")

    # Fallback: YouTube Search for full track if SoundCloud yielded no match
    if not raw_file_path or not raw_file_path.exists():
        from bot.config import YOUTUBE_COOKIES_PATH
        yt_search_opts = {
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 6,
            "cookiefile": str(YOUTUBE_COOKIES_PATH) if YOUTUBE_COOKIES_PATH.exists() else None,
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["visionos", "android", "web"]
                }
            },
        }
        yt_cand_opts = {
            "format": "bestaudio/best",
            "ffmpeg_location": ffmpeg_exe,
            "outtmpl": str(DOWNLOADS_DIR / f"raw_spot_{track_id}_yt_%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 8,
            "retries": 1,
            "cookiefile": str(YOUTUBE_COOKIES_PATH) if YOUTUBE_COOKIES_PATH.exists() else None,
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["visionos", "android", "web"]
                }
            },
        }
        for q in queries[:2]:
            yt_query = f"ytsearch3:{q}"
            logger.info("Searching YouTube fallback for full audio: %s", yt_query)
            try:
                with yt_dlp.YoutubeDL(yt_search_opts) as yt_ydl:
                    yt_info = yt_ydl.extract_info(yt_query, download=False)
                    candidates = yt_info.get("entries") or [yt_info]
                    for entry in candidates:
                        if not entry:
                            continue
                        cand_url = entry.get("webpage_url") or entry.get("url")
                        c_title = entry.get("title") or ""
                        c_dur = entry.get("duration") or 0
                        c_id = entry.get("id")

                        if meta_duration > 40:
                            if c_dur > 0 and (c_dur < 50 or abs(c_dur - meta_duration) > 55):
                                continue
                        elif c_dur > 0 and c_dur < 50:
                            continue

                        try:
                            logger.info("Downloading YouTube candidate: %s (%ss) -> %s", c_title, c_dur, cand_url)
                            with yt_dlp.YoutubeDL(yt_cand_opts) as yt_dl:
                                c_info = yt_dl.extract_info(cand_url, download=True)
                            matches = list(DOWNLOADS_DIR.glob(f"raw_spot_{track_id}_yt_{c_id}.*"))
                            if matches and matches[0].exists() and matches[0].stat().st_size > 500000:
                                raw_file_path = matches[0]
                                actual_duration = int(c_dur or (c_info.get("duration") if c_info else 0) or meta_duration or 0)
                                logger.info("Successfully fetched full track from YouTube: %s (%s bytes)", raw_file_path.name, raw_file_path.stat().st_size)
                                break
                        except Exception as cand_err:
                            all_errors.append(f"YT Candidate {c_id}: {cand_err}")
                            continue

                if raw_file_path and raw_file_path.exists():
                    break
            except Exception as yt_err:
                all_errors.append(f"YT query '{q}': {yt_err}")

    # Step 2: Master High-Fidelity 320kbps Encoding with HD Cover Art embedding (ID3v2.3)
    final_audio_path = DOWNLOADS_DIR / f"spot_{track_id}.mp3"
    sp_ready_path = DOWNLOADS_DIR / f"sp_{track_id}.mp3"

    if raw_file_path and raw_file_path.exists():
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
                "-threads", "0",
                "-map", "0:a:0",
                "-map", "1:v:0",
                "-c:v", "copy",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                *meta_args,
                str(final_audio_path),
            ])
        else:
            ffmpeg_cmd.extend([
                "-c:a", "libmp3lame",
                "-b:a", "320k",
                "-threads", "0",
                "-map", "0:a:0",
                *meta_args,
                str(final_audio_path),
            ])

        try:
            subprocess.run(
                ffmpeg_cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
            safe_remove(raw_file_path)
            output_file = final_audio_path
            import shutil
            shutil.copy2(final_audio_path, sp_ready_path)
            logger.info("Produced master 320kbps MP3 with embedded cover: %s (%s bytes)", sp_ready_path.name, sp_ready_path.stat().st_size)
        except Exception as conv_err:
            logger.warning("FFmpeg conversion error (%s), using raw audio", conv_err)
            output_file = raw_file_path
    else:
        err_msg = " | ".join(all_errors)
        raise ValueError(f"امکان یافتن یا دانلود فایل صوتی این قطعه وجود ندارد: '{artist} - {title}'. ({err_msg})")

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
    """Retrieve in-memory cached track metadata from recent searches, SQLite DB, or Deezer API."""
    meta = _INLINE_TRACK_CACHE.get(track_id)
    if meta:
        return meta

    try:
        from bot.services.cache import get_track_meta_db
        db_data = get_track_meta_db(track_id)
        if db_data:
            meta = SpotifyTrackMetadata(
                title=db_data["title"],
                artist=db_data["artist"],
                duration=db_data["duration"],
                cover_url=db_data["cover_url"],
                track_id=track_id,
                album=db_data.get("album"),
                album_artist=db_data.get("album_artist"),
                genre=db_data.get("genre"),
                release_date=db_data.get("release_date"),
                track_number=db_data.get("track_number"),
                disc_number=db_data.get("disc_number"),
            )
            _INLINE_TRACK_CACHE[track_id] = meta
            return meta
    except Exception as e:
        logger.warning("Error fetching track meta from DB for %s: %s", track_id, e)

    # Deezer ID fast recovery (dz_...)
    if track_id.startswith("dz_") and len(track_id) > 3:
        dz_id = track_id[3:]
        try:
            dz_url = f"https://api.deezer.com/track/{dz_id}"
            req = urllib.request.Request(dz_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                t_name = _clean_title(data.get("title") or "Music Track")
                a_name = data.get("artist", {}).get("name") or "Artist"
                dur = data.get("duration") or 0
                album_obj = data.get("album", {})
                cover_url = album_obj.get("cover_big") or album_obj.get("cover_medium")
                al_name = album_obj.get("title")
                al_artist = album_obj.get("artist", {}).get("name") or a_name
                rel_date = data.get("release_date")
                trk_num = data.get("track_position")
                dsc_num = data.get("disk_number")
                genre = None
                al_id = album_obj.get("id")
                if al_id:
                    try:
                        al_req = urllib.request.Request(f"https://api.deezer.com/album/{al_id}", headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(al_req, timeout=3) as al_resp:
                            al_data = json.loads(al_resp.read().decode("utf-8"))
                            genres = [g["name"] for g in al_data.get("genres", {}).get("data", []) if g.get("name")]
                            if genres:
                                genre = ", ".join(genres)
                    except Exception:
                        pass
                meta = SpotifyTrackMetadata(
                    title=t_name,
                    artist=a_name,
                    duration=dur,
                    cover_url=cover_url,
                    track_id=track_id,
                    album=al_name,
                    album_artist=al_artist,
                    genre=genre,
                    release_date=rel_date,
                    track_number=str(trk_num or 1),
                    disc_number=str(dsc_num or 1),
                )
                _cache_track_meta(meta)
                return meta
        except Exception as de:
            logger.warning("Failed to recover Deezer track metadata for %s: %s", track_id, de)

    return None


def _cache_track_meta(meta: SpotifyTrackMetadata):
    """Store track metadata in fast LRU-like memory cache and SQLite."""
    if meta and meta.track_id:
        if len(_INLINE_TRACK_CACHE) > 500:
            for k in list(_INLINE_TRACK_CACHE.keys())[:100]:
                _INLINE_TRACK_CACHE.pop(k, None)
        _INLINE_TRACK_CACHE[meta.track_id] = meta
        try:
            from bot.services.cache import save_track_meta_db
            save_track_meta_db(
                track_id=meta.track_id,
                title=meta.title,
                artist=meta.artist,
                duration=meta.duration,
                cover_url=meta.cover_url,
                album=meta.album,
                album_artist=meta.album_artist,
                genre=meta.genre,
                release_date=meta.release_date,
                track_number=str(meta.track_number) if meta.track_number is not None else None,
                disc_number=str(meta.disc_number) if meta.disc_number is not None else None,
            )
        except Exception as e:
            logger.warning("Failed to persist track meta to DB: %s", e)



def _search_spotify_sync(query: str, limit: int = 10) -> list[SpotifyTrackMetadata]:
    """
    Search music catalog with popularity ranking and intelligent fallback:
    1. Official Spotify Web API (if SPOTIFY_CLIENT_ID / SECRET configured)
    2. Deezer API with order=RANKING (Orders by artist popularity score, supports prefix/fuzzy matching)
    3. YouTube Music AI search (_search_music_sync) - Essential for Persian queries and partial titles
    4. Apple Music / iTunes Catalog
    """
    clean_q = query.strip()
    if not clean_q:
        return []

    is_persian = any('\u0600' <= c <= '\u06FF' for c in clean_q)
    results: list[SpotifyTrackMetadata] = []
    seen = set()

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
                for t in items:
                    if not t:
                        continue
                    t_id = t.get("id")
                    title = _clean_title(t.get("name") or "Unknown")
                    artists = " & ".join([a["name"] for a in t.get("artists", []) if a.get("name")])
                    dur = int((t.get("duration_ms") or 0) / 1000)
                    imgs = t.get("album", {}).get("images", [])
                    cover = imgs[0].get("url") if imgs else None
                    album_obj = t.get("album", {})
                    al_name = album_obj.get("name")
                    al_artist = " & ".join([a["name"] for a in album_obj.get("artists", []) if a.get("name")]) or artists
                    rel_date = album_obj.get("release_date")
                    trk_num = t.get("track_number")
                    dsc_num = t.get("disc_number")
                    dedup_key = (title.lower(), artists.lower())
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        m = SpotifyTrackMetadata(
                            title=title,
                            artist=artists,
                            duration=dur,
                            cover_url=cover,
                            track_id=t_id,
                            album=al_name,
                            album_artist=al_artist,
                            release_date=rel_date,
                            track_number=str(trk_num or 1),
                            disc_number=str(dsc_num or 1),
                        )
                        results.append(m)
                        _cache_track_meta(m)
                if results:
                    return results[:limit]
        except Exception as e:
            logger.warning("Spotify API search failed: %s", e)
    return results[:limit]


async def search_spotify(query: str, limit: int = 10) -> list[SpotifyTrackMetadata]:
    """Asynchronously search Spotify tracks."""
    return await asyncio.to_thread(_search_spotify_sync, query, limit)


def get_or_prepare_spotify_mp3_sync(meta: SpotifyTrackMetadata) -> Path:
    """Ensure high-quality 320k MP3 file exists for the given track metadata."""
    final_path = DOWNLOADS_DIR / f"sp_{meta.track_id}.mp3"
    if final_path.exists() and final_path.stat().st_size > 500000:
        return final_path

    alt_path = DOWNLOADS_DIR / f"spot_{meta.track_id}.mp3"
    if alt_path.exists() and alt_path.stat().st_size > 500000:
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



