import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

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

# YouTube Settings (optional, recommended for bypassing datacenter restrictions)
YOUTUBE_COOKIE_FILE = os.getenv("YOUTUBE_COOKIE_FILE", "").strip() or None
if YOUTUBE_COOKIE_FILE:
    yt_cookie_path = Path(YOUTUBE_COOKIE_FILE)
    if not yt_cookie_path.is_absolute():
        yt_cookie_path = BASE_DIR / yt_cookie_path
    YOUTUBE_COOKIE_FILE = str(yt_cookie_path) if yt_cookie_path.exists() else None

YOUTUBE_COOKIES_TEXT = os.getenv("YOUTUBE_COOKIES_TEXT", "").strip() or None

# Bot Owner / Admin
ADMIN_ID = int(os.getenv("ADMIN_ID", "448833436"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@AriMoX")

# Spotify API Settings (for direct inline Spotify catalog search)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip() or None
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip() or None

# Server Public URL (for serving direct inline audio streams to Telegram)
SERVER_PUBLIC_URL = os.getenv("SERVER_PUBLIC_URL", "https://timod-bot.onrender.com").rstrip("/")


