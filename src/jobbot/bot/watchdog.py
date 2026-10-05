"""Hourly tick (called by an external free cron, e.g. cron-job.org, via /api/tick).

1. Reliable cadence: GitHub's scheduler skips runs on busy days. If no scan has started
   in the last 50 minutes and none is queued/running, start one (intervals still apply).
2. Dead-man's switch: if no scan has *succeeded* for 3 hours, warn on Telegram once,
   and say when scanning recovers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from jobbot.notify.base import Notifier, OutgoingMessage
from jobbot.storage.base import Repository

log = logging.getLogger(__name__)

RESCAN_AFTER = timedelta(minutes=50)
STALE_AFTER = timedelta(hours=3)
META_KEY = "watchdog"


async def tick(
    repo: Repository,
    notifier: Notifier,
    dispatch: Callable[[bool], Awaitable[str]],
    now: datetime,
    in_flight: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    runs = repo.latest_runs(20)
    last_start = runs[0].started_at if runs else None
    result: dict[str, Any] = {"dispatched": False}

    overdue = last_start is None or now - last_start >= RESCAN_AFTER
    if overdue and in_flight is not None and await in_flight():
        result["in_flight"] = True  # a scan is queued/running; don't stack another
    elif overdue:
        try:
            await dispatch(False)
            result["dispatched"] = True
        except Exception as exc:  # report, but still run the watchdog below
            result["dispatch_error"] = str(exc)
            log.warning("tick dispatch failed", extra={"error": str(exc)})

    healthy = [r for r in runs if r.finished_at and (r.stats.get("sources_ok") or 0) > 0]
    last_ok = healthy[0].finished_at if healthy else None
    stale = last_ok is None or now - last_ok >= STALE_AFTER
    state = repo.get_meta(META_KEY) or {}
    if stale and not state.get("alerting"):
        since = (
            "ever" if last_ok is None else f"{int((now - last_ok).total_seconds() // 3600)} hours"
        )
        await notifier.send(
            OutgoingMessage(
                f"🚨 <b>No successful scan for {since}.</b>\n"
                "I've asked GitHub to start one. If this repeats, check the Actions tab "
                "(scheduled workflows can be disabled after 60 days without commits)."
            )
        )
        repo.set_meta(META_KEY, {"alerting": True, "since": now.isoformat()})
    elif not stale and state.get("alerting"):
        await notifier.send(OutgoingMessage("✅ Scanning is healthy again."))
        repo.set_meta(META_KEY, {"alerting": False, "since": now.isoformat()})
    result["last_success_minutes"] = (
        None if last_ok is None else int((now - last_ok).total_seconds() // 60)
    )
    result["stale"] = stale
    return result
