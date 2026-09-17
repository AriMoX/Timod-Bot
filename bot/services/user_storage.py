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


def to_shamsi_tehran(ts_str: str | None, assume_utc: bool = True) -> str:
    """Convert a timestamp string to Shamsi (Solar Hijri) date and Tehran time (UTC+03:30)."""
    if not ts_str or ts_str == "نامشخص":
        return "نامشخص"
    try:
        from datetime import timezone, timedelta
        dt = datetime.strptime(str(ts_str).strip(), "%Y-%m-%d %H:%M:%S")
        if assume_utc:
            tehran_tz = timezone(timedelta(hours=3, minutes=30))
            dt = dt.replace(tzinfo=timezone.utc).astimezone(tehran_tz)

        gy, gm, gd = dt.year, dt.month, dt.day
        g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        gy2 = gy + 1 if gm > 2 else gy
        days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
        jy = -1595 + (33 * (days // 12053))
        days %= 12053
        jy += 4 * (days // 1461)
        days %= 1461
        if days > 365:
            jy += (days - 1) // 365
            days = (days - 1) % 365
        if days < 186:
            jm = 1 + (days // 31)
            jd = 1 + (days % 31)
        else:
            jm = 7 + ((days - 186) // 30)
            jd = 1 + ((days - 186) % 30)

        persian_months = [
            "", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"
        ]
        month_name = persian_months[jm] if 1 <= jm <= 12 else str(jm)
        time_str = dt.strftime("%H:%M:%S")
        return f"{jd} {month_name} {jy} - ساعت {time_str} (تهران)"
    except Exception:
        return str(ts_str)


def format_users_report_chunks(users: list[dict], admin_id: int) -> list[str]:
    """Format all users into chunked Telegram messages with Shamsi date & Tehran time."""
    from datetime import datetime, timezone, timedelta
    tehran_now = datetime.now(timezone(timedelta(hours=3, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
    now_shamsi = to_shamsi_tehran(tehran_now, assume_utc=False)

    total = len(users)
    header = (
        f"📊 <b>گزارش کامل کاربران ربات (@Timod27_Bot)</b>\n\n"
        f"👥 <b>تعداد کل کاربران از روز اول:</b> {total} نفر\n"
        f"🕒 <b>زمان تهیه گزارش:</b> {now_shamsi}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not users:
        return [header + "❌ هیچ کاربری در دیتابیس ثبت نشده است."]

    chunks = []
    current_chunk = header

    for idx, u in enumerate(users, start=1):
        username_str = f"@{u['username']}" if u.get("username") else "ندارد"
        first = u.get("first_name") or ""
        last = u.get("last_name") or ""
        name_str = f"{first} {last}".strip() or "بدون نام"
        is_owner = " 👑 <b>(مالک ربات)</b>" if u.get("user_id") == admin_id else ""

        first_seen_shamsi = to_shamsi_tehran(u.get("first_seen"))
        last_seen_shamsi = to_shamsi_tehran(u.get("last_seen"))

        user_block = (
            f"👤 <b>{idx}. {name_str}</b>{is_owner}\n"
            f"   ├ 🆔 <b>آیدی عددی:</b> <code>{u.get('user_id')}</code>\n"
            f"   ├ 🌐 <b>یوزرنیم:</b> {username_str}\n"
            f"   ├ 📅 <b>تاریخ اولین استارت:</b> {first_seen_shamsi}\n"
            f"   └ ⏱ <b>آخرین فعالیت:</b> {last_seen_shamsi}\n"
            f"────────────────────\n"
        )

        if len(current_chunk) + len(user_block) > 3800:
            chunks.append(current_chunk)
            current_chunk = f"📊 <b>ادامه لیست کاربران (بخش {len(chunks) + 1}):</b>\n\n" + user_block
        else:
            current_chunk += user_block

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


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
