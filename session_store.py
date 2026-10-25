"""Encrypted Telethon session storage backed by Upstash Redis REST."""

import binascii
import os

import aiohttp
from cryptography.fernet import Fernet, InvalidToken


class UpstashSessionStore:
    def __init__(self, rest_url, rest_token, encryption_key):
        if not rest_url or not rest_token or not encryption_key:
            raise RuntimeError(
                "Set UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, and "
                "SESSION_ENCRYPTION_KEY in the host environment."
            )
        self.rest_url = rest_url.rstrip("/")
        self.rest_token = rest_token
        try:
            self.cipher = Fernet(encryption_key.encode("ascii"))
        except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
            raise RuntimeError(
                "SESSION_ENCRYPTION_KEY must be a Fernet key. Generate one with "
                "Fernet.generate_key().decode()."
            ) from exc

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("UPSTASH_REDIS_REST_URL", ""),
            os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
            os.environ.get("SESSION_ENCRYPTION_KEY", ""),
        )

    async def _command(self, *parts):
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"Authorization": f"Bearer {self.rest_token}"}
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                self.rest_url,
                json=list(parts),
                headers=headers,
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        if "error" in payload:
            raise RuntimeError("Upstash rejected a session-storage operation.")
        return payload.get("result")

    async def load_session(self, key):
        encrypted = await self._command("GET", key)
        if encrypted is None:
            return None
        try:
            return self.cipher.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "Stored session could not be decrypted. Keep SESSION_ENCRYPTION_KEY "
                "unchanged or generate a new Telegram session."
            ) from exc

    async def save_session(self, key, session_string):
        encrypted = self.cipher.encrypt(session_string.encode("utf-8")).decode("ascii")
        result = await self._command("SET", key, encrypted)
        if result != "OK":
            raise RuntimeError("Upstash did not confirm the session save.")
