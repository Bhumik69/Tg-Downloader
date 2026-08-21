import asyncio
import os
import re
import time
import secrets

import aiohttp
from aiohttp import web
from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient


# ============================================================
# CONFIG
# ============================================================

# Fake/reference credentials. Replace with your own.
BOT_TOKEN = "7991103091:AAEwq2gcibypJq2Z9gy9UaagOQ2Z0EENm_Y"

ACCOUNTS = [
    {"name": "Account 1", "api_id": 22476344, "api_hash": "1d98fd4acfbb8bd59c66ba104b423cb9", "session": "mahdiashtian2"},
    {"name": "Account 2", "api_id": 19989378, "api_hash": "b85b6b543d548b6bbfaa41ab619534dd", "session": "mr_noob"},
]

SAVE_DIR = "bhumik"

# Render provides PORT automatically.
PORT = int(os.environ.get("PORT", 10000))

os.makedirs(SAVE_DIR, exist_ok=True)


# ============================================================
# TELEGRAM / TELETHON CLIENTS
# ============================================================

clients = [{"name": a["name"], "client": TelegramClient(a["session"], a["api_id"], a["api_hash"])} for a in ACCOUNTS]
bot = Bot(token=BOT_TOKEN)

# Replaces queue.txt now that both programs are merged.
download_queue = asyncio.Queue()

# Active download progress states.
download_progress = {}


# ============================================================
# TELEGRAM LINK PARSING
# ============================================================

def parse_link(link):
    match = re.search(r"t\.me/(?:c/)?([A-Za-z0-9_]+)/(\d+)", link)

    if not match:
        return None, None

    return match.groups()


# ============================================================
# SEND TELETHON MESSAGE THROUGH THE BOT
# ============================================================

def format_bytes(value):
    value = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.0f} {units[index]}" if index == 0 else f"{value:.2f} {units[index]}"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "Calculating..."
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def make_progress_bar(current, total, width=15):
    if not total:
        return "░" * width
    ratio = min(max(current / total, 0), 1)
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def build_progress_text(state):
    current = state.get("current", 0)
    total = state.get("total", 0)
    speed_bps = state.get("speed_bps", 0)
    percent = (current / total * 100) if total else 0
    if speed_bps > 0 and total > current:
        eta = (total - current) / speed_bps
    elif total and current >= total:
        eta = 0
    else:
        eta = None
    speed_mbps = speed_bps * 8 / 1_000_000
    speed_mbs = speed_bps / (1024 * 1024)
    status = state.get("status", "Downloading")
    return (
        f"⬇️ {status}\n\n"
        f"{make_progress_bar(current, total)}  {percent:.1f}%\n\n"
        f"📦 {format_bytes(current)} / {format_bytes(total)}\n"
        f"⚡ {speed_mbs:.2f} MB/s ({speed_mbps:.2f} Mbps)\n"
        f"⏳ ETA: {format_eta(eta)}"
    )


def progress_keyboard(progress_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{progress_id}")]])


async def refresh_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    progress_id = query.data.split(":", 1)[1]
    state = download_progress.get(progress_id)

    if not state:
        await query.answer("This download is no longer active.", show_alert=False)
        return

    await query.answer("Refreshed")
    try:
        await query.edit_message_text(
            build_progress_text(state),
            reply_markup=progress_keyboard(progress_id),
        )
    except Exception as exc:
        if "Message is not modified" not in str(exc):
            print("Refresh progress error:", exc)


async def safe_delete_bot_message(chat_id, message_id):
    """Delete a temporary bot message without breaking the job if Telegram rejects it."""
    if not message_id:
        return

    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception as exc:
        print(
            f"Could not delete temporary message "
            f"{chat_id}/{message_id}: {exc}"
        )


async def send_message_to_user(user_id, msg):
    downloaded_path = None
    progress_id = None
    progress_message = None

    try:
        if msg.text and not msg.media:
            await bot.send_message(chat_id=user_id, text=msg.text)
            return

        if msg.media:
            progress_id = secrets.token_hex(4)
            state = {
                "current": 0,
                "total": 0,
                "speed_bps": 0,
                "status": "Starting download...",
                "started_at": time.perf_counter(),
                "last_update": 0.0,
            }
            download_progress[progress_id] = state

            progress_message = await bot.send_message(
                chat_id=user_id,
                text=build_progress_text(state),
                reply_markup=progress_keyboard(progress_id),
            )

            async def progress_callback(current, total):
                now = time.perf_counter()
                elapsed = max(now - state["started_at"], 0.001)
                state["current"] = current
                state["total"] = total or state["total"]
                state["speed_bps"] = current / elapsed
                state["status"] = "Downloading..."

                should_update = now - state["last_update"] >= 2.0 or (total and current >= total)
                if not should_update:
                    return
                state["last_update"] = now

                try:
                    await progress_message.edit_text(
                        build_progress_text(state),
                        reply_markup=progress_keyboard(progress_id),
                    )
                except Exception as exc:
                    if "Message is not modified" not in str(exc):
                        print("Progress update error:", exc)

            downloaded_path = await msg.download_media(
                file=SAVE_DIR,
                progress_callback=progress_callback,
            )

            if downloaded_path:
                state["status"] = "Download complete"
                if state["total"]:
                    state["current"] = state["total"]
                try:
                    await progress_message.edit_text(
                        build_progress_text(state) + "\n\n📤 Sending to Telegram...",
                        reply_markup=progress_keyboard(progress_id),
                    )
                except Exception:
                    pass

                caption = msg.text or ""
                sent_message = None

                with open(downloaded_path, "rb") as media_file:
                    lower_path = downloaded_path.lower()

                    if lower_path.endswith((".jpg", ".jpeg", ".png")):
                        sent_message = await bot.send_photo(
                            chat_id=user_id,
                            photo=media_file,
                            caption=caption,
                        )
                    elif lower_path.endswith((".mp4", ".mkv")):
                        sent_message = await bot.send_video(
                            chat_id=user_id,
                            video=media_file,
                            caption=caption,
                        )
                    else:
                        sent_message = await bot.send_document(
                            chat_id=user_id,
                            document=media_file,
                            caption=caption,
                        )

                # Telegram has accepted the final media message.
                print(
                    "Sent:",
                    downloaded_path,
                    "message_id:",
                    getattr(sent_message, "message_id", None),
                )

                state["status"] = "Completed"

                # Remove the local Render copy immediately after a successful send.
                if downloaded_path and os.path.exists(downloaded_path):
                    try:
                        os.remove(downloaded_path)
                        print("Deleted local media:", downloaded_path)
                        downloaded_path = None
                    except OSError as exc:
                        print("Local media cleanup error:", exc)

                # Remove the temporary progress bar/Refresh message.
                if progress_message:
                    await safe_delete_bot_message(
                        user_id,
                        progress_message.message_id,
                    )
                    progress_message = None

    except Exception as exc:
        print("Error while sending message:", exc)
        if progress_id and progress_id in download_progress:
            download_progress[progress_id]["status"] = "Failed"
        if progress_message:
            try:
                await progress_message.edit_text(
                    "❌ Download failed.\n\n" + build_progress_text(download_progress.get(progress_id, {}))
                )
            except Exception:
                pass
        raise

    finally:
        if downloaded_path and os.path.exists(downloaded_path):
            try:
                os.remove(downloaded_path)
            except OSError:
                pass
        if progress_id:
            download_progress.pop(progress_id, None)


async def find_accessible_client(chat_id, msg_id):
    for account in clients:
        try:
            msg = await account["client"].get_messages(chat_id, ids=msg_id)
            if msg:
                print(f'{account["name"]} can access {chat_id}/{msg_id}')
                return account, msg
        except Exception as exc:
            print(f'{account["name"]} cannot access {chat_id}/{msg_id}: {exc}')
    return None, None


async def process_message_with_client(account, chat_id, msg_id, user_id):
    try:
        msg = await account["client"].get_messages(chat_id, ids=msg_id)
        if msg:
            await send_message_to_user(user_id, msg)
            return True
    except Exception as exc:
        print(f'{account["name"]} failed on message {msg_id}: {exc}')
    return False


# ============================================================
# BOT COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot is running.\n\n"
        "Usage:\n"
        "/download <telegram_link>\n"
        "/download <telegram_link> <count>\n"
        "/speedtest - test Render server speed"
    )


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/download <link>\n"
            "/download <link> <count>"
        )
        return

    link = context.args[0]
    count = 1

    if len(context.args) > 1:
        try:
            count = int(context.args[1])

            if count < 1:
                raise ValueError

        except ValueError:
            await update.message.reply_text(
                "Count must be a positive number."
            )
            return

    chat_part, start_id = parse_link(link)

    if not chat_part:
        await update.message.reply_text(
            "Invalid Telegram message link."
        )
        return

    user_id = update.effective_chat.id

    queued_message = await update.message.reply_text(
        f"✅ Queued ({count} message(s))"
    )

    await download_queue.put(
        {
            "link": link,
            "count": count,
            "user_id": user_id,
            # Only bot-generated temporary messages are tracked.
            # The user's original /download message is never deleted.
            "temp_message_ids": [queued_message.message_id],
        }
    )



# ============================================================
# SPEED TEST COMMAND
# ============================================================

async def speedtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text(
        "🚀 Testing Render server speed...\n\n"
        "⬇️ Download: testing...\n"
        "⬆️ Upload: waiting..."
    )

    download_mbps = None
    upload_mbps = None

    try:
        test_size = 10_000_000
        url = f"https://speed.cloudflare.com/__down?bytes={test_size}"

        start_time = time.perf_counter()
        received = 0

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                response.raise_for_status()

                async for chunk in response.content.iter_chunked(1024 * 1024):
                    received += len(chunk)

        elapsed = max(time.perf_counter() - start_time, 0.001)
        download_mbps = (received * 8) / elapsed / 1_000_000

        await status.edit_text(
            "🚀 Testing Render server speed...\n\n"
            f"⬇️ Download: {download_mbps:.2f} Mbps\n"
            "⬆️ Upload: testing..."
        )

    except Exception as e:
        print("Download speed test error:", repr(e))

        await status.edit_text(
            "🚀 Testing Render server speed...\n\n"
            "⬇️ Download: failed\n"
            "⬆️ Upload: testing..."
        )

    try:
        upload_size = 5_000_000
        payload = b"0" * upload_size
        url = "https://speed.cloudflare.com/__up"

        start_time = time.perf_counter()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=payload,
                timeout=30,
                headers={
                    "Content-Type": "application/octet-stream",
                    "User-Agent": "Mozilla/5.0"
                }
            ) as response:
                response.raise_for_status()
                await response.read()

        elapsed = max(time.perf_counter() - start_time, 0.001)
        upload_mbps = (upload_size * 8) / elapsed / 1_000_000

    except Exception as e:
        print("Upload speed test error:", repr(e))

    download_text = (
        f"{download_mbps:.2f} Mbps"
        if download_mbps is not None
        else "Failed"
    )

    upload_text = (
        f"{upload_mbps:.2f} Mbps"
        if upload_mbps is not None
        else "Failed"
    )

    await status.edit_text(
        "🚀 Render Server Speed Test\n\n"
        f"⬇️ Download: {download_text}\n"
        f"⬆️ Upload: {upload_text}\n\n"
        "This measures the Render server connection."
    )


# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker():
    print("Download worker started.")

    while True:
        job = await download_queue.get()
        job_succeeded = False

        try:
            link = job["link"]
            count = job["count"]
            user_id = job["user_id"]
            temp_message_ids = job.setdefault("temp_message_ids", [])

            chat_part, start_id = parse_link(link)

            if not chat_part:
                await bot.send_message(
                    chat_id=user_id,
                    text="❌ Invalid Telegram link.",
                )
                continue

            chat_id = (
                int(f"-100{chat_part}")
                if chat_part.isdigit()
                else chat_part
            )

            current = int(start_id)
            selected, first_msg = await find_accessible_client(
                chat_id,
                current,
            )

            if not selected:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ None of the configured Telegram accounts "
                        "can access this message."
                    ),
                )
                continue

            account_message = await bot.send_message(
                chat_id=user_id,
                text=(
                    f'🔐 Using {selected["name"]}\n'
                    f'✅ Download started ({count} message(s))'
                ),
            )
            temp_message_ids.append(account_message.message_id)

            downloaded = 0

            try:
                await send_message_to_user(user_id, first_msg)
                downloaded = 1
                current += 1
            except Exception:
                pass

            while downloaded < count:
                ok = await process_message_with_client(
                    selected,
                    chat_id,
                    current,
                    user_id,
                )

                if not ok:
                    fallback, fallback_msg = await find_accessible_client(
                        chat_id,
                        current,
                    )

                    if fallback and fallback_msg:
                        selected = fallback

                        try:
                            await send_message_to_user(
                                user_id,
                                fallback_msg,
                            )
                            ok = True
                        except Exception as exc:
                            print("Fallback send failed:", exc)

                if ok:
                    downloaded += 1

                current += 1
                await asyncio.sleep(1)

            job_succeeded = downloaded >= count

            if job_succeeded:
                # Successful job: remove all bot-generated queue/account
                # status messages. The user's /download command and the
                # delivered media/messages remain in chat.
                for message_id in list(temp_message_ids):
                    await safe_delete_bot_message(
                        user_id,
                        message_id,
                    )

                temp_message_ids.clear()

        except Exception as exc:
            print("Worker error:", exc)

            try:
                await bot.send_message(
                    chat_id=job["user_id"],
                    text=f"❌ Download failed: {exc}",
                )
            except Exception:
                pass

        finally:
            # Defensive filesystem cleanup. This only removes temporary
            # downloaded media inside SAVE_DIR. It never touches .session files.
            try:
                if os.path.isdir(SAVE_DIR):
                    for filename in os.listdir(SAVE_DIR):
                        path = os.path.join(SAVE_DIR, filename)

                        if os.path.isfile(path):
                            try:
                                os.remove(path)
                                print("Deleted leftover local file:", path)
                            except OSError as exc:
                                print("Leftover cleanup error:", exc)
            except Exception as exc:
                print("SAVE_DIR cleanup error:", exc)

            download_queue.task_done()


# ============================================================
# HTTP SERVER FOR RENDER
# ============================================================

async def ping(request):
    return web.json_response(
        {
            "status": "ok",
            "message": "Bot is running",
        }
    )


async def home(request):
    return web.json_response(
        {
            "service": "Telegram downloader bot",
            "status": "online",
            "ping": "/ping",
        }
    )


async def start_web_server():
    app = web.Application()

    app.router.add_get("/", home)
    app.router.add_get("/ping", ping)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    print(f"HTTP server running on port {PORT}")

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():
    # Connect every configured Telethon account WITHOUT interactive login.
    # Each .session file must already exist and be authorized.
    for account in clients:
        telethon_client = account["client"]

        await telethon_client.connect()

        if not await telethon_client.is_user_authorized():
            session_file = getattr(
                telethon_client.session,
                "filename",
                "configured .session file",
            )
            raise RuntimeError(
                f'{account["name"]} session is missing or not authorized: '
                f'{session_file}. Interactive phone/OTP login is disabled.'
            )

        me = await telethon_client.get_me()
        print(
            f'{account["name"]} logged in as '
            f'{getattr(me, "id", "unknown")}'
        )

    # Start HTTP server required by Render Web Service.
    web_runner = await start_web_server()

    # Build and manually start python-telegram-bot so it can coexist
    # with Telethon and aiohttp in the same asyncio event loop.
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("download", download))
    application.add_handler(CommandHandler("speedtest", speedtest_command))
    application.add_handler(CallbackQueryHandler(refresh_progress, pattern=r"^refresh:"))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    worker_task = asyncio.create_task(download_worker())

    print("🤖 Bot running...")
    print("🌐 /ping endpoint is ready.")

    try:
        # Keep the service alive.
        await asyncio.Event().wait()

    finally:
        worker_task.cancel()

        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        await application.updater.stop()
        await application.stop()
        await application.shutdown()

        for account in clients:
            await account["client"].disconnect()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
