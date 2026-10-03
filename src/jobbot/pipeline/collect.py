"""Fetch many companies concurrently with per-source isolation."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize import normalize_job
from jobbot.sources import get_source

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 8


@dataclass
class TargetOutcome:
    target: CompanyTarget
    jobs: list[Job] = field(default_factory=list)
    skipped: int = 0
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


async def collect(
    targets: list[CompanyTarget],
    http: HttpClient,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[TargetOutcome]:
    """Fetch + normalise every target. A failing target never affects the others."""
    gate = asyncio.Semaphore(concurrency)

    async def one(target: CompanyTarget) -> TargetOutcome:
        started = time.monotonic()
        async with gate:
            try:
                result = await get_source(target.ats).fetch(target, http)
            except FetchError as exc:
                outcome = TargetOutcome(target, error=str(exc))
            except Exception as exc:  # a bug in one adapter must not kill the run
                log.exception("source crashed", extra={"source": f"{target.ats}:{target.key}"})
                outcome = TargetOutcome(target, error=f"crashed: {type(exc).__name__}: {exc}")
            else:
                if any(job.source != target.ats for job in result.jobs):
                    # Closing stale postings relies on job.source == target.ats.
                    outcome = TargetOutcome(target, error="adapter bug: job.source != ats name")
                else:
                    outcome = TargetOutcome(
                        target, jobs=[normalize_job(j) for j in result.jobs], skipped=result.skipped
                    )
        outcome.seconds = round(time.monotonic() - started, 2)
        log.info(
            "fetched",
            extra={
                "source": f"{target.ats}:{target.key}",
                "jobs": len(outcome.jobs),
                "error": outcome.error,
                "seconds": outcome.seconds,
            },
        )
        return outcome

    return list(await asyncio.gather(*(one(t) for t in targets)))
