import os
import asyncio
import logging
import sys
import re

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from aiohttp import web
from bot.config import BOT_TOKEN, PROXY_URL, REQUIRED_CHANNEL, ADMIN_ID
from bot.handlers.common import router as common_router
from bot.handlers.downloader import router as downloader_router
from bot.handlers.video_note import router as video_note_router
from bot.handlers.inline import router as inline_router
from bot.handlers.movies import router as movies_router
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



    async def handle_test_spot_search(request):
        q = request.query.get("q", "shayea")
        import urllib.request, urllib.parse, json
        try:
            url = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Referer": "https://open.spotify.com/",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
                token = token_data.get("accessToken")
                
            s_url = f"https://api.spotify.com/v1/search?type=track&limit=5&q={urllib.parse.quote(q)}"
            s_req = urllib.request.Request(
                s_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }
            )
            with urllib.request.urlopen(s_req, timeout=8) as s_resp:
                s_data = json.loads(s_resp.read().decode("utf-8"))
                items = s_data.get("tracks", {}).get("items", [])
                tracks = []
                for t in items:
                    tracks.append({
                        "id": t.get("id"),
                        "name": t.get("name"),
                        "artist": ", ".join([a["name"] for a in t.get("artists", []) if a.get("name")]),
                        "duration": int((t.get("duration_ms") or 0) / 1000),
                        "cover": t.get("album", {}).get("images", [{}])[0].get("url"),
                    })
                return web.json_response({"ok": True, "tracks": tracks})
        except Exception as e:
            import traceback
            return web.json_response({"ok": False, "error": str(e), "trace": traceback.format_exc()})


    async def handle_test_inline_audio(request):
        import traceback
        track_id = request.query.get("id", "4cOdK2wGLETKBW3PvgPWqT")
        from bot.services.spotify import (
            get_cached_track_meta,
            get_spotify_track_metadata,
            get_or_prepare_spotify_mp3,
            SpotifyTrackMetadata,
        )
        try:
            meta = get_cached_track_meta(track_id)
            if not meta:
                meta = await asyncio.to_thread(get_spotify_track_metadata, track_id)
            audio_path = await get_or_prepare_spotify_mp3(meta)
            return web.json_response({
                "ok": True,
                "meta": {
                    "title": meta.title,
                    "artist": meta.artist,
                    "duration": meta.duration,
                    "cover_url": meta.cover_url,
                    "album": getattr(meta, "album", None),
                    "album_artist": getattr(meta, "album_artist", None),
                    "genre": getattr(meta, "genre", None),
                    "release_date": getattr(meta, "release_date", None),
                    "track_number": getattr(meta, "track_number", None),
                    "disc_number": getattr(meta, "disc_number", None),
                },
                "path": str(audio_path),
                "exists": audio_path.exists(),
                "size": audio_path.stat().st_size if audio_path.exists() else 0,
            })
        except Exception as e:
            return web.json_response({
                "ok": False,
                "error": str(e),
                "trace": traceback.format_exc(),
            })


    _audio_request_logs = []

    async def handle_serve_audio(request):
        import time
        t_start = time.time()
        client_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.remote or "unknown"
        user_agent = request.headers.get("User-Agent", "unknown")
        method = request.method
        filename = request.match_info.get("filename", "")
        req_entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": client_ip,
            "ua": user_agent,
            "method": method,
            "filename": filename,
            "range": request.headers.get("Range"),
            "status": "pending",
        }
        _audio_request_logs.append(req_entry)
        if len(_audio_request_logs) > 100:
            _audio_request_logs.pop(0)

        logger.info(">>> Audio request received: %s %s from %s (UA: %s)", method, filename, client_ip, user_agent)

        try:
            if not re.match(r"^sp_[A-Za-z0-9_\-]+\.mp3$", filename):
                req_entry["status"] = "404_invalid_name"
                return web.Response(status=404, text="Audio file not found")

            from bot.config import DOWNLOADS_DIR
            file_path = DOWNLOADS_DIR / filename
            if file_path.exists() and (file_path.stat().st_size > 500000 or filename == "sp_pending_v2.mp3"):
                elapsed = round(time.time() - t_start, 3)
                logger.info("Serving cached 320k audio from disk in %ss: %s (%s bytes)", elapsed, filename, file_path.stat().st_size)
                req_entry["status"] = f"200_cached_{elapsed}s"
                return web.FileResponse(file_path)

            track_id = filename[3:-4]
            logger.info("Audio not on disk, preparing on-the-fly: %s", track_id)
            from bot.services.spotify import (
                get_cached_track_meta,
                get_spotify_track_metadata,
                get_or_prepare_spotify_mp3,
                SpotifyTrackMetadata,
            )
            meta = get_cached_track_meta(track_id)
            if not meta:
                try:
                    meta = await asyncio.to_thread(get_spotify_track_metadata, track_id)
                except Exception as e:
                    logger.warning("Error fetching track meta for %s: %s", track_id, e)
                    meta = None

            if not meta or not meta.title:
                meta = SpotifyTrackMetadata(title="Music Track", artist="Artist", duration=0, cover_url=None, track_id=track_id)

            # Wait for full 320kbps MP3 preparation (background task usually completes ahead of time)
            audio_path = None
            try:
                audio_path = await asyncio.wait_for(get_or_prepare_spotify_mp3(meta), timeout=14.0)
            except asyncio.TimeoutError:
                logger.warning("Preparation for %s took > 14s", track_id)

            elapsed = round(time.time() - t_start, 2)
            if audio_path and audio_path.exists() and audio_path.stat().st_size > 500000:
                logger.info("Prepared and serving 320k audio in %ss: %s (%s bytes)", elapsed, filename, audio_path.stat().st_size)
                req_entry["status"] = f"200_prepared_{elapsed}s"
                return web.FileResponse(audio_path)

            # Final check: if final sp_{track_id}.mp3 exists on disk (>500KB), serve it
            final_sp = DOWNLOADS_DIR / f"sp_{track_id}.mp3"
            if final_sp.exists() and final_sp.stat().st_size > 500000:
                logger.info("Serving ready 320k audio: %s (%s bytes)", final_sp.name, final_sp.stat().st_size)
                req_entry["status"] = f"200_sp_{elapsed}s"
                return web.FileResponse(final_sp)

            req_entry["status"] = f"500_file_missing_or_small_{elapsed}s"
            return web.Response(status=500, text=f"File not ready: {audio_path}")
        except Exception as e:
            import traceback
            elapsed = round(time.time() - t_start, 2)
            req_entry["status"] = f"500_exception_{e}"
            logger.exception("Error in handle_serve_audio: %s", e)
            return web.Response(status=500, text=f"Exception: {e}\n{traceback.format_exc()}")

    async def handle_debug_audio_logs(request):
        return web.json_response({
            "ok": True,
            "total_requests": len(_audio_request_logs),
            "recent_requests": list(reversed(_audio_request_logs[-50:])),
        })

    async def handle_debug_db(request):
        import sqlite3
        from bot.services.cache import DB_PATH
        tracks = []
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT track_id, title, artist, duration, file_id FROM cached_tracks ORDER BY rowid DESC LIMIT 25")
                tracks = [{"track_id": r[0], "title": r[1], "artist": r[2], "duration": r[3], "file_id": r[4]} for r in cur.fetchall()]
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "count": len(tracks), "cached_tracks": tracks})

    async def handle_clear_cache(request):
        import sqlite3
        from bot.services.cache import DB_PATH
        from bot.config import DOWNLOADS_DIR
        cleared_files = 0
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM cached_tracks")
                conn.commit()
            for pattern in ["sp_*.mp3", "spot_*.mp3", "fast_*.mp3", "raw_*.mp3", "raw_*.m4a"]:
                for f in DOWNLOADS_DIR.glob(pattern):
                    try:
                        f.unlink()
                        cleared_files += 1
                    except Exception:
                        pass
            return web.json_response({"ok": True, "msg": f"Cache cleared, {cleared_files} disk files removed"})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_api_users_list(request):
        from bot.services.user_storage import get_all_users, to_shamsi_tehran
        users = get_all_users()
        res = []
        for u in users:
            res.append({
                "user_id": u.get("user_id"),
                "username": u.get("username"),
                "name": f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip(),
                "first_seen_raw": u.get("first_seen"),
                "first_seen_shamsi": to_shamsi_tehran(u.get("first_seen")),
                "last_seen_raw": u.get("last_seen"),
                "last_seen_shamsi": to_shamsi_tehran(u.get("last_seen")),
            })
        return web.json_response({"ok": True, "total_users": len(res), "users": res})

    async def handle_send_users_report(request):
        from bot.services.user_storage import get_all_users, format_users_report_chunks, to_shamsi_tehran
        from bot.config import ADMIN_ID
        users = get_all_users()
        if not _global_bot:
            return web.json_response({"ok": False, "error": "Bot instance not initialized yet"})

        chunks = format_users_report_chunks(users, ADMIN_ID)
        sent_messages = 0
        for chunk in chunks:
            try:
                await _global_bot.send_message(chat_id=ADMIN_ID, text=chunk, parse_mode="HTML")
                sent_messages += 1
                await asyncio.sleep(0.5)
            except Exception as se:
                logger.warning("Failed sending chunk to admin: %s", se)

        if users:
            from aiogram.types import BufferedInputFile
            txt_lines = [
                f"گزارش کامل کاربران ربات @Timod27_Bot",
                f"تعداد کل کاربران: {len(users)} نفر",
                "=" * 50,
            ]
            for idx, u in enumerate(users, start=1):
                name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "بدون نام"
                uname = f"@{u.get('username')}" if u.get('username') else "ندارد"
                fs = to_shamsi_tehran(u.get('first_seen'))
                ls = to_shamsi_tehran(u.get('last_seen'))
                txt_lines.append(f"{idx}. {name} | آیدی: {u.get('user_id')} | یوزرنیم: {uname}")
                txt_lines.append(f"   اولین استارت: {fs}")
                txt_lines.append(f"   آخرین فعالیت: {ls}")
                txt_lines.append("-" * 40)
            try:
                file_bytes = "\n".join(txt_lines).encode("utf-8")
                doc = BufferedInputFile(file_bytes, filename="users_list_shamsi.txt")
                await _global_bot.send_document(chat_id=ADMIN_ID, document=doc, caption="📁 فایل متنی کامل مشخصات کاربران ربات (تاریخ شمسی و ساعت تهران)")
            except Exception as de:
                logger.warning("Failed sending txt file to admin: %s", de)

        return web.json_response({
            "ok": True,
            "total_users": len(users),
            "sent_chunks": sent_messages,
        })

    async def handle_api_backup(request):
        from bot.services.user_storage import dump_users_json_data
        return web.json_response(dump_users_json_data())

    async def handle_api_backup_now(request):
        from bot.services.user_storage import send_telegram_backup
        from bot.config import ADMIN_ID
        if not _global_bot:
            return web.json_response({"ok": False, "error": "Bot instance not initialized yet"})
        success = await send_telegram_backup(_global_bot, ADMIN_ID)
        return web.json_response({"ok": success, "msg": "Backup sent to Telegram admin" if success else "Failed to send backup"})

    async def handle_api_restore(request):
        from bot.services.user_storage import restore_users_from_json
        try:
            body = await request.json()
            before, after, imported = restore_users_from_json(body, sync_after=True)
            return web.json_response({
                "ok": True,
                "users_before": before,
                "users_after": after,
                "imported_or_updated": imported,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/version", handle_version)
    app.router.add_get("/test-pin", handle_test_pin)
    app.router.add_get("/test-spotify", handle_test_spotify)
    app.router.add_get("/test-spotify-album", handle_test_spotify_album)
    app.router.add_get("/test-spot-search", handle_test_spot_search)
    app.router.add_get("/test-inline-audio", handle_test_inline_audio)
    async def handle_test_f2m(request):
        q = request.query.get("q", "Inception")
        from bot.services.film2media import search_f2m, get_movie_details
        try:
            results = await search_f2m(q)
            details = None
            if results:
                details = await get_movie_details(results[0]["url"])
            return web.json_response({
                "ok": True,
                "query": q,
                "results_count": len(results),
                "first_result": results[0] if results else None,
                "first_details": {
                    "title": details.get("title") if details else None,
                    "downloads_count": len(details.get("downloads", [])) if details else 0,
                    "downloads": details.get("downloads", [])[:5] if details else [],
                } if details else None,
            })
        except Exception as e:
            import traceback
            return web.json_response({"ok": False, "error": str(e), "trace": traceback.format_exc()})

    app.router.add_get("/debug-yt", handle_debug_yt)
    app.router.add_get("/debug-audio-logs", handle_debug_audio_logs)
    app.router.add_get("/debug-db", handle_debug_db)
    app.router.add_get("/clear-cache", handle_clear_cache)
    app.router.add_get("/api/users-list", handle_api_users_list)
    app.router.add_get("/api/send-users-report", handle_send_users_report)
    app.router.add_get("/api/backup", handle_api_backup)
    app.router.add_get("/api/backup-now", handle_api_backup_now)
    app.router.add_post("/api/restore", handle_api_restore)
    app.router.add_get("/test-f2m", handle_test_f2m)
    app.router.add_route("*", "/audio/{filename}", handle_serve_audio)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info("Healthcheck web server running on port %s", port)
    except Exception as e:
        logger.warning("Healthcheck server not started: %s", e)


_global_bot: Bot | None = None


async def _send_admin_startup_report(bot: Bot):
    """Send users report with Shamsi date and Tehran time to admin upon startup."""
    await asyncio.sleep(3)
    try:
        from bot.services.user_storage import get_all_users, format_users_report_chunks, to_shamsi_tehran
        users = get_all_users()
        chunks = format_users_report_chunks(users, ADMIN_ID)
        for chunk in chunks:
            await bot.send_message(chat_id=ADMIN_ID, text=chunk, parse_mode="HTML")
            await asyncio.sleep(0.5)

        if users:
            from aiogram.types import BufferedInputFile
            txt_lines = [
                f"گزارش کامل کاربران ربات @Timod27_Bot",
                f"تعداد کل کاربران: {len(users)} نفر",
                "=" * 50,
            ]
            for idx, u in enumerate(users, start=1):
                name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "بدون نام"
                uname = f"@{u.get('username')}" if u.get('username') else "ندارد"
                fs = to_shamsi_tehran(u.get('first_seen'))
                ls = to_shamsi_tehran(u.get('last_seen'))
                txt_lines.append(f"{idx}. {name} | آیدی: {u.get('user_id')} | یوزرنیم: {uname}")
                txt_lines.append(f"   اولین استارت: {fs}")
                txt_lines.append(f"   آخرین فعالیت: {ls}")
                txt_lines.append("-" * 40)
            file_bytes = "\n".join(txt_lines).encode("utf-8")
            doc = BufferedInputFile(file_bytes, filename="users_list_shamsi.txt")
            await bot.send_document(chat_id=ADMIN_ID, document=doc, caption="📁 فایل متنی کامل مشخصات کاربران ربات (تاریخ شمسی و ساعت تهران)")
        logger.info("Admin startup user report sent successfully to %s", ADMIN_ID)
    except Exception as e:
        logger.warning("Failed to send admin startup user report: %s", e)


async def main():
    global _global_bot
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
    _global_bot = bot

    dp = Dispatcher()

    # Enforce channel subscription (@Timod27) and track all users (messages, callbacks, inline searches)
    dp.message.outer_middleware(ChannelSubscriptionMiddleware())
    dp.callback_query.outer_middleware(ChannelSubscriptionMiddleware())
    dp.inline_query.outer_middleware(ChannelSubscriptionMiddleware())

    # Register handlers
    dp.include_router(common_router)
    dp.include_router(video_note_router)
    dp.include_router(downloader_router)
    dp.include_router(movies_router)
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

    # Automatically send the full user list with Shamsi date & Tehran time to admin
    asyncio.create_task(_send_admin_startup_report(bot))

    # Start automated periodic database backup task (every 6 hours to Telegram admin)
    from bot.services.user_storage import start_periodic_backup_worker
    asyncio.create_task(start_periodic_backup_worker(bot, interval_hours=6))

    while True:
        try:
            await dp.start_polling(
                bot,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
            )
            break
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "terminated by other" in err_str:
                print(f"⚠️ Telegram conflict detected: {e}. Retrying in 5 seconds...", flush=True)
                await asyncio.sleep(5)
                continue
            logger.exception("Fatal error in polling loop: %s", e)
            break
    try:
        await bot.session.close()
    except Exception:
        pass
    print("Bot session closed.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown requested.")
