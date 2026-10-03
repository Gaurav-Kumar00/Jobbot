"""Shared outbound HTTP for job sources: timeouts, retries with backoff, politeness.

Every source goes through `HttpClient`, so retry and rate-limit behaviour is uniform
and one misbehaving host can't monopolise the run.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobbot import __version__
from jobbot.timeutil import utcnow

log = logging.getLogger(__name__)

USER_AGENT = f"JobBot/{__version__} (personal, non-commercial job alerts)"
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0

Sleep = Callable[[float], Awaitable[None]]


class FetchError(Exception):
    """A request failed permanently (after retries, or with a non-retryable status)."""

    def __init__(self, url: str, reason: str, status_code: int | None = None):
        self.url = url
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"{reason} for {_host_path(url)}")


class HttpClient:
    def __init__(
        self,
        *,
        timeout: httpx.Timeout | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        per_host_concurrency: int = 4,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._per_host = per_host_concurrency
        self._host_limits: dict[str, asyncio.Semaphore] = {}
        self._sleep = sleep
        self._jitter = jitter

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return _decode_json(url, await self.request("GET", url, **kwargs))

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return _decode_json(url, await self.request("POST", url, json=payload, **kwargs))

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        limit = self._host_limits.setdefault(host, asyncio.Semaphore(self._per_host))
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt == self._max_retries
            try:
                async with limit:
                    resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                reason = f"network error ({type(exc).__name__})"
                if last_attempt:
                    raise FetchError(url, reason) from None
                await self._wait(url, attempt, reason, None)
                continue

            if resp.is_success:
                return resp
            reason = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUSES or last_attempt:
                raise FetchError(url, reason, resp.status_code)
            await self._wait(url, attempt, reason, _retry_after(resp))

        raise AssertionError("unreachable")  # pragma: no cover

    async def _wait(self, url: str, attempt: int, reason: str, retry_after: float | None) -> None:
        if retry_after:  # "Retry-After: 0" (Workable sends it) means "use your own backoff"
            wait = min(retry_after, MAX_RETRY_AFTER_SECONDS)
        else:
            exp = min(self._backoff_cap, self._backoff_base * 2**attempt)
            wait = exp + self._jitter() * self._backoff_base
        log.warning(
            "http retry",
            extra={
                "target": _host_path(url),
                "reason": reason,
                "attempt": attempt + 1,
                "wait": wait,
            },
        )
        await self._sleep(wait)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None


def _decode_json(url: str, resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        raise FetchError(url, "invalid JSON response", resp.status_code) from None


def _host_path(url: str) -> str:
    """URL without query string (which may carry API keys)."""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"
