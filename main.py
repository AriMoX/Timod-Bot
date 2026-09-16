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

        report = {"cobalt": {}, "piped": {}, "y2mate": {}}

        # 1. Test Cobalt instances
        def _test_cobalts():
            cobalt_insts = [
                "https://api.cobalt.tools",
                "https://cobalt-api.kwiatekm.tokyo",
                "https://cobalt.canine.tools",
                "https://cobalt.streamrip.app",
                "https://dl.khub.win",
            ]
            c_res = {}
            for base in cobalt_insts:
                try:
                    payload = json.dumps({"url": v_url}).encode("utf-8")
                    req = urllib.request.Request(
                        base,
                        data=payload,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = json.loads(r.read().decode())
                        c_res[base] = {
                            "status": data.get("status"),
                            "url": (data.get("url") or "")[:80],
                        }
                        if data.get("url") or data.get("status") in ("tunnel", "redirect", "stream"):
                            return c_res, base, data
                except Exception as ce:
                    c_res[base] = str(ce)[:80]
            return c_res, None, None

        # 2. Test Piped instances
        def _test_pipeds():
            piped_insts = [
                "https://api.piped.privacy.com.de",
                "https://pipedapi.leptons.xyz",
                "https://piped-api.garudalinux.org",
                "https://pipedapi.drgns.space",
                "https://api-piped.mha.fi",
                "https://piped-api.lunar.icu",
            ]
            p_res = {}
            for base in piped_insts:
                try:
                    req = urllib.request.Request(f"{base}/streams/{v_id}", headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = json.loads(r.read().decode())
                        v_streams = data.get("videoStreams", [])
                        p_res[base] = {"streams": len(v_streams)}
                        if v_streams:
                            return p_res, base, v_streams[0].get("url")
                except Exception as pe:
                    p_res[base] = str(pe)[:80]
            return p_res, None, None

        # 3. Test Y2mate
        def _test_y2mate():
            y_res = {}
            try:
                import urllib.parse
                post_data = urllib.parse.urlencode({
                    "k_query": v_url,
                    "k_page": "home",
                    "hl": "en",
                    "q_auto": "0"
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://www.y2mate.com/mates/analyzeV2/ajax",
                    data=post_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "X-Requested-With": "XMLHttpRequest",
                    }
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode())
                    y_res = {"status": data.get("status"), "mess": data.get("mess"), "title": data.get("title")}
                    return y_res, True
            except Exception as ye:
                y_res["error"] = str(ye)[:120]
                return y_res, False

        # Execute tests concurrently
        c_task = asyncio.to_thread(_test_cobalts)
        p_task = asyncio.to_thread(_test_pipeds)
        y_task = asyncio.to_thread(_test_y2mate)

        c_done, p_done, y_done = await asyncio.gather(c_task, p_task, y_task, return_exceptions=True)

        return web.json_response({
            "cobalt": c_done if not isinstance(c_done, Exception) else str(c_done),
            "piped": p_done if not isinstance(p_done, Exception) else str(p_done),
            "y2mate": y_done if not isinstance(y_done, Exception) else str(y_done),
        })

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
