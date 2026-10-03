"""Greenhouse public job board API (no auth).

Docs: https://developers.greenhouse.io/job-board.html
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime
from jobbot.timeutil import utcnow

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
RAW_FIELDS = (
    "id",
    "internal_job_id",
    "requisition_id",
    "title",
    "absolute_url",
    "location",
    "metadata",
    "departments",
    "offices",
    "first_published",
    "updated_at",
    "company_name",
)


class GreenhouseSource:
    name = "greenhouse"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        url = API_URL.format(slug=quote(target.slug, safe=""))
        data = await http.get_json(url, params={"content": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FetchError(url, "unexpected response shape (no 'jobs' list)")

        now = utcnow()
        result = FetchResult()
        for item in data["jobs"]:
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("id", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"greenhouse:{target.key}:{ident}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: Any):
        location = item.get("location") or {}
        return build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=str(item["absolute_url"]),
            location_raw=str(location.get("name") or ""),
            description=html_to_text(item.get("content")),
            # first_published = when the posting went live; updated_at changes on edits.
            posted_at=parse_iso_datetime(item.get("first_published"))
            or parse_iso_datetime(item.get("updated_at")),
            raw={k: item[k] for k in RAW_FIELDS if k in item},
            now=now,
        )
