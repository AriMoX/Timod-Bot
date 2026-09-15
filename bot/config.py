import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env file!")

PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

DOWNLOADS_DIR = BASE_DIR / os.getenv("DOWNLOADS_DIR", "downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Instagram Settings (optional, recommended for bypassing Meta restrictions)
INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID", "").strip() or None
INSTAGRAM_COOKIE_FILE = os.getenv("INSTAGRAM_COOKIE_FILE", "").strip() or None
if INSTAGRAM_COOKIE_FILE:
    cookie_path = Path(INSTAGRAM_COOKIE_FILE)
    if not cookie_path.is_absolute():
        cookie_path = BASE_DIR / cookie_path
    INSTAGRAM_COOKIE_FILE = str(cookie_path) if cookie_path.exists() else None

# Required Channel Membership
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Timod27")
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/Timod27")

# Bot Owner / Admin
ADMIN_ID = int(os.getenv("ADMIN_ID", "448833436"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@AriMoX")


