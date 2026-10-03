"""Source contract and helpers shared by every job-source adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from jobbot.http import HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import truncate

MAX_DESCRIPTION_CHARS = 8000  # keeps Atlas M0 (512 MB) usage bounded
MAX_RAW_STRING_CHARS = 2000


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    skipped: int = 0  # malformed items ignored without failing the whole fetch
    errors: list[str] = field(default_factory=list)


class Source(Protocol):
    name: str

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        """Fetch all currently listed jobs for one company. Raises FetchError on failure."""
        ...


def build_job(
    *,
    source: str,
    target: CompanyTarget,
    native_id: str,
    title: str,
    url: str,
    location_raw: str,
    description: str,
    posted_at: datetime | None,
    raw: dict[str, Any],
    now: datetime,
    company_name: str | None = None,
) -> Job:
    title = " ".join(title.split())
    if not native_id or not title or not url:
        raise ValueError("job is missing id, title or url")
    description = truncate(description, MAX_DESCRIPTION_CHARS)
    location_raw = " ".join(location_raw.split())
    return Job(
        id=f"{source}:{target.key}:{native_id}",
        source=source,
        company=target.key,
        company_name=company_name or target.name,
        title=title,
        url=url,
        location_raw=location_raw,
        description=description,
        posted_at=posted_at,
        first_seen_at=now,
        last_seen_at=now,
        content_hash=content_hash(title, location_raw, description, url),
        raw=sanitize_raw(raw),
    )


def content_hash(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def sanitize_raw(value: Any) -> Any:
    """Make a source payload safe to store in MongoDB and small enough to keep."""
    if isinstance(value, dict):
        return {
            str(k).replace(".", "_"): sanitize_raw(v)
            for k, v in value.items()
            if not str(k).startswith("$")
        }
    if isinstance(value, list):
        return [sanitize_raw(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_RAW_STRING_CHARS:
        return value[:MAX_RAW_STRING_CHARS] + "…"
    return value


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 (with offset, or naive meaning UTC) into aware UTC; None if invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# Role categories worth an extra request for full details (others are rejected anyway).
DETAIL_WORTHY_CATEGORIES = frozenset(
    {"backend", "software_generic", "fullstack", "ai_engineering", "data_engineering", "unknown"}
)


def worth_details(title: str) -> bool:
    """Cheap title check before fetching a posting's detail page (be polite to APIs)."""
    from jobbot.normalize.role import classify_role, classify_seniority

    return classify_role(title) in DETAIL_WORTHY_CATEGORIES and classify_seniority(title) not in {
        "senior",
        "staff",
        "principal",
        "lead",
        "manager",
    }


def epoch_ms_to_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
