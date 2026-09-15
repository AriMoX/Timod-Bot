import sqlite3
import logging
from datetime import datetime
from bot.config import BASE_DIR

logger = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "bot_database.db"


def init_user_db():
    """Initialize SQLite database for tracking bot users."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    # Pre-seed with known users
    save_or_update_user(448833436, "AriMoX", "Aria", None)
    save_or_update_user(8126127833, "Aria_Moghaddam", "Aria", "Moghaddam")


def save_or_update_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
):
    """Insert or update a user record with timestamp."""
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if user exists
        cursor.execute("SELECT first_seen FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if row:
            cursor.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?, last_seen = ?
                WHERE user_id = ?
                """,
                (username, first_name, last_name, now_str, user_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, last_name, now_str, now_str),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Error saving user %s to database: %s", user_id, e)


def get_all_users() -> list[dict]:
    """Retrieve all users ordered by last seen descending."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users ORDER BY last_seen DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.exception("Error fetching users from database: %s", e)
        return []


def get_user_count() -> int:
    """Return total number of registered users."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.exception("Error fetching user count: %s", e)
        return 0
