import os
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from aiohttp import web
from bot.config import BOT_TOKEN, PROXY_URL, REQUIRED_CHANNEL
from bot.handlers.common import router as common_router
from bot.handlers.downloader import router as downloader_router
from bot.handlers.video_note import router as video_note_router
from bot.handlers.inline import router as inline_router
from bot.middlewares.subscription import ChannelSubscriptionMiddleware
from bot.services.user_storage import init_user_db

# Configure unbuffered UTF-8 output for Windows console
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
if sys.stderr.encoding != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")


async def start_healthcheck_server():
    """Lightweight web server on port 7860 to satisfy Hugging Face Space healthcheck."""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Timod Downloader Bot is running 24/7!", content_type="text/plain")

    async def handle_version(request):
        return web.json_response({"status": "running", "version": "v2.2-pin-dual", "timestamp": "2026-09-15"})

    async def handle_test_pin(request):
        pin_url = request.query.get("url", "https://pin.it/4RwSXGxJL")
        from bot.services.pinterest import download_pinterest
        from bot.utils.cleanup import safe_remove
        try:
            res = await download_pinterest(pin_url)
            size = res.file_path.stat().st_size if res.file_path.exists() else 0
            safe_remove(res.file_path)
            return web.json_response({
                "ok": True,
                "media_type": res.media_type,
                "title": res.title,
                "uploader": res.uploader,
                "size_bytes": size,
            })
        except Exception as err:
            return web.json_response({"ok": False, "error": str(err)})

    async def handle_test_spotify(request):
        spot_url = request.query.get("url", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        from bot.services.spotify import download_spotify
        from bot.utils.cleanup import safe_remove
        import time
        t0 = time.time()
        try:
            track = await download_spotify(spot_url)
            t1 = time.time()
            size = track.file_path.stat().st_size if track.file_path.exists() else 0
            has_thumb = bool(track.thumbnail_path and track.thumbnail_path.exists())
            thumb_size = track.thumbnail_path.stat().st_size if has_thumb else 0
            safe_remove(track.file_path, track.thumbnail_path)
            return web.json_response({
                "ok": True,
                "title": track.title,
                "artist": track.artist,
                "duration": track.duration,
                "file_size": size,
                "has_thumb": has_thumb,
                "thumb_size": thumb_size,
                "time_sec": round(t1 - t0, 2),
            })
        except Exception as err:
            import traceback
            return web.json_response({
                "ok": False,
                "error": str(err),
                "trace": traceback.format_exc(),
            })

    async def handle_test_spotify_album(request):
        album_url = request.query.get("url", "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa")
        from bot.services.spotify import get_spotify_album
        try:
            album = await get_spotify_album(album_url)
            return web.json_response({
                "ok": True,
                "name": album.name,
                "artist": album.artist,
                "is_playlist": album.is_playlist,
                "total_tracks": len(album.tracks),
                "tracks_sample": [{"title": t.title, "artist": t.artist, "duration": t.duration} for t in album.tracks[:5]],
            })
        except Exception as err:
            import traceback
            return web.json_response({"ok": False, "error": str(err), "trace": traceback.format_exc()})

    async def handle_debug_yt(request):
        v_url = request.query.get("url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        use_cookie = request.query.get("cookie", "1") == "1"
        client = request.query.get("client")

        import json
        import yt_dlp
        from bot.services.youtube import _prepare_youtube_cookie_file
        
        cookie_file = _prepare_youtube_cookie_file() if use_cookie else None
        
        logs = []
        class LogCapture:
            def debug(self, msg): logs.append(f"[D] {msg}")
            def info(self, msg): logs.append(f"[I] {msg}")
            def warning(self, msg): logs.append(f"[W] {msg}")
            def error(self, msg): logs.append(f"[E] {msg}")

        clients_list = [c.strip() for c in client.split(",")] if client else ["visionos", "android", "web"]

        opts = {
            "format": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "cookiefile": cookie_file,
            "js_runtimes": {"node": {}},
            "extractor_args": {"youtube": {"player_client": clients_list}},
            "logger": LogCapture(),
            "verbose": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(v_url, download=False)
                return web.json_response({
                    "ok": True,
                    "title": info.get("title") if info else None,
                    "formats_count": len(info.get("formats", [])) if info else 0,
                    "using_cookie": bool(cookie_file),
                    "clients": clients_list,
                    "logs": logs[-25:],
                })
        except Exception as e:
            return web.json_response({
                "ok": False,
                "error": str(e),
                "using_cookie": bool(cookie_file),
                "clients": clients_list,
                "logs": logs[-25:],
            })



    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/version", handle_version)
    app.router.add_get("/test-pin", handle_test_pin)
    app.router.add_get("/test-spotify", handle_test_spotify)
    app.router.add_get("/test-spotify-album", handle_test_spotify_album)
    app.router.add_get("/debug-yt", handle_debug_yt)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info("Healthcheck web server running on port %s", port)
    except Exception as e:
        logger.warning("Healthcheck server not started: %s", e)


async def main():
    print("=" * 50, flush=True)
    print("Initializing Telegram Downloader Bot...", flush=True)

    # Initialize user tracking database
    init_user_db()

    # Start healthcheck server for cloud hosting (Hugging Face)
    await start_healthcheck_server()

    # Optional proxy setup (useful for bypassing local ISP restrictions)
    session = None
    if PROXY_URL:
        print(f"Using proxy: {PROXY_URL}", flush=True)
        session = AiohttpSession(proxy=PROXY_URL)

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Enforce channel subscription (@Timod27)
    dp.message.outer_middleware(ChannelSubscriptionMiddleware())
    dp.callback_query.outer_middleware(ChannelSubscriptionMiddleware())

    # Register handlers
    dp.include_router(common_router)
    dp.include_router(video_note_router)
    dp.include_router(downloader_router)
    dp.include_router(inline_router)

    # Test bot connection
    me = await bot.get_me()
    print(f"✅ Connected successfully as @{me.username} (ID: {me.id})", flush=True)
    if not me.supports_inline_queries:
        print("⚠️ NOTE: Inline queries are DISABLED in @BotFather. Run /setinline to enable popup search!", flush=True)
    else:
        print("✨ Inline mode is active in Telegram!", flush=True)
    print(f"🔒 Required channel lock active for: {REQUIRED_CHANNEL}", flush=True)

    print("🚀 Bot is now listening for messages... (Press Ctrl+C to stop)", flush=True)
    print("=" * 50, flush=True)

    try:
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()
        print("Bot session closed.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown requested.")
