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

    async def handle_debug_yt(request):
        v_id = request.query.get("id", "dQw4w9WgXcQ")
        v_url = f"https://www.youtube.com/watch?v={v_id}"
        req_client = request.query.get("client")
        import urllib.request, json, asyncio, yt_dlp, traceback

        if req_client:
            def _test_single():
                extra = {}
                if req_client != "default":
                    extra = {"extractor_args": {"youtube": {"player_client": [req_client]}}}
                opts = {
                    "quiet": True,
                    "no_warnings": False,
                    "noplaylist": True,
                    "socket_timeout": 12,
                    "js_runtimes": {"node": {}},
                    **extra,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(v_url, download=False)
                    formats = [f.get("format_id") for f in info.get("formats", [])]
                    return {
                        "ok": True,
                        "client": req_client,
                        "title": info.get("title"),
                        "formats_count": len(formats),
                        "formats": formats[:10],
                    }
            try:
                res = await asyncio.wait_for(asyncio.to_thread(_test_single), timeout=25)
                return web.json_response(res)
            except Exception as e:
                return web.json_response({"ok": False, "client": req_client, "error": str(e), "trace": traceback.format_exc()})

        report = {"yt_dlp": {}, "invidious": {}, "piped": {}, "cobalt": {}}

        def _test_ytdlp():
            configs = [
                ("visionos", {"extractor_args": {"youtube": {"player_client": ["visionos"]}}}),
                ("ios", {"extractor_args": {"youtube": {"player_client": ["ios"]}}}),
                ("tv_downgraded", {"extractor_args": {"youtube": {"player_client": ["tv_downgraded"]}}}),
                ("web_embedded", {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}),
                ("default", {}),
            ]
            ytdlp_res = {}
            for name, extra in configs:
                opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "socket_timeout": 5,
                    "retries": 0,
                    "js_runtimes": {"node": {}},
                    **extra,
                }
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(v_url, download=False)
                        if info:
                            formats = [f.get("format_id") for f in info.get("formats", [])]
                            ytdlp_res[name] = {
                                "ok": True,
                                "title": info.get("title"),
                                "formats_count": len(formats),
                            }
                            return ytdlp_res, name
                except Exception as e:
                    ytdlp_res[name] = {"ok": False, "err": str(e)[:150]}
            return ytdlp_res, None

        # 1. Test yt-dlp in thread
        try:
            res, winner = await asyncio.wait_for(asyncio.to_thread(_test_ytdlp), timeout=15)
            report["yt_dlp"] = {"winner": winner, "details": res}
            if winner:
                return web.json_response({"ok": True, "source": "yt-dlp", "winner": winner, "report": report})
        except Exception as e:
            report["yt_dlp"] = {"error": str(e)}

        # 2. Test Invidious instances
        def _test_invidious():
            inv_res = {}
            try:
                req = urllib.request.Request("https://instances.invidious.io/api/v1/instances.json", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    instances = json.loads(r.read().decode())
                    live = [item[1].get("uri") for item in instances if isinstance(item, list) and item[1].get("type") == "https" and item[1].get("api") and item[1].get("monitor", {}).get("status") == "200"]
                    inv_res["live_count"] = len(live)
                    for uri in live[:5]:
                        try:
                            v_req = urllib.request.Request(f"{uri}/api/v1/videos/{v_id}", headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(v_req, timeout=5) as vr:
                                v_data = json.loads(vr.read().decode())
                                streams = v_data.get("formatStreams", [])
                                if streams:
                                    inv_res["winner"] = {
                                        "uri": uri,
                                        "title": v_data.get("title"),
                                        "streams": len(streams),
                                        "stream_url": streams[0].get("url")[:80]
                                    }
                                    return inv_res, uri
                        except Exception as ie:
                            inv_res[uri] = str(ie)[:80]
            except Exception as e:
                inv_res["fetch_error"] = str(e)
            return inv_res, None

        try:
            inv_res, inv_winner = await asyncio.wait_for(asyncio.to_thread(_test_invidious), timeout=12)
            report["invidious"] = inv_res
            if inv_winner:
                return web.json_response({"ok": True, "source": "invidious", "winner": inv_winner, "report": report})
        except Exception as e:
            report["invidious"] = {"error": str(e)}

        # 3. Test Piped instances
        def _test_piped():
            piped_res = {}
            try:
                req = urllib.request.Request("https://piped-instances.kavin.rocks/", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    instances = json.loads(r.read().decode())
                    piped_urls = [inst.get("api_url") for inst in instances if inst.get("api_url")]
                    piped_res["count"] = len(piped_urls)
                    for purl in piped_urls[:5]:
                        try:
                            v_req = urllib.request.Request(f"{purl}/streams/{v_id}", headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(v_req, timeout=5) as vr:
                                p_data = json.loads(vr.read().decode())
                                v_streams = p_data.get("videoStreams", [])
                                if v_streams:
                                    piped_res["winner"] = {
                                        "api_url": purl,
                                        "title": p_data.get("title"),
                                        "streams": len(v_streams),
                                        "url": v_streams[0].get("url")[:80]
                                    }
                                    return piped_res, purl
                        except Exception as pe:
                            piped_res[purl] = str(pe)[:80]
            except Exception as e:
                piped_res["fetch_error"] = str(e)
            return piped_res, None

        try:
            piped_res, piped_winner = await asyncio.wait_for(asyncio.to_thread(_test_piped), timeout=12)
            report["piped"] = piped_res
            if piped_winner:
                return web.json_response({"ok": True, "source": "piped", "winner": piped_winner, "report": report})
        except Exception as e:
            report["piped"] = {"error": str(e)}

        return web.json_response({"ok": False, "report": report})

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/version", handle_version)
    app.router.add_get("/test-pin", handle_test_pin)
    app.router.add_get("/test-spotify", handle_test_spotify)
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
