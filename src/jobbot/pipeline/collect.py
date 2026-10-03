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


def _normalize_all(jobs: list[Job], target: CompanyTarget) -> tuple[list[Job], int]:
    """Normalise each job; one unparseable posting is skipped, never fatal."""
    out: list[Job] = []
    broken = 0
    for job in jobs:
        try:
            out.append(normalize_job(job))
        except Exception:
            broken += 1
            log.exception("normaliser failed", extra={"job": job.id})
    return out, broken


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
                if any(job.source != target.ats for job in result.jobs):
                    # Closing stale postings relies on job.source == target.ats.
                    outcome = TargetOutcome(target, error="adapter bug: job.source != ats name")
                else:
                    jobs, broken = _normalize_all(result.jobs, target)
                    outcome = TargetOutcome(target, jobs=jobs, skipped=result.skipped + broken)
            except FetchError as exc:
                outcome = TargetOutcome(target, error=str(exc))
            except Exception as exc:  # a bug in one adapter must not kill the run
                log.exception("source crashed", extra={"source": f"{target.ats}:{target.key}"})
                outcome = TargetOutcome(target, error=f"crashed: {type(exc).__name__}: {exc}")
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
