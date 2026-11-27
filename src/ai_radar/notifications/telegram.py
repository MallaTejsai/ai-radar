"""Minimal, honest Telegram Bot API client.

Failure classification matters more than the happy path here:

* ``rate_limit``  - 429 with ``retry_after``; the queue waits, nothing is lost.
* ``unauthorized``- bad token or chat id; retrying cannot help, stop and report.
* ``bad_request`` - 400, usually malformed markup; permanent for this message.
* ``unavailable``  - 5xx before acceptance; safe to retry later.
* ``unknown``      - timeout/reset **after** the request was sent. Telegram may or
  may not have delivered it. This is reported, never auto-retried.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient

logger = get_logger(__name__)

API_ROOT = "https://api.telegram.org"
MAX_TEXT_LENGTH = 4096


class TelegramError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status

    @property
    def ambiguous(self) -> bool:
        """True when delivery may or may not have happened."""
        return self.kind == "unknown"


class TelegramClient:
    """Thin wrapper over ``sendMessage`` / ``getMe``."""

    def __init__(
        self,
        client: SafeHttpClient,
        bot_token: str,
        chat_id: str,
        *,
        min_interval_seconds: float = 1.2,
    ) -> None:
        self._client = client
        self._token = bot_token
        self._chat_id = chat_id
        self._min_interval = min_interval_seconds
        self._last_send = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token}/{method}"

    async def send_message(self, text: str, *, disable_preview: bool = True) -> int:
        """Send one message and return its ``message_id``."""
        if not self.configured:
            raise TelegramError("TELEGRAM_BOT_TOKEN/CHAT_ID not configured", kind="unauthorized")
        if not text.strip():
            raise TelegramError("refusing to send an empty message", kind="bad_request")
        if len(text) > MAX_TEXT_LENGTH:
            raise TelegramError(
                f"message exceeds Telegram's {MAX_TEXT_LENGTH} character limit", kind="bad_request"
            )

        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }

        async with self._lock:
            await self._respect_interval()
            try:
                response = await self._client.post_json(self._url("sendMessage"), payload=payload)
            except FetchError as exc:
                # A timeout may occur after Telegram accepted the message.
                kind = "unknown" if exc.kind in {"timeout", "transport"} else "unavailable"
                raise TelegramError(f"send failed: {exc}", kind=kind) from exc
            finally:
                self._last_send = asyncio.get_running_loop().time()

        data = response.data if isinstance(response.data, dict) else {}

        if response.status == 200 and data.get("ok"):
            result = data.get("result") or {}
            message_id = result.get("message_id")
            if not isinstance(message_id, int):
                raise TelegramError("Telegram accepted the message but returned no id", kind="unknown")
            return message_id

        if response.status == 429:
            retry_after = None
            parameters = data.get("parameters") or {}
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), (int, float)):
                retry_after = float(parameters["retry_after"])
            elif response.retry_after is not None:
                retry_after = response.retry_after
            raise TelegramError(
                f"rate limited by Telegram (retry_after={retry_after})",
                kind="rate_limit",
                retry_after=retry_after,
                status=429,
            )
        if response.status in (401, 404):
            raise TelegramError(
                f"Telegram rejected the credentials ({response.status})",
                kind="unauthorized",
                status=response.status,
            )
        if response.status == 400:
            description = str(data.get("description") or "bad request")
            raise TelegramError(f"Telegram rejected the payload: {description}", kind="bad_request", status=400)
        if response.status >= 500:
            raise TelegramError(
                f"Telegram upstream error {response.status}", kind="unavailable", status=response.status
            )
        raise TelegramError(f"unexpected Telegram status {response.status}", kind="unavailable")

    async def get_me(self) -> dict[str, Any]:
        import json

        try:
            response = await self._client.get(self._url("getMe"))
        except FetchError as exc:
            # A 404 usually means a mistyped or revoked token; 401 the same. Without
            # this translation a bad token escapes as a traceback instead of a
            # readable doctor line.
            kind = "unauthorized" if exc.status in (401, 404) else "unavailable"
            raise TelegramError(f"getMe failed: {exc}", kind=kind) from exc
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise TelegramError("getMe returned invalid JSON", kind="unavailable") from exc
        if not data.get("ok"):
            raise TelegramError(
                str(data.get("description") or "getMe failed"),
                kind="unauthorized" if response.status in (401, 404) else "unavailable",
            )
        return data.get("result") or {}

    async def get_chat(self, chat_id: str | None = None) -> dict[str, Any]:
        """Look up a chat **without sending anything**.

        Used by ``ai-radar notify --check``: ``getChat`` is read-only, so a chat id
        can be validated while the notification gate is still closed. A message is
        never delivered by this method.
        """
        import json
        from urllib.parse import quote

        target = (chat_id or self._chat_id or "").strip()
        if not target:
            raise TelegramError("TELEGRAM_CHAT_ID is not configured", kind="not_configured")

        url = f"{self._url('getChat')}?chat_id={quote(target, safe='')}"
        try:
            response = await self._client.get(url)
        except FetchError as exc:
            # SafeHttpClient raises on >=400, so a "chat not found" 400 arrives
            # here rather than as a parsed body. It is permanent for this config.
            if exc.status == 400:
                kind = "bad_request"
            elif exc.status in (401, 404):
                kind = "unauthorized"
            else:
                kind = "unavailable"
            raise TelegramError(f"getChat failed: {exc}", kind=kind) from exc
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise TelegramError("getChat returned invalid JSON", kind="unavailable") from exc

        if not data.get("ok"):
            description = str(data.get("description") or "getChat failed")
            if response.status == 400:
                # "chat not found" / "bot was blocked" is a configuration error and
                # will fail identically every time, so it is not retryable.
                kind = "bad_request"
            elif response.status in (401, 404):
                kind = "unauthorized"
            else:
                kind = "unavailable"
            raise TelegramError(description, kind=kind, status=response.status)
        return data.get("result") or {}

    async def _respect_interval(self) -> None:
        now = asyncio.get_running_loop().time()
        wait = self._min_interval - (now - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)
