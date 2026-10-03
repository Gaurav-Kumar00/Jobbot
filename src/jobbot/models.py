"""Domain models shared by the scanner, matcher, storage and bot."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from jobbot.timeutil import utcnow


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class RemoteScope(StrEnum):
    """Who may apply to a remote role."""

    NOT_REMOTE = "not_remote"
    INDIA = "india"  # explicitly open to India
    GLOBAL = "global"  # anywhere / worldwide
    APAC = "apac"  # region that includes India
    RESTRICTED = "restricted"  # limited to other countries/regions
    UNKNOWN = "unknown"  # remote, eligibility not stated


class LocationInfo(BaseModel):
    cities: list[str] = Field(default_factory=list)  # canonical, e.g. ["bengaluru"]
    regions: list[str] = Field(default_factory=list)  # e.g. ["ncr"]
    countries: list[str] = Field(default_factory=list)  # ISO-ish codes, e.g. ["IN"]
    work_mode: WorkMode = WorkMode.UNKNOWN
    remote_scope: RemoteScope = RemoteScope.NOT_REMOTE


class ExperienceInfo(BaseModel):
    min_years: float | None = None
    max_years: float | None = None
    fresher: bool = False  # explicit fresher / new-grad / entry-level signal
    evidence: str | None = None  # the text it was read from, for explanations


class SalaryInfo(BaseModel):
    currency: str  # "INR", "USD", ...
    min: float | None = None  # INR in LPA; other currencies in annual units
    max: float | None = None
    kind: Literal["base", "ctc", "unspecified"] = "unspecified"
    period: Literal["annual", "monthly"] = "annual"  # as stated; min/max are annualised
    evidence: str = ""


class Normalized(BaseModel):
    """Derived fields. Bump NORMALIZER_VERSION when extraction logic changes."""

    version: int
    location: LocationInfo = Field(default_factory=LocationInfo)
    experience: ExperienceInfo = Field(default_factory=ExperienceInfo)
    salary: SalaryInfo | None = None
    role_category: str = "unknown"
    seniority: str = "unknown"
    employment_type: str = "full_time"
    skills: list[str] = Field(default_factory=list)
    title_skills: list[str] = Field(default_factory=list)


class Job(BaseModel):
    """A single posting from one source. Identity: `{source}:{company}:{native_id}`."""

    id: str
    source: str  # e.g. "greenhouse"
    company: str  # stable short key from config/companies.yaml, e.g. "razorpay"
    company_name: str = ""  # display name, e.g. "Razorpay"
    title: str
    url: str
    location_raw: str = ""
    description: str = ""
    posted_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    is_active: bool = True
    compacted: bool = False  # description/raw dropped by storage housekeeping
    content_hash: str = ""
    fingerprint: str = ""  # cross-source dedup key
    duplicate_of: str | None = None
    # Structured hints some sources provide (e.g. Lever workplaceType/commitment).
    hints: dict[str, str] = Field(default_factory=dict)
    normalized: Normalized | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("normalized", mode="before")
    @classmethod
    def _drop_legacy_normalized(cls, value: Any) -> Any:
        # Docs written before normalisation was typed hold `{}` or an older shape.
        # Treat them as "not normalised yet" so they get re-normalised, instead of
        # failing to load.
        if isinstance(value, dict) and "version" not in value:
            return None
        return value

    @property
    def source_key(self) -> str:
        return f"{self.source}:{self.company}"


class CompanyTarget(BaseModel):
    """One company on one ATS, from config/companies.yaml."""

    key: str  # stable short id used in job ids, e.g. "razorpay"
    name: str  # display name
    ats: str  # source name, e.g. "greenhouse"
    slug: str  # the ATS board token / company identifier
    enabled: bool = True
    interval_minutes: int | None = None  # scan cadence; None -> 60 (Telegram can override)
    options: dict[str, Any] = Field(default_factory=dict)  # source-specific extras


class UpsertOutcome(StrEnum):
    NEW = "new"
    CHANGED = "changed"  # content_hash differs -> needs re-matching
    UNCHANGED = "unchanged"


class MatchResult(BaseModel):
    job_id: str
    score: int
    decision: Literal["match", "reject"]
    reasons: list[str] = Field(default_factory=list)
    reject_reason: str | None = None
    breakdown: dict[str, int] = Field(default_factory=dict)  # component -> points
    details: dict[str, str] = Field(default_factory=dict)  # component -> human explanation
    prefs_version: int
    computed_at: datetime = Field(default_factory=utcnow)


class AlertStatus(StrEnum):
    SENDING = "sending"  # claimed; outcome unknown if the process died here
    SENT = "sent"
    FAILED = "failed"  # definitive send failure -> may be retried
    BASELINE = "baseline"  # already open when its company was first scanned; never sent


class AlertTrigger(StrEnum):
    NEW = "new"
    REMATCH = "rematch"
    REALERT = "realert"


class AlertRecord(BaseModel):
    job_id: str
    status: AlertStatus
    trigger: AlertTrigger
    score: int | None = None
    fingerprint: str = ""  # one alert per opening, even if posted under several ids
    claimed_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    message_id: str | None = None
    error: str | None = None
    attempts: int = 1


class SourceState(BaseModel):
    key: str  # "{source}:{company}" or a bare source name for aggregators
    bootstrapped: bool = False
    interval_minutes: int | None = None  # None -> source default
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    failure_notified: bool = False
    last_counts: dict[str, int] = Field(default_factory=dict)


class RunSummary(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    trigger: str = "schedule"
    stats: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class PreferencesDoc(BaseModel):
    """Versioned preference blob. Its schema is defined by the matcher (Phase 5)."""

    version: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)
