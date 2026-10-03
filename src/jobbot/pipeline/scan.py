"""One scan: fetch -> store -> match -> deduplicate openings -> deliver alerts.

Safe to run repeatedly and to crash at any point: an alert is reserved in the
database *before* it is sent, so a job is never alerted twice (see Repository.claim_alert).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobbot.http import HttpClient
from jobbot.matching.preferences import Preferences, Profile
from jobbot.matching.scorer import match_job
from jobbot.models import (
    AlertStatus,
    AlertTrigger,
    CompanyTarget,
    Job,
    MatchResult,
    RemoteScope,
    RunSummary,
    SourceState,
    UpsertOutcome,
)
from jobbot.normalize import NORMALIZER_VERSION, normalize_job
from jobbot.normalize.dedup import choose_canonical
from jobbot.notify.base import Notifier, OutgoingMessage
from jobbot.notify.formatter import format_alert
from jobbot.notify.telegram import TelegramError
from jobbot.pipeline.collect import TargetOutcome, collect
from jobbot.pipeline.housekeeping import housekeeping
from jobbot.storage.base import Repository
from jobbot.timeutil import utcnow

log = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 1.1  # Telegram allows ~1 message/second per chat
BACKFILL_WINDOW = timedelta(hours=48)  # on a company's first scan, only alert recent posts
FAILURE_ALERT_THRESHOLD = 3  # consecutive failed scans before a Telegram warning
DEFAULT_INTERVAL_MINUTES = 60
# GitHub cron starts late, never early; the grace keeps an hourly source due every hour.
INTERVAL_GRACE = timedelta(minutes=10)
Sleep = Callable[[float], Awaitable[None]]


@dataclass
class ScanReport:
    started_at: datetime
    finished_at: datetime | None = None
    sources_due: int = 0
    sources_ok: int = 0
    sources_failed: list[str] = field(default_factory=list)
    fetched: int = 0
    skipped_foreign: int = 0
    new: int = 0
    changed: int = 0
    closed: int = 0
    scored: int = 0
    new_matches: int = 0
    duplicates: int = 0
    alerts_sent: int = 0
    alerts_failed: int = 0
    alerts_uncertain: int = 0
    held_paused: int = 0
    baseline: int = 0
    compacted: int = 0
    deleted: int = 0
    storage_pct: float = 0.0

    def to_run_summary(self, trigger: str) -> RunSummary:
        stats = {
            k: v for k, v in vars(self).items() if isinstance(v, int) and not isinstance(v, bool)
        }
        stats["sources_failed"] = len(self.sources_failed)
        stats["storage_pct_x10"] = round(self.storage_pct * 10)
        return RunSummary(
            started_at=self.started_at,
            finished_at=self.finished_at,
            trigger=trigger,
            stats=stats,
            errors=self.sources_failed[:20],
        )


async def run_scan(
    *,
    repo: Repository,
    http: HttpClient,
    notifier: Notifier | None,
    targets: list[CompanyTarget],
    prefs: Preferences,
    prefs_version: int,
    profile: Profile,
    trigger: str = "manual",
    force: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> ScanReport:
    """`notifier=None` stores and scores but sends nothing (alerts stay pending).

    `force` scans every target now, ignoring per-source intervals.
    """
    now = utcnow()
    report = ScanReport(started_at=now)
    states = {state.key: state for state in repo.list_source_states()}
    due = [t for t in targets if force or is_due(states.get(_key(t)), now, t)]
    report.sources_due = len(due)
    outcomes = await collect(due, http)

    stored: list[Job] = []
    changed_ids: set[str] = set()
    baseline_ids: set[str] = set()
    for outcome in outcomes:
        if not outcome.ok:
            report.sources_failed.append(f"{_key(outcome.target)}: {outcome.error}")
            continue
        report.sources_ok += 1
        report.fetched += len(outcome.jobs)
        keep = [job for job in outcome.jobs if worth_storing(job)]
        report.skipped_foreign += len(outcome.jobs) - len(keep)
        first_scan = not getattr(states.get(_key(outcome.target)), "bootstrapped", False)
        for job, result in zip(keep, repo.upsert_jobs(keep), strict=True):
            if result is UpsertOutcome.NEW:
                report.new += 1
                if first_scan and not _recent(job, now):
                    baseline_ids.add(job.id)  # already open before we started watching
            elif result is UpsertOutcome.CHANGED:
                report.changed += 1
            if result is not UpsertOutcome.UNCHANGED:
                changed_ids.add(job.id)
        # Only a complete, successful fetch can tell us which postings were closed.
        report.closed += repo.mark_missing_inactive(
            outcome.target.ats, outcome.target.key, (j.id for j in outcome.jobs)
        )
        stored.extend(keep)

    fresh = _score(repo, stored, changed_ids, prefs, prefs_version, profile, report)
    by_id = {job.id: job for job in stored}
    for job_id in baseline_ids:
        if fresh.get(job_id) is not None and fresh[job_id].decision == "match":
            report.baseline += repo.suppress_alert(job_id, by_id[job_id].fingerprint)
    await deliver_pending(repo, notifier, prefs, report, trigger=AlertTrigger.NEW, sleep=sleep)
    await _update_source_health(repo, notifier, outcomes, states, now)

    upkeep = await housekeeping(repo, notifier, now=now)
    report.compacted, report.deleted, report.storage_pct = (
        upkeep.compacted,
        upkeep.deleted,
        upkeep.used_pct,
    )
    report.finished_at = utcnow()
    repo.record_run(report.to_run_summary(trigger))
    return report


def is_due(state: SourceState | None, now: datetime, target: CompanyTarget | None = None) -> bool:
    """A per-source override (set from Telegram) beats companies.yaml, which beats 60 min."""
    if state is None or state.last_run_at is None:
        return True
    minutes = (
        state.interval_minutes
        or (target.interval_minutes if target else None)
        or DEFAULT_INTERVAL_MINUTES
    )
    interval = timedelta(minutes=minutes)
    return now - state.last_run_at >= interval - INTERVAL_GRACE


def _key(target: CompanyTarget) -> str:
    return f"{target.ats}:{target.key}"


def _recent(job: Job, now: datetime) -> bool:
    return job.posted_at is not None and now - job.posted_at <= BACKFILL_WINDOW


async def _update_source_health(
    repo: Repository,
    notifier: Notifier | None,
    outcomes: list[TargetOutcome],
    states: dict[str, SourceState],
    now: datetime,
) -> None:
    """Track per-source health; warn once after repeated failures, and on recovery."""
    failing: list[SourceState] = []
    recovered: list[SourceState] = []
    updated: list[SourceState] = []
    for outcome in outcomes:
        key = _key(outcome.target)
        state = (states.get(key) or SourceState(key=key)).model_copy(deep=True)
        state.last_run_at = now
        if outcome.ok:
            if state.failure_notified:
                recovered.append(state)
            state.bootstrapped = True
            state.last_success_at = now
            state.consecutive_failures = 0
            state.last_error = None
            state.failure_notified = False
            state.last_counts = {"fetched": len(outcome.jobs), "skipped": outcome.skipped}
        else:
            state.consecutive_failures += 1
            state.last_error = (outcome.error or "")[:300]
            if state.consecutive_failures >= FAILURE_ALERT_THRESHOLD and not state.failure_notified:
                failing.append(state)
        updated.append(state)

    if notifier is not None and (failing or recovered):
        lines = []
        for state in failing:
            lines.append(
                f"⚠️ <b>{state.key}</b> failed {state.consecutive_failures} scans in a row: "
                f"<i>{_esc(state.last_error or '?')}</i>"
            )
        for state in recovered:
            lines.append(f"✅ <b>{state.key}</b> is working again")
        try:
            await notifier.send(OutgoingMessage("🩺 <b>Source health</b>\n" + "\n".join(lines)))
        except TelegramError as exc:
            log.warning("health message failed", extra={"error": str(exc)})
        else:
            for state in failing:
                state.failure_notified = True
    for state in updated:
        repo.save_source_state(state)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def worth_storing(job: Job) -> bool:
    """Drop postings that are clearly outside India (keeps Atlas M0 small)."""
    loc = job.normalized.location if job.normalized else None
    if loc is None or not loc.countries or "IN" in loc.countries:
        return True
    return loc.remote_scope not in (RemoteScope.NOT_REMOTE, RemoteScope.RESTRICTED)


def _score(
    repo: Repository,
    jobs: list[Job],
    changed_ids: set[str],
    prefs: Preferences,
    prefs_version: int,
    profile: Profile,
    report: ScanReport,
) -> dict[str, MatchResult]:
    existing = repo.get_matches(job.id for job in jobs)
    fresh: list[MatchResult] = []
    for job in jobs:
        previous = existing.get(job.id)
        if job.id in changed_ids or previous is None or previous.prefs_version != prefs_version:
            fresh.append(match_job(job, prefs, profile, prefs_version))
    repo.save_matches(fresh)
    report.scored = len(fresh)
    report.new_matches = sum(1 for m in fresh if m.decision == "match")
    return {m.job_id: m for m in fresh}


async def deliver_pending(
    repo: Repository,
    notifier: Notifier | None,
    prefs: Preferences,
    report: ScanReport,
    *,
    trigger: AlertTrigger,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Send every matched, active, not-yet-alerted opening exactly once."""
    matches = {m.job_id: m for m in repo.list_matches(decision="match")}
    alerts = repo.get_alerts(matches)
    pending_ids = [
        job_id
        for job_id in matches
        if job_id not in alerts or alerts[job_id].status is AlertStatus.FAILED
    ]
    jobs = [
        job for job in repo.get_jobs(pending_ids).values() if job.is_active and not job.duplicate_of
    ]
    to_send = _one_per_opening(repo, jobs, report)
    to_send.sort(key=lambda job: (job.posted_at or job.first_seen_at, job.id))

    if prefs.paused or notifier is None:
        report.held_paused = len(to_send)
        return

    now = utcnow()
    for index, job in enumerate(to_send):
        match = matches[job.id]
        if not repo.claim_alert(job.id, trigger, match.score, fingerprint=job.fingerprint):
            continue  # another run got there first
        try:
            message_id = await notifier.send(format_alert(job, match, now=now))
        except TelegramError as exc:
            if exc.maybe_delivered:
                # Possibly on the user's phone already: leave it "sending", never resend.
                report.alerts_uncertain += 1
                log.warning("alert outcome unknown", extra={"job": job.id, "error": str(exc)})
            else:
                repo.mark_alert_failed(job.id, str(exc))
                report.alerts_failed += 1
                log.warning("alert failed", extra={"job": job.id, "error": str(exc)})
            continue
        repo.mark_alert_sent(job.id, message_id)
        report.alerts_sent += 1
        if index < len(to_send) - 1:
            await sleep(SEND_INTERVAL_SECONDS)


def _one_per_opening(repo: Repository, jobs: list[Job], report: ScanReport) -> list[Job]:
    """Collapse jobs sharing a fingerprint; skip openings already alerted under another id."""
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        groups[job.fingerprint or job.id].append(job)
    already = repo.alerted_fingerprints(groups)
    chosen: list[Job] = []
    for fingerprint, group in groups.items():
        if fingerprint in already:
            canonical_id = already[fingerprint]
            members = group
        else:
            canonical = choose_canonical(group)
            chosen.append(canonical)
            canonical_id = canonical.id
            members = [job for job in group if job.id != canonical.id]
        for job in members:
            repo.set_duplicate_of(job.id, canonical_id)
            report.duplicates += 1
    return chosen


async def realert(
    repo: Repository,
    notifier: Notifier,
    job_id: str,
    prefs: Preferences,
    prefs_version: int,
    profile: Profile,
) -> str:
    """Explicitly re-send one job (the only way a job is ever alerted twice)."""
    job = repo.get_job(job_id)
    if job is None:
        raise LookupError(f"job {job_id} is not stored")
    if job.normalized is None or job.normalized.version != NORMALIZER_VERSION:
        job = normalize_job(job)
    match = match_job(job, prefs, profile, prefs_version)
    if not repo.claim_alert(
        job.id, AlertTrigger.REALERT, match.score, force=True, fingerprint=job.fingerprint
    ):
        raise RuntimeError(f"an alert for {job_id} is already in flight")
    try:
        message_id = await notifier.send(format_alert(job, match, now=utcnow()))
    except TelegramError as exc:
        if not exc.maybe_delivered:
            repo.mark_alert_failed(job.id, str(exc))
        raise
    repo.mark_alert_sent(job.id, message_id)
    return message_id
