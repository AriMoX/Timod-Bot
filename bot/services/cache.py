import sqlite3
import logging
from pathlib import Path
from bot.config import BASE_DIR

logger = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "music_cache.db"


def init_db():
    """Initializes the SQLite cache table if it does not exist."""
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


# Initialize DB on module load
init_db()
