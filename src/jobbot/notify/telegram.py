"""Minimal async Telegram Bot API client.

We call the HTTP API directly instead of using a bot framework: the scanner only
sends messages, and the webhook (Phase 8) handles one update per request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jobbot.notify.base import OutgoingMessage

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096
MAX_RETRY_AFTER_SECONDS = 60

Sleep = Callable[[float], Awaitable[None]]
# Failures that happen before the request leaves this machine.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class TelegramError(Exception):
    """A Bot API call failed. The message never contains the bot token."""

    def __init__(
        self,
        method: str,
        description: str,
        status_code: int | None = None,
        *,
        maybe_delivered: bool = False,
    ):
        self.method = method
        self.description = description
        self.status_code = status_code
        # True when the request may have reached Telegram (e.g. timeout waiting for the
        # reply): callers must not resend, or the user could get the message twice.
        self.maybe_delivered = maybe_delivered
        status = f" (HTTP {status_code})" if status_code else ""
        super().__init__(f"{method} failed{status}: {description}")


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._base = f"{API_BASE}/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._owns_http = http is None
        self._max_retries = max_retries
        self._sleep = sleep

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        """Call a Bot API method, retrying on rate limits, 5xx and network errors."""
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt == self._max_retries
            try:
                resp = await self._http.post(f"{self._base}/{method}", json=payload or {})
            except httpx.TransportError as exc:
                if last_attempt:
                    # `from None`: the chained httpx error can include the request URL (token).
                    raise TelegramError(
                        method,
                        f"network error: {type(exc).__name__}",
                        maybe_delivered=not isinstance(exc, _NOT_SENT_ERRORS),
                    ) from None
                await self._backoff(method, attempt, f"network error {type(exc).__name__}")
                continue

            data = _json_or_empty(resp)
            if data.get("ok"):
                return data.get("result")

            description = str(data.get("description") or resp.reason_phrase or "unknown error")
            if resp.status_code == 429 and not last_attempt:
                retry_after = data.get("parameters", {}).get("retry_after", 1)
                wait = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
                log.warning("telegram rate limited", extra={"method": method, "retry_after": wait})
                await self._sleep(wait)
                continue
            if resp.status_code >= 500 and not last_attempt:
                await self._backoff(method, attempt, f"HTTP {resp.status_code}")
                continue
            raise TelegramError(method, description, resp.status_code)

        raise AssertionError("unreachable")  # pragma: no cover

    async def _backoff(self, method: str, attempt: int, reason: str) -> None:
        wait = float(2**attempt)
        log.warning(
            "telegram call retrying", extra={"method": method, "reason": reason, "wait": wait}
        )
        await self._sleep(wait)

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 0}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload)

    async def send_message(
        self,
        chat_id: str | int,
        message: OutgoingMessage,
        *,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        if len(message.text) > MAX_MESSAGE_LEN:
            raise ValueError(
                f"message is {len(message.text)} chars; Telegram max is {MAX_MESSAGE_LEN}"
            )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message.text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": disable_preview},
        }
        if message.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": b.text, "url": b.url} for b in message.buttons]]
            }
        return await self.call("sendMessage", payload)


class TelegramNotifier:
    """`Notifier` implementation that delivers to a single owner chat."""

    def __init__(self, client: TelegramClient, chat_id: str | int) -> None:
        self._client = client
        self._chat_id = chat_id

    async def send(self, message: OutgoingMessage) -> str:
        result = await self._client.send_message(self._chat_id, message)
        return str(result["message_id"])


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
