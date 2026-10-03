"""Atlassian careers listings (no auth): one JSON list of every open role.

GET https://www.atlassian.com/endpoint/careers/listings
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job
from jobbot.timeutil import utcnow

API_URL = "https://www.atlassian.com/endpoint/careers/listings"


class AtlassianSource:
    name = "atlassian"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        data = await http.get_json(API_URL)
        if not isinstance(data, list):
            raise FetchError(API_URL, "unexpected response shape (expected a list)")
        now = utcnow()
        result = FetchResult()
        for item in data:
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("id", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"atlassian:{ident}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        portal = item.get("portalJobPost") or {}
        description = "\n\n".join(
            html_to_text(item.get(section))
            for section in ("overview", "responsibilities", "qualifications")
            if item.get(section)
        )
        if item.get("category"):
            description = f"Category: {item['category']}\n\n{description}"
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=str(portal.get("portalUrl") or item["applyUrl"]),
            location_raw=" / ".join(
                " ".join(str(loc).split()) for loc in item.get("locations") or []
            ),
            description=description,
            posted_at=_updated(portal.get("updatedDate")),
            raw={k: item.get(k) for k in ("id", "title", "type", "locations", "category")},
            now=now,
        )
        hints = {"employment": str(item["type"])} if item.get("type") else {}
        return job.model_copy(update={"hints": hints})


def _updated(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %I:%M %p").replace(tzinfo=UTC)
    except ValueError:
        return None
