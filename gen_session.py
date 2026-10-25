"""Create and save Telethon user sessions for the downloader.

The bot imports SessionLogin to perform the same login flow through its
admin-only private /generate conversation. Sessions are encrypted and stored
in Upstash Redis. The terminal mode uses the same Upstash configuration.
"""

import asyncio
import getpass
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from session_store import UpstashSessionStore


class SessionLogin:
    """Manage one Telethon phone/code/2FA login and persist its session."""

    def __init__(self, api_id, api_hash):
        self.client = TelegramClient(StringSession(), int(api_id), api_hash)
        self.phone = None
        self.phone_code_hash = None

    async def request_code(self, phone):
        self.phone = phone
        await self.client.connect()
        sent = await self.client.send_code_request(phone)
        self.phone_code_hash = sent.phone_code_hash

    async def submit_code(self, code):
        try:
            await self.client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
            return "authorized"
        except SessionPasswordNeededError:
            return "password"

    async def submit_password(self, password):
        await self.client.sign_in(password=password)

    async def is_authorized(self):
        return await self.client.is_user_authorized()

    def save_string(self):
        return self.client.session.save()

    async def close(self):
        if self.client.is_connected():
            await self.client.disconnect()


def account_configs():
    accounts = []
    for number in (1, 2):
        api_id = os.environ.get(f"TG_API_ID_{number}")
        api_hash = os.environ.get(f"TG_API_HASH_{number}")
        if api_id and api_hash:
            accounts.append(
                {
                    "name": f"Account {number}",
                    "session_key": f"telegram-downloader:session:account{number}",
                    "api_id": int(api_id),
                    "api_hash": api_hash,
                }
            )
    return accounts


async def terminal_login(account, store):
    login = SessionLogin(account["api_id"], account["api_hash"])
    try:
        phone = input(f"{account['name']} phone number (include country code): ").strip()
        await login.request_code(phone)
        code = input("Telegram login code: ").strip()
        result = await login.submit_code(code)
        if result == "password":
            await login.submit_password(getpass.getpass("Two-step verification password: "))
        if not await login.is_authorized():
            raise RuntimeError("Telegram did not authorize this session")
        await store.save_session(account["session_key"], login.save_string())
        print(f"Saved encrypted session for {account['name']} to Upstash Redis.")
    finally:
        await login.close()


async def main():
    store = UpstashSessionStore.from_environment()
    accounts = account_configs()
    if not accounts:
        raise SystemExit(
            "Set TG_API_ID_1/TG_API_HASH_1 and/or TG_API_ID_2/TG_API_HASH_2 first."
        )

    if len(sys.argv) > 1:
        requested = sys.argv[1]
        accounts = [account for account in accounts if account["name"].endswith(requested)]
        if not accounts:
            raise SystemExit("Choose account 1 or 2, with matching API environment variables.")

    for account in accounts:
        await terminal_login(account, store)


if __name__ == "__main__":
    asyncio.run(main())
