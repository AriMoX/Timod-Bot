import sqlite3
import logging
from pathlib import Path
from bot.config import BASE_DIR

logger = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "music_cache.db"


def init_db():
    """Initializes the SQLite cache tables if they do not exist."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cached_tracks (
                track_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                duration INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS track_meta_cache (
                track_id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                cover_url TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def get_cached_audio(track_id: str) -> dict | None:
    """Retrieve cached audio record by track ID."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_id, title, artist, duration FROM cached_tracks WHERE track_id = ?",
                (track_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "file_id": row[0],
                    "title": row[1],
                    "artist": row[2],
                    "duration": row[3],
                }
    except Exception as e:
        logger.warning("Failed to get cached audio for %s: %s", track_id, e)
    return None


def save_cached_audio(track_id: str, file_id: str, title: str, artist: str, duration: int):
    """Save or update Telegram file_id for a given track ID."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cached_tracks (track_id, file_id, title, artist, duration)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    file_id=excluded.file_id,
                    title=excluded.title,
                    artist=excluded.artist,
                    duration=excluded.duration
                """,
                (track_id, file_id, title, artist, duration),
            )
            conn.commit()
            logger.info("Cached Telegram audio file_id for track: %s", track_id)
    except Exception as e:
        logger.warning("Failed to save cached audio for %s: %s", track_id, e)


def save_track_meta_db(track_id: str, title: str, artist: str, duration: int, cover_url: str | None):
    """Persist track metadata in SQLite so it survives restarts."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO track_meta_cache (track_id, title, artist, duration, cover_url)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    title=excluded.title,
                    artist=excluded.artist,
                    duration=excluded.duration,
                    cover_url=excluded.cover_url,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (track_id, title, artist, duration, cover_url),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to save track meta for %s: %s", track_id, e)


def get_track_meta_db(track_id: str) -> dict | None:
    """Retrieve persisted track metadata from SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, artist, duration, cover_url FROM track_meta_cache WHERE track_id = ?",
                (track_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "title": row[0],
                    "artist": row[1],
                    "duration": row[2],
                    "cover_url": row[3],
                }
    except Exception as e:
        logger.warning("Failed to get track meta for %s: %s", track_id, e)
    return None


# Initialize DB on module load
init_db()
