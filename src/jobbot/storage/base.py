"""Storage contract. Everything above this layer depends only on `Repository`."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from jobbot.models import (
    AIInsight,
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


class StaleVersionError(Exception):
    """Preferences were changed by someone else since they were read."""


class Repository(Protocol):
    # --- lifecycle ---
    def ensure_indexes(self) -> None: ...
    def ping(self) -> None: ...
    def storage_bytes(self) -> int: ...

    # --- jobs ---
    def upsert_job(self, job: Job) -> UpsertOutcome:
        """Insert, or refresh `last_seen_at`/`is_active`. Keeps `first_seen_at`.

        If `content_hash` changed, content fields are replaced and CHANGED is returned.
        """
        ...

    def upsert_jobs(self, jobs: list[Job]) -> list[UpsertOutcome]:
        """Batch `upsert_job` (one round trip per batch). Unchanged jobs also get their
        derived fields (normalized, fingerprint) refreshed."""
        ...

    def get_job(self, job_id: str) -> Job | None: ...
    def get_jobs(self, job_ids: Iterable[str]) -> dict[str, Job]: ...
    def set_duplicate_of(self, job_id: str, original_id: str | None) -> None: ...
    def save_derived(self, job_id: str, normalized: Normalized, fingerprint: str) -> None:
        """Store re-computed normalisation without touching content or timestamps."""
        ...

    def find_by_fingerprint(
        self, fingerprint: str, *, since: datetime, exclude_id: str | None = None
    ) -> list[Job]: ...
    def list_jobs(
        self, *, active_only: bool = True, since: datetime | None = None, limit: int = 0
    ) -> list[Job]: ...
    def mark_missing_inactive(self, source: str, company: str, seen_ids: Iterable[str]) -> int:
        """Mark active jobs of this source/company not in `seen_ids` inactive. Returns count."""
        ...

    # --- housekeeping (storage safety net) ---
    def stale_job_ids(
        self, *, inactive_before: datetime, include_compacted: bool = False
    ) -> list[str]:
        """Inactive jobs last seen before the cutoff."""
        ...

    def compact_jobs(self, job_ids: Iterable[str]) -> int:
        """Drop description/raw (keep identity + fingerprint for dedup). Returns count."""
        ...

    def delete_jobs(self, job_ids: Iterable[str]) -> int:
        """Delete jobs and their match records. Returns jobs deleted."""
        ...

    def get_meta(self, key: str) -> dict[str, Any] | None: ...
    def set_meta(self, key: str, value: dict[str, Any]) -> None: ...

    # --- LLM insight cache (keyed by task + content hash) ---
    def get_insights(self, keys: Iterable[str]) -> dict[str, AIInsight]: ...
    def save_insight(self, key: str, insight: AIInsight) -> None: ...

    # --- matches ---
    def save_match(self, match: MatchResult) -> None: ...
    def save_matches(self, matches: list[MatchResult]) -> None: ...
    def get_matches(self, job_ids: Iterable[str]) -> dict[str, MatchResult]: ...
    def get_match(self, job_id: str) -> MatchResult | None: ...
    def list_matches(
        self, *, decision: str | None = None, min_score: int | None = None, limit: int = 0
    ) -> list[MatchResult]: ...

    # --- alerts (idempotency lives here) ---
    def claim_alert(
        self,
        job_id: str,
        trigger: AlertTrigger,
        score: int | None,
        *,
        force: bool = False,
        fingerprint: str = "",
    ) -> bool:
        """Atomically reserve the right to send an alert for `job_id`.

        Succeeds if no alert exists, or the previous one FAILED. With `force=True`
        (explicit /realert) it also succeeds over SENT. Never succeeds over SENDING,
        whose outcome is unknown.
        """
        ...

    def mark_alert_sent(self, job_id: str, message_id: str) -> None: ...
    def mark_alert_failed(self, job_id: str, error: str) -> None: ...
    def suppress_alert(self, job_id: str, fingerprint: str = "") -> bool:
        """Record a BASELINE alert (never sent) unless an alert record already exists."""
        ...

    def get_alert(self, job_id: str) -> AlertRecord | None: ...
    def get_alerts(self, job_ids: Iterable[str]) -> dict[str, AlertRecord]: ...
    def alerted_fingerprints(self, fingerprints: Iterable[str]) -> dict[str, str]:
        """fingerprint -> job id, for openings already handled (sent, in flight or baseline)."""
        ...

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        since: datetime | None = None,
        limit: int = 0,
    ) -> list[AlertRecord]: ...

    # --- source health ---
    def get_source_state(self, key: str) -> SourceState | None: ...
    def save_source_state(self, state: SourceState) -> None: ...
    def list_source_states(self) -> list[SourceState]: ...

    # --- runs ---
    def record_run(self, run: RunSummary) -> None: ...
    def latest_runs(self, limit: int = 10) -> list[RunSummary]: ...

    # --- preferences ---
    def get_preferences(self) -> PreferencesDoc | None: ...
    def save_preferences(
        self, data: dict[str, Any], *, expected_version: int, note: str = ""
    ) -> PreferencesDoc:
        """Write a new version. Raises StaleVersionError if `expected_version` is outdated."""
        ...
