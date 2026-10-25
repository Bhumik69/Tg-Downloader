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
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
)
from gen_session import SessionLogin


# ============================================================
# CONFIG
# ============================================================

# Keep credentials in the host's environment settings, never in the repository.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

ACCOUNTS = [
    {"name": "Account 1", "api_id": os.environ.get("TG_API_ID_1", ""), "api_hash": os.environ.get("TG_API_HASH_1", ""), "session": "mahdiashtian2"},
    {"name": "Account 2", "api_id": os.environ.get("TG_API_ID_2", ""), "api_hash": os.environ.get("TG_API_HASH_2", ""), "session": "mr_noob"},
]

SAVE_DIR = "bhumik"
SESSION_DIR = os.environ.get("SESSION_DIR", ".")
ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

GENERATE_ACCOUNT, GENERATE_PHONE, GENERATE_CODE, GENERATE_PASSWORD = range(4)
session_logins = {}

# Render provides PORT automatically.
PORT = int(os.environ.get("PORT", 10000))

os.makedirs(SAVE_DIR, exist_ok=True)


# ============================================================
# TELEGRAM / TELETHON CLIENTS
# ============================================================

clients = []
bot = None

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
        if not account["available"]:
            continue
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
    unavailable = [account["name"] for account in clients if not account["available"]]
    session_notice = ""
    if unavailable:
        session_notice = (
            "\n\nSession unavailable or expired for: "
            + ", ".join(unavailable)
            + ". An authorized admin can create/renew it in a private chat with /generate."
        )
    await update.message.reply_text(
        "Bot is running.\n\n"
        "Usage:\n"
        "/download <telegram_link>\n"
        "/download <telegram_link> <count>\n"
        "/speedtest - test Render server speed"
        + session_notice
    )


def is_session_admin(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id in ADMIN_USER_IDS
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


async def generate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_session_admin(update):
        await update.effective_message.reply_text(
            "Session generation is restricted to configured admins in a private chat."
        )
        return ConversationHandler.END
    if not ADMIN_USER_IDS:
        await update.effective_message.reply_text(
            "Session generation is disabled. Set ADMIN_USER_IDS to your Telegram user ID."
        )
        return ConversationHandler.END

    names = "\n".join(
        f"{index + 1}. {account['name']}"
        for index, account in enumerate(clients)
    )
    await update.effective_message.reply_text(
        "Choose which Telegram user session to create or renew by sending its number:\n"
        f"{names}\n\nSend /cancel to stop."
    )
    return GENERATE_ACCOUNT


async def generate_choose_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_session_admin(update):
        return ConversationHandler.END
    answer = (update.effective_message.text or "").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(clients):
        await update.effective_message.reply_text("Send one of the listed account numbers.")
        return GENERATE_ACCOUNT

    account = clients[int(answer) - 1]
    if account["available"]:
        await update.effective_message.reply_text(
            f"{account['name']} already has an authorized session. Choose an unavailable account or send /cancel."
        )
        return GENERATE_ACCOUNT
    old_client = account["client"]
    try:
        if old_client.is_connected():
            await old_client.disconnect()
    except Exception:
        pass
    account["available"] = False

    config = account["config"]
    login = SessionLogin(
        account["session_path"], config["api_id"], config["api_hash"]
    )
    session_logins[update.effective_user.id] = (account, login)
    context.user_data["session_account"] = account["name"]
    await update.effective_message.reply_text(
        f"Selected {account['name']}. Send its phone number including country code. "
        "Use this only in this private chat."
    )
    return GENERATE_PHONE


async def generate_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_session_admin(update):
        return ConversationHandler.END
    phone = (update.effective_message.text or "").strip()
    await delete_user_input(update)
    item = session_logins.get(update.effective_user.id)
    if not item:
        await send_session_reply(update, "Session setup expired. Run /generate again.")
        return ConversationHandler.END
    try:
        await item[1].request_code(phone)
    except Exception:
        await send_session_reply(
            update,
            "Telegram could not send a login code. Check the phone number and API settings, then run /generate again."
        )
        await discard_session_login(update.effective_user.id)
        return ConversationHandler.END
    await send_session_reply(
        update,
        "Code requested. Send the Telegram login code here. This message will be deleted after processing."
    )
    return GENERATE_CODE


async def generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_session_admin(update):
        return ConversationHandler.END
    code = (update.effective_message.text or "").strip().replace(" ", "")
    await delete_user_input(update)
    item = session_logins.get(update.effective_user.id)
    if not item:
        await send_session_reply(update, "Session setup expired. Run /generate again.")
        return ConversationHandler.END
    try:
        result = await item[1].submit_code(code)
    except PhoneCodeInvalidError:
        await send_session_reply(update, "That code is invalid. Send the current Telegram login code.")
        return GENERATE_CODE
    except PhoneCodeExpiredError:
        await send_session_reply(update, "That code expired. Run /generate again to request a new one.")
        await discard_session_login(update.effective_user.id)
        return ConversationHandler.END
    except Exception:
        await send_session_reply(update, "Telegram rejected the code. Run /generate again.")
        await discard_session_login(update.effective_user.id)
        return ConversationHandler.END

    if result == "password":
        await send_session_reply(
            update,
            "This account has two-step verification enabled. Send its password in this private chat."
        )
        return GENERATE_PASSWORD
    await finish_session_generation(update)
    return ConversationHandler.END


async def generate_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_session_admin(update):
        return ConversationHandler.END
    password = update.effective_message.text or ""
    await delete_user_input(update)
    item = session_logins.get(update.effective_user.id)
    if not item:
        await send_session_reply(update, "Session setup expired. Run /generate again.")
        return ConversationHandler.END
    try:
        await item[1].submit_password(password)
    except PasswordHashInvalidError:
        await send_session_reply(update, "That password was rejected. Send the correct 2FA password.")
        return GENERATE_PASSWORD
    except Exception:
        await send_session_reply(update, "Telegram rejected the password. Run /generate again.")
        await discard_session_login(update.effective_user.id)
        return ConversationHandler.END
    await finish_session_generation(update)
    return ConversationHandler.END


async def delete_user_input(update: Update):
    try:
        await update.effective_message.delete()
    except Exception:
        pass


async def send_session_reply(update: Update, text: str):
    await update.get_bot().send_message(
        chat_id=update.effective_chat.id,
        text=text,
    )


async def finish_session_generation(update: Update):
    item = session_logins.pop(update.effective_user.id, None)
    if not item:
        await update.effective_message.reply_text("Session setup expired. Run /generate again.")
        return
    account, login = item
    try:
        if not await login.is_authorized():
            raise RuntimeError("Authorization did not complete")
        account["client"] = login.client
        account["available"] = True
        await send_session_reply(
            update,
            f"✅ {account['name']} session created and saved. The bot can use it now."
        )
    except Exception:
        await login.close()
        account["available"] = False
        await send_session_reply(
            update,
            "Session setup did not complete. Run /generate again."
        )


async def discard_session_login(user_id):
    item = session_logins.pop(user_id, None)
    if item:
        try:
            await item[1].close()
        except Exception:
            pass


async def cancel_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await discard_session_login(update.effective_user.id)
    await update.effective_message.reply_text("Session generation cancelled.")
    return ConversationHandler.END


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not any(account["available"] for account in clients):
        await update.effective_message.reply_text(
            "No Telegram user session is available or authorized. "
            "An admin must open a private chat with the bot and run /generate."
        )
        return
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
                        "can access this message. If a user session is missing or expired, "
                        "an admin can create/renew it in a private chat with /generate."
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
    global bot
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in the host environment before starting the bot.")
    for config in ACCOUNTS:
        if not config["api_id"].isdigit() or not config["api_hash"]:
            raise RuntimeError(
                f"Set TG_API_ID_{ACCOUNTS.index(config) + 1} and "
                f"TG_API_HASH_{ACCOUNTS.index(config) + 1} in the host environment."
            )

    os.makedirs(SESSION_DIR, exist_ok=True)
    clients.extend(
        {
            "name": config["name"],
            "config": {**config, "api_id": int(config["api_id"])},
            "session_path": os.path.join(SESSION_DIR, config["session"]),
            "client": TelegramClient(
                os.path.join(SESSION_DIR, config["session"]),
                int(config["api_id"]),
                config["api_hash"],
            ),
            "available": False,
        }
        for config in ACCOUNTS
    )
    bot = Bot(token=BOT_TOKEN)

    # Start the bot even when user sessions are missing or expired. Admins
    # can create or renew them later with the private /generate flow.
    for account in clients:
        telethon_client = account["client"]
        try:
            await telethon_client.connect()
            if await telethon_client.is_user_authorized():
                account["available"] = True
                me = await telethon_client.get_me()
                print(
                    f'{account["name"]} logged in as '
                    f'{getattr(me, "id", "unknown")}'
                )
            else:
                print(
                    f'{account["name"]} session is missing or expired. '
                    'An admin can create it with /generate in a private chat.'
                )
                await telethon_client.disconnect()
        except Exception as exc:
            account["available"] = False
            print(f'{account["name"]} session unavailable: {type(exc).__name__}')
            try:
                await telethon_client.disconnect()
            except Exception:
                pass

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
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("generate", generate_start)],
            states={
                GENERATE_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_choose_account)],
                GENERATE_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_phone)],
                GENERATE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_code)],
                GENERATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_password)],
            },
            fallbacks=[CommandHandler("cancel", cancel_generate)],
            per_user=True,
            per_chat=True,
        )
    )
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
