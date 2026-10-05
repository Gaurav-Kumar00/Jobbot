"""Ordered provider fallback with per-run exhaustion (free tiers run out; that's normal)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from jobbot.llm.client import LLMError, LLMRateLimited, Provider
from jobbot.models import AIInsight, Job
from jobbot.settings import Settings

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
MAX_RETRY_WAIT = 20.0  # seconds; longer waits mean the quota is gone for now

Sleep = Callable[[float], Awaitable[None]]


class LLMChain:
    def __init__(self, providers: list[Provider], *, sleep: Sleep = asyncio.sleep) -> None:
        self.providers = providers
        self._exhausted: set[str] = set()
        self._sleep = sleep
        self.errors: list[str] = []

    @property
    def available(self) -> bool:
        return any(self._key(p) not in self._exhausted for p in self.providers)

    async def extract(self, http: httpx.AsyncClient, job: Job) -> AIInsight | None:
        """First provider that answers wins; None if all failed (caller keeps the rules)."""
        for provider in self.providers:
            if self._key(provider) in self._exhausted:
                continue
            for attempt in range(2):
                try:
                    return await provider.extract(http, job)
                except LLMRateLimited as exc:
                    wait = exc.retry_after
                    if attempt == 0 and wait is not None and wait <= MAX_RETRY_WAIT:
                        await self._sleep(wait)
                        continue
                    self._exhausted.add(self._key(provider))  # quota gone for this run
                    self.errors.append(str(exc))
                    break
                except LLMError as exc:
                    self.errors.append(str(exc))
                    log.warning("llm call failed", extra={"error": str(exc)})
                    break
        return None

    @staticmethod
    def _key(provider: Provider) -> str:
        return f"{provider.name}:{provider.model}"


def build_chain(settings: Settings) -> LLMChain | None:
    """Providers from settings, in order. None when no key is configured (LLM off)."""
    providers: list[Provider] = []
    if settings.groq_api_key and settings.groq_api_key.get_secret_value().strip():
        key = settings.groq_api_key.get_secret_value().strip()
        # Each Groq model has its own free quota, so the bigger model is a real fallback.
        for model in ("openai/gpt-oss-20b", "openai/gpt-oss-120b"):
            providers.append(Provider("groq", GROQ_URL, model, key, reasoning_effort="low"))
    if settings.gemini_api_key and settings.gemini_api_key.get_secret_value().strip():
        providers.append(
            Provider(
                "gemini",
                GEMINI_URL,
                settings.gemini_model,
                settings.gemini_api_key.get_secret_value().strip(),
                strict_schema=False,
            )
        )
    return LLMChain(providers) if providers else None
