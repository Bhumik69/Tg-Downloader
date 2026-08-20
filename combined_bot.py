import asyncio
import os
import re
import time

import aiohttp
from aiohttp import web
from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from telethon import TelegramClient


# ============================================================
# CONFIG
# ============================================================

# Fake/reference credentials. Replace with your own.
BOT_TOKEN = "7991103091:AAHUauqaRLSAiwy3U2NOagCiDfDh1UBEcF0"

ACCOUNTS = [
    {"name": "Account 1", "api_id": 22476344, "api_hash": "1d98fd4acfbb8bd59c66ba104b423cb9", "session": "MahdiAshtian"},
    {"name": "Account 2", "api_id": 19989378, "api_hash": "b85b6b543d548b6bbfaa41ab619534dd", "session": "user_session"},
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

async def send_message_to_user(user_id, msg):
    downloaded_path = None

    try:
        # Send text-only messages.
        if msg.text and not msg.media:
            await bot.send_message(
                chat_id=user_id,
                text=msg.text,
            )

        # Download and send media.
        if msg.media:
            downloaded_path = await msg.download_media(file=SAVE_DIR)

            if downloaded_path:
                caption = msg.text or ""

                with open(downloaded_path, "rb") as media_file:
                    lower_path = downloaded_path.lower()

                    if lower_path.endswith((".jpg", ".jpeg", ".png")):
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=media_file,
                            caption=caption,
                        )

                    elif lower_path.endswith((".mp4", ".mkv")):
                        await bot.send_video(
                            chat_id=user_id,
                            video=media_file,
                            caption=caption,
                        )

                    else:
                        await bot.send_document(
                            chat_id=user_id,
                            document=media_file,
                            caption=caption,
                        )

                print("Sent:", downloaded_path)

    except Exception as exc:
        print("Error while sending message:", exc)
        raise

    finally:
        # Prevent downloaded media from filling Render's filesystem.
        if downloaded_path and os.path.exists(downloaded_path):
            try:
                os.remove(downloaded_path)
            except OSError:
                pass


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

    await download_queue.put(
        {
            "link": link,
            "count": count,
            "user_id": user_id,
        }
    )

    await update.message.reply_text(
        f"✅ Queued ({count} message(s))"
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
        try:
            link, count, user_id = job["link"], job["count"], job["user_id"]
            chat_part, start_id = parse_link(link)
            if not chat_part:
                await bot.send_message(chat_id=user_id, text="❌ Invalid Telegram link.")
                continue
            chat_id = int(f"-100{chat_part}") if chat_part.isdigit() else chat_part
            current = int(start_id)
            selected, first_msg = await find_accessible_client(chat_id, current)
            if not selected:
                await bot.send_message(chat_id=user_id, text="❌ None of the configured Telegram accounts can access this message.")
                continue
            await bot.send_message(chat_id=user_id, text=f'🔐 Using {selected["name"]}\n✅ Download started ({count} message(s))')
            downloaded = 0
            try:
                await send_message_to_user(user_id, first_msg)
                downloaded, current = 1, current + 1
            except Exception:
                pass
            while downloaded < count:
                ok = await process_message_with_client(selected, chat_id, current, user_id)
                if not ok:
                    fallback, msg = await find_accessible_client(chat_id, current)
                    if fallback and msg:
                        selected = fallback
                        try:
                            await send_message_to_user(user_id, msg)
                            ok = True
                        except Exception as exc:
                            print("Fallback send failed:", exc)
                if ok:
                    downloaded += 1
                current += 1
                await asyncio.sleep(1)
            await bot.send_message(chat_id=user_id, text=f'✅ Finished sending {downloaded} message(s).\nLast account used: {selected["name"]}')
        except Exception as exc:
            print("Worker error:", exc)
            try:
                await bot.send_message(chat_id=job["user_id"], text=f"❌ Download failed: {exc}")
            except Exception:
                pass
        finally:
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
