"""Storage safety net for the 512 MB Atlas free tier (runs after every scan).

Layer 1 - compaction: inactive jobs lose description/raw (identity + fingerprint stay,
          so dedup keeps working): rejected ones after 7 days, all others after 30.
Layer 2 - retention: jobs inactive for 180 days that never matched/alerted are deleted.
Layer 3 - guard: at 70% a Telegram warning (at most daily); at 85% every inactive job is
          compacted; at 95% active rejected jobs are compacted too. Alerting never stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobbot.notify.base import Notifier, OutgoingMessage
from jobbot.storage.base import Repository

log = logging.getLogger(__name__)

FREE_TIER_BYTES = 512 * 1024 * 1024
COMPACT_REJECTED_AFTER = timedelta(days=7)
COMPACT_ALL_AFTER = timedelta(days=30)
DELETE_AFTER = timedelta(days=180)
WARN_PCT, EMERGENCY_PCT, CRITICAL_PCT = 70.0, 85.0, 95.0
WARNING_COOLDOWN = timedelta(hours=24)
META_KEY = "storage_warning"


@dataclass
class HousekeepingReport:
    compacted: int = 0
    deleted: int = 0
    used_pct: float = 0.0
    warned: bool = False


async def housekeeping(
    repo: Repository,
    notifier: Notifier | None,
    *,
    now: datetime,
    capacity_bytes: int = FREE_TIER_BYTES,
) -> HousekeepingReport:
    report = HousekeepingReport()

    # Layer 1: compaction
    candidates = repo.stale_job_ids(inactive_before=now - COMPACT_REJECTED_AFTER)
    if candidates:
        matches = repo.get_matches(candidates)
        alerts = repo.get_alerts(candidates)
        old = set(repo.stale_job_ids(inactive_before=now - COMPACT_ALL_AFTER))
        compactable = [
            job_id
            for job_id in candidates
            if job_id in old
            or (job_id not in alerts and getattr(matches.get(job_id), "decision", "") != "match")
        ]
        report.compacted += repo.compact_jobs(compactable)

    # Layer 2: retention
    ancient = repo.stale_job_ids(inactive_before=now - DELETE_AFTER, include_compacted=True)
    if ancient:
        alerts = repo.get_alerts(ancient)
        matches = repo.get_matches(ancient)
        doomed = [
            job_id
            for job_id in ancient
            if job_id not in alerts and getattr(matches.get(job_id), "decision", "") != "match"
        ]
        report.deleted = repo.delete_jobs(doomed)

    # Layer 3: guard
    report.used_pct = round(100 * repo.storage_bytes() / capacity_bytes, 1)
    if report.used_pct >= EMERGENCY_PCT:
        report.compacted += repo.compact_jobs(repo.stale_job_ids(inactive_before=now))
    if report.used_pct >= CRITICAL_PCT:
        rejected = [m.job_id for m in repo.list_matches(decision="reject")]
        report.compacted += repo.compact_jobs(rejected)
    if report.used_pct >= WARN_PCT and notifier is not None:
        last = (repo.get_meta(META_KEY) or {}).get("warned_at")
        if last is None or now - datetime.fromisoformat(last) >= WARNING_COOLDOWN:
            await notifier.send(
                OutgoingMessage(
                    f"⚠️ <b>Database {report.used_pct:g}% full</b> (Atlas free tier, 512 MB).\n"
                    f"Old postings are being compacted automatically"
                    f"{' (emergency mode)' if report.used_pct >= EMERGENCY_PCT else ''}. "
                    "Alerts keep working."
                )
            )
            repo.set_meta(META_KEY, {"warned_at": now.isoformat(), "pct": report.used_pct})
            report.warned = True
    log.info("housekeeping", extra=vars(report))
    return report
