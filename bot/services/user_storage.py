import sqlite3
import json
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot.config import BASE_DIR, ADMIN_ID

logger = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "bot_database.db"
JSON_BACKUP_PATH = BASE_DIR / "users_backup.json"


def to_shamsi_tehran(ts_str: str | None, assume_utc: bool = True) -> str:
    """Convert a timestamp string to Shamsi (Solar Hijri) date and Tehran time (UTC+03:30)."""
    if not ts_str or ts_str == "نامشخص":
        return "نامشخص"
    try:
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


def init_user_db():
    """Initialize SQLite database for tracking bot users and seed from backup if needed."""
    try:
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
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                permissions TEXT,
                added_date TEXT
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")
        conn.commit()
        conn.close()

        # 1. First, if users_backup.json exists, restore/merge any users from it
        if JSON_BACKUP_PATH.exists():
            try:
                content = JSON_BACKUP_PATH.read_text(encoding="utf-8")
                data = json.loads(content)
                imported = restore_users_from_json(data, sync_after=False)
                logger.info("Initialized user database from backup JSON: %s users processed", imported)
            except Exception as e:
                logger.warning("Could not pre-load users from %s: %s", JSON_BACKUP_PATH, e)

        # 2. Guarantee known core users exist
        save_or_update_user(448833436, "AriMoX", "Aria", None, sync_json=False)
        save_or_update_user(8126127833, "Aria_Moghaddam", "Aria", "Moghaddam", sync_json=False)

        # 3. Write back synchronized JSON backup
        sync_json_backup()
        logger.info("User database initialized successfully. Total users: %s", get_user_count())
    except Exception as e:
        logger.exception("Error initializing user database: %s", e)


def save_or_update_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    sync_json: bool = True,
) -> bool:
    """Insert or update a user record with timestamp. Returns True if brand new user."""
    is_new = False
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

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
            is_new = True
            cursor.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, last_name, now_str, now_str),
            )

        conn.commit()
        conn.close()

        if sync_json:
            sync_json_backup()
    except Exception as e:
        logger.exception("Error saving user %s to database: %s", user_id, e)
    return is_new


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


def dump_users_json_data() -> dict:
    """Generate structured dictionary of all users with Shamsi & UTC metadata."""
    users = get_all_users()
    tehran_now = datetime.now(timezone(timedelta(hours=3, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
    now_shamsi = to_shamsi_tehran(tehran_now, assume_utc=False)

    users_dump = []
    for u in users:
        users_dump.append({
            "user_id": u.get("user_id"),
            "username": u.get("username"),
            "first_name": u.get("first_name"),
            "last_name": u.get("last_name"),
            "full_name": f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "بدون نام",
            "first_seen": u.get("first_seen"),
            "first_seen_shamsi": to_shamsi_tehran(u.get("first_seen")),
            "last_seen": u.get("last_seen"),
            "last_seen_shamsi": to_shamsi_tehran(u.get("last_seen")),
        })

    return {
        "app": "Timod Downloader Bot (@Timod27_Bot)",
        "exported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "exported_at_shamsi": now_shamsi,
        "total_users": len(users_dump),
        "users": users_dump,
    }


def sync_json_backup():
    """Write latest user records to JSON_BACKUP_PATH on disk."""
    try:
        data = dump_users_json_data()
        tmp_path = JSON_BACKUP_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(JSON_BACKUP_PATH)
    except Exception as e:
        logger.warning("Error syncing users backup to JSON file: %s", e)


def restore_users_from_json(data: dict | list, sync_after: bool = True) -> tuple[int, int, int]:
    """
    Restore or merge users from a JSON object/list into the SQLite database.
    Returns: (count_before, count_after, imported_or_updated)
    """
    users_list = []
    if isinstance(data, dict):
        users_list = data.get("users", [])
    elif isinstance(data, list):
        users_list = data

    count_before = get_user_count()
    imported_or_updated = 0

    if not users_list:
        return count_before, count_before, 0

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        for item in users_list:
            user_id = item.get("user_id")
            if not user_id:
                continue
            try:
                user_id = int(user_id)
            except Exception:
                continue

            username = item.get("username")
            first_name = item.get("first_name")
            last_name = item.get("last_name")
            first_seen = item.get("first_seen")
            last_seen = item.get("last_seen")

            cursor.execute("SELECT first_seen, last_seen, username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()

            if row:
                existing_first_seen, existing_last_seen, ex_uname, ex_fn, ex_ln = row
                best_first_seen = min(filter(None, [existing_first_seen, first_seen])) if (existing_first_seen or first_seen) else None
                best_last_seen = max(filter(None, [existing_last_seen, last_seen])) if (existing_last_seen or last_seen) else None
                best_uname = username or ex_uname
                best_fn = first_name or ex_fn
                best_ln = last_name or ex_ln

                cursor.execute(
                    """
                    UPDATE users
                    SET username = ?, first_name = ?, last_name = ?, first_seen = ?, last_seen = ?
                    WHERE user_id = ?
                    """,
                    (best_uname, best_fn, best_ln, best_first_seen, best_last_seen, user_id),
                )
                imported_or_updated += 1
            else:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username, first_name, last_name, first_seen or now_str, last_seen or now_str),
                )
                imported_or_updated += 1

        conn.commit()
        conn.close()

        if sync_after:
            sync_json_backup()

    except Exception as e:
        logger.exception("Error restoring users from JSON: %s", e)

    count_after = get_user_count()
    return count_before, count_after, imported_or_updated


def restore_database_from_bytes(db_bytes: bytes) -> tuple[bool, str, int]:
    """
    Restore or merge users from a raw SQLite database byte stream.
    Validates SQLite magic header and merges rows safely into current database.
    """
    if not db_bytes.startswith(b"SQLite format 3\x00"):
        return False, "فایل ارسالی یک دیتابیس معتبر SQLite نیست!", 0

    count_before = get_user_count()
    import tempfile
    temp_db = Path(tempfile.gettempdir()) / f"uploaded_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}.db"
    try:
        temp_db.write_bytes(db_bytes)
        temp_conn = sqlite3.connect(temp_db)
        temp_conn.row_factory = sqlite3.Row
        temp_cursor = temp_conn.cursor()

        temp_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if not temp_cursor.fetchone():
            temp_conn.close()
            return False, "جدول کاربران (users) در این فایل دیتابیس یافت نشد!", 0

        temp_cursor.execute("SELECT * FROM users")
        rows = [dict(r) for r in temp_cursor.fetchall()]
        temp_conn.close()

        _, count_after, imported = restore_users_from_json(rows, sync_after=True)
        return True, f"دیتابیس با موفقیت ادغام شد. {imported} کاربر پردازش گردید.", count_after - count_before
    except Exception as e:
        logger.exception("Failed restoring database from bytes: %s", e)
        return False, f"خطا در پردازش دیتابیس: {e}", 0
    finally:
        try:
            if temp_db.exists():
                temp_db.unlink()
        except Exception:
            pass


def get_backup_documents() -> tuple[BufferedInputFile, BufferedInputFile, str]:
    """
    Generate Telegram BufferedInputFile instances for both SQLite DB and JSON backup,
    along with a detailed Persian summary caption.
    """
    sync_json_backup()
    tehran_now = datetime.now(timezone(timedelta(hours=3, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
    now_shamsi = to_shamsi_tehran(tehran_now, assume_utc=False)
    total_users = get_user_count()

    db_bytes = DB_PATH.read_bytes() if DB_PATH.exists() else b""
    json_bytes = JSON_BACKUP_PATH.read_bytes() if JSON_BACKUP_PATH.exists() else json.dumps(dump_users_json_data(), ensure_ascii=False, indent=2).encode("utf-8")

    db_doc = BufferedInputFile(db_bytes, filename=f"bot_database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    json_doc = BufferedInputFile(json_bytes, filename=f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

    caption = (
        f"💾 <b>پشتیبان‌گیری خودکار دیتابیس ربات (@Timod27_Bot)</b>\n\n"
        f"👥 <b>تعداد کل کاربران ثبت‌شده:</b> {total_users} نفر\n"
        f"🕒 <b>زمان پشتیبان‌گیری:</b> {now_shamsi}\n"
        f"📦 <b>محتویات:</b> فایل SQLite دیتابیس (.db) + فایل ساختاریافته (.json)\n\n"
        f"💡 <i>راهنما: برای بازگردانی در هر زمان، کافیست این فایل را با دستور <code>/restore</code> ریپلای کنید.</i>"
    )

    return db_doc, json_doc, caption


async def send_telegram_backup(bot: Bot, chat_id: int = ADMIN_ID) -> bool:
    """Send both database files to admin chat as permanent Telegram cloud backup."""
    try:
        db_doc, json_doc, caption = get_backup_documents()
        db_msg = await bot.send_document(chat_id=chat_id, document=db_doc, caption=caption, parse_mode="HTML")
        await asyncio.sleep(0.5)
        await bot.send_document(
            chat_id=chat_id,
            document=json_doc,
            caption="📋 <b>فایل JSON ساختاریافته مشخصات کاربران ربات</b>",
            parse_mode="HTML",
        )
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=db_msg.message_id, disable_notification=True)
        except Exception as pin_err:
            logger.warning("Could not pin backup message: %s", pin_err)
            
        logger.info("Backup successfully dispatched and pinned to Telegram chat %s", chat_id)
        return True
    except Exception as e:
        logger.exception("Failed to send Telegram backup to %s: %s", chat_id, e)
        return False


async def start_periodic_backup_worker(bot: Bot, interval_hours: int = 6):
    """Background worker that periodically dispatches automated backups to Admin."""
    logger.info("Starting periodic backup worker (interval: %s hours)", interval_hours)
    # Give bot 45 seconds after startup before initial backup run
    await asyncio.sleep(45)
    while True:
        try:
            logger.info("Running periodic database backup...")
            await send_telegram_backup(bot, ADMIN_ID)
        except Exception as e:
            logger.warning("Periodic backup worker encountered an error: %s", e)

        await asyncio.sleep(interval_hours * 3600)


def format_users_report_chunks(users: list[dict], admin_id: int) -> list[str]:
    """Format all users into chunked Telegram messages with Shamsi date & Tehran time."""
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



def get_all_admins() -> list[dict]:
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM admins")
        return [dict(row) for row in c.fetchall()]

def is_admin(user_id: int) -> bool:
    from bot.config import ADMIN_ID
    if user_id == ADMIN_ID: return True
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return bool(c.fetchone())

def get_admin_permissions(user_id: int) -> list[str]:
    from bot.config import ADMIN_ID
    if user_id == ADMIN_ID: return ["all"]
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT permissions FROM admins WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row and row[0]:
            import json
            try:
                return json.loads(row[0])
            except:
                return []
        return []

def add_or_update_admin(user_id: int, added_by: int, permissions: list[str]):
    import json
    import sqlite3
    from datetime import datetime, timezone
    perms_json = json.dumps(permissions)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''
            INSERT INTO admins (user_id, added_by, permissions, added_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                permissions=excluded.permissions
        ''', (user_id, added_by, perms_json, now))
        conn.commit()

def remove_admin(user_id: int):
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()

