"""In-memory Repository for tests and dry runs. Never used in production."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from jobbot.models import (
    AlertRecord,
    AlertStatus,
    AlertTrigger,
    Job,
    MatchResult,
    Normalized,
    PreferencesDoc,
    RunSummary,
    SourceState,
    UpsertOutcome,
)
from jobbot.storage.base import StaleVersionError
from jobbot.timeutil import utcnow

# Fields replaced when a posting's content changes; identity/history fields are kept.
CONTENT_FIELDS = (
    "title",
    "url",
    "location_raw",
    "description",
    "posted_at",
    "content_hash",
    "fingerprint",
    "normalized",
    "raw",
    "hints",
)


class MemoryRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.matches: dict[str, MatchResult] = {}
        self.alerts: dict[str, AlertRecord] = {}
        self.source_states: dict[str, SourceState] = {}
        self.runs: list[RunSummary] = []
        self.preferences: PreferencesDoc | None = None
        self.preference_history: list[PreferencesDoc] = []
        self.meta: dict[str, dict[str, Any]] = {}

    # --- lifecycle ---
    def ensure_indexes(self) -> None:
        return None

    def ping(self) -> None:
        return None

    def storage_bytes(self) -> int:
        return 0

    # --- jobs ---
    def upsert_job(self, job: Job) -> UpsertOutcome:
        existing = self.jobs.get(job.id)
        if existing is None:
            self.jobs[job.id] = job.model_copy(deep=True)
            return UpsertOutcome.NEW
        updates: dict[str, Any] = {
            "last_seen_at": job.last_seen_at,
            "is_active": True,
            "normalized": job.normalized,
            "fingerprint": job.fingerprint,
        }
        outcome = UpsertOutcome.UNCHANGED
        if existing.content_hash != job.content_hash or existing.compacted:
            updates.update({f: getattr(job, f) for f in CONTENT_FIELDS})
            updates["compacted"] = False
            outcome = UpsertOutcome.CHANGED
        self.jobs[job.id] = existing.model_copy(update=updates, deep=True)
        return outcome

    def upsert_jobs(self, jobs: list[Job]) -> list[UpsertOutcome]:
        return [self.upsert_job(job) for job in jobs]

    def get_jobs(self, job_ids: Iterable[str]) -> dict[str, Job]:
        return {i: self.jobs[i].model_copy(deep=True) for i in job_ids if i in self.jobs}

    def get_job(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        return job.model_copy(deep=True) if job else None

    def set_duplicate_of(self, job_id: str, original_id: str | None) -> None:
        if job_id in self.jobs:
            self.jobs[job_id] = self.jobs[job_id].model_copy(update={"duplicate_of": original_id})

    def save_derived(self, job_id: str, normalized: Normalized, fingerprint: str) -> None:
        if job_id in self.jobs:
            self.jobs[job_id] = self.jobs[job_id].model_copy(
                update={"normalized": normalized.model_copy(deep=True), "fingerprint": fingerprint}
            )

    def find_by_fingerprint(
        self, fingerprint: str, *, since: datetime, exclude_id: str | None = None
    ) -> list[Job]:
        found = [
            j
            for j in self.jobs.values()
            if j.fingerprint == fingerprint and j.first_seen_at >= since and j.id != exclude_id
        ]
        return sorted((j.model_copy(deep=True) for j in found), key=lambda j: j.first_seen_at)

    def list_jobs(
        self, *, active_only: bool = True, since: datetime | None = None, limit: int = 0
    ) -> list[Job]:
        jobs = [
            j
            for j in self.jobs.values()
            if (not active_only or j.is_active) and (since is None or j.first_seen_at >= since)
        ]
        jobs.sort(key=lambda j: j.first_seen_at, reverse=True)
        return [j.model_copy(deep=True) for j in (jobs[:limit] if limit else jobs)]

    def mark_missing_inactive(self, source: str, company: str, seen_ids: Iterable[str]) -> int:
        seen = set(seen_ids)
        count = 0
        for job_id, job in self.jobs.items():
            if (
                job.source == source
                and job.company == company
                and job.is_active
                and job_id not in seen
            ):
                self.jobs[job_id] = job.model_copy(update={"is_active": False})
                count += 1
        return count

    # --- housekeeping ---
    def stale_job_ids(
        self, *, inactive_before: datetime, include_compacted: bool = False
    ) -> list[str]:
        return [
            j.id
            for j in self.jobs.values()
            if not j.is_active
            and j.last_seen_at < inactive_before
            and (include_compacted or not j.compacted)
        ]

    def compact_jobs(self, job_ids: Iterable[str]) -> int:
        count = 0
        for job_id in job_ids:
            job = self.jobs.get(job_id)
            if job is not None and not job.compacted:
                self.jobs[job_id] = job.model_copy(
                    update={"description": "", "raw": {}, "compacted": True}
                )
                count += 1
        return count

    def delete_jobs(self, job_ids: Iterable[str]) -> int:
        count = 0
        for job_id in list(job_ids):
            if self.jobs.pop(job_id, None) is not None:
                count += 1
            self.matches.pop(job_id, None)
        return count

    def get_meta(self, key: str) -> dict[str, Any] | None:
        value = self.meta.get(key)
        return dict(value) if value is not None else None

    def set_meta(self, key: str, value: dict[str, Any]) -> None:
        self.meta[key] = dict(value)

    # --- matches ---
    def save_match(self, match: MatchResult) -> None:
        self.matches[match.job_id] = match.model_copy(deep=True)

    def save_matches(self, matches: list[MatchResult]) -> None:
        for match in matches:
            self.save_match(match)

    def get_matches(self, job_ids: Iterable[str]) -> dict[str, MatchResult]:
        return {i: self.matches[i].model_copy(deep=True) for i in job_ids if i in self.matches}

    def get_match(self, job_id: str) -> MatchResult | None:
        match = self.matches.get(job_id)
        return match.model_copy(deep=True) if match else None

    def list_matches(
        self, *, decision: str | None = None, min_score: int | None = None, limit: int = 0
    ) -> list[MatchResult]:
        found = [
            m
            for m in self.matches.values()
            if (decision is None or m.decision == decision)
            and (min_score is None or m.score >= min_score)
        ]
        found.sort(key=lambda m: m.score, reverse=True)
        return [m.model_copy(deep=True) for m in (found[:limit] if limit else found)]

    # --- alerts ---
    def claim_alert(
        self,
        job_id: str,
        trigger: AlertTrigger,
        score: int | None,
        *,
        force: bool = False,
        fingerprint: str = "",
    ) -> bool:
        existing = self.alerts.get(job_id)
        if existing is None:
            self.alerts[job_id] = AlertRecord(
                job_id=job_id,
                status=AlertStatus.SENDING,
                trigger=trigger,
                score=score,
                fingerprint=fingerprint,
            )
            return True
        reclaimable = {AlertStatus.FAILED} | ({AlertStatus.SENT} if force else set())
        if existing.status not in reclaimable:
            return False
        self.alerts[job_id] = existing.model_copy(
            update={
                "status": AlertStatus.SENDING,
                "trigger": trigger,
                "score": score,
                "fingerprint": fingerprint or existing.fingerprint,
                "claimed_at": utcnow(),
                "error": None,
                "attempts": existing.attempts + 1,
            }
        )
        return True

    def mark_alert_sent(self, job_id: str, message_id: str) -> None:
        self._update_alert(
            job_id, status=AlertStatus.SENT, message_id=message_id, sent_at=utcnow(), error=None
        )

    def mark_alert_failed(self, job_id: str, error: str) -> None:
        self._update_alert(job_id, status=AlertStatus.FAILED, error=error)

    def _update_alert(self, job_id: str, **fields: Any) -> None:
        if job_id in self.alerts:
            self.alerts[job_id] = self.alerts[job_id].model_copy(update=fields)

    def get_alert(self, job_id: str) -> AlertRecord | None:
        alert = self.alerts.get(job_id)
        return alert.model_copy() if alert else None

    def suppress_alert(self, job_id: str, fingerprint: str = "") -> bool:
        if job_id in self.alerts:
            return False
        self.alerts[job_id] = AlertRecord(
            job_id=job_id,
            status=AlertStatus.BASELINE,
            trigger=AlertTrigger.NEW,
            fingerprint=fingerprint,
        )
        return True

    def get_alerts(self, job_ids: Iterable[str]) -> dict[str, AlertRecord]:
        return {i: self.alerts[i].model_copy() for i in job_ids if i in self.alerts}

    def alerted_fingerprints(self, fingerprints: Iterable[str]) -> dict[str, str]:
        wanted = set(fingerprints)
        live = {AlertStatus.SENT, AlertStatus.SENDING, AlertStatus.BASELINE}
        return {
            a.fingerprint: a.job_id
            for a in self.alerts.values()
            if a.fingerprint in wanted and a.fingerprint and a.status in live
        }

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        since: datetime | None = None,
        limit: int = 0,
    ) -> list[AlertRecord]:
        found = [
            a
            for a in self.alerts.values()
            if (status is None or a.status == status) and (since is None or a.claimed_at >= since)
        ]
        found.sort(key=lambda a: a.claimed_at, reverse=True)
        return [a.model_copy() for a in (found[:limit] if limit else found)]

    # --- source health ---
    def get_source_state(self, key: str) -> SourceState | None:
        state = self.source_states.get(key)
        return state.model_copy(deep=True) if state else None

    def save_source_state(self, state: SourceState) -> None:
        self.source_states[state.key] = state.model_copy(deep=True)

    def list_source_states(self) -> list[SourceState]:
        return [s.model_copy(deep=True) for s in sorted(self.source_states.values(), key=_key)]

    # --- runs ---
    def record_run(self, run: RunSummary) -> None:
        self.runs.append(run.model_copy(deep=True))

    def latest_runs(self, limit: int = 10) -> list[RunSummary]:
        runs = sorted(self.runs, key=lambda r: r.started_at, reverse=True)
        return [r.model_copy(deep=True) for r in runs[:limit]]

    # --- preferences ---
    def get_preferences(self) -> PreferencesDoc | None:
        return self.preferences.model_copy(deep=True) if self.preferences else None

    def save_preferences(
        self, data: dict[str, Any], *, expected_version: int, note: str = ""
    ) -> PreferencesDoc:
        current = self.preferences.version if self.preferences else 0
        if current != expected_version:
            raise StaleVersionError(f"expected version {expected_version}, found {current}")
        doc = PreferencesDoc(version=current + 1, data=dict(data))
        self.preferences = doc
        self.preference_history.append(doc.model_copy(deep=True))
        return doc.model_copy(deep=True)


def _key(state: SourceState) -> str:
    return state.key
