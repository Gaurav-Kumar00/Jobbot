"""Workable public widget API (no auth).

GET https://apply.workable.com/api/v1/widget/accounts/{account}
The widget lists titles, locations and job type but no description, so these jobs are
scored on title, location and experience level only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime
from jobbot.timeutil import utcnow

API_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


class WorkableSource:
    name = "workable"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        url = API_URL.format(slug=quote(target.slug, safe=""))
        data = await http.get_json(url)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FetchError(url, "unexpected response shape (no 'jobs' list)")
        now = utcnow()
        result = FetchResult()
        for item in data["jobs"]:
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("shortcode", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"workable:{target.key}:{ident}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        places = item.get("locations") or [
            {"city": item.get("city"), "country": item.get("country")}
        ]
        location_raw = " / ".join(
            ", ".join(p for p in (place.get("city"), place.get("country")) if p)
            for place in places
            if isinstance(place, dict)
        )
        details = [
            f"{label}: {item[key]}"
            for key, label in (
                ("experience", "Experience level"),
                ("function", "Function"),
                ("department", "Department"),
            )
            if item.get(key)
        ]
        hints = {
            key: str(value)
            for key, value in (
                ("workplace", "remote" if item.get("telecommuting") else None),
                ("employment", item.get("employment_type")),
            )
            if value
        }
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["shortcode"]),
            title=str(item["title"]),
            url=str(item.get("url") or item["shortlink"]),
            location_raw=location_raw,
            description="\n".join(details),
            posted_at=parse_iso_datetime(item.get("published_on") or item.get("created_at")),
            raw={k: item.get(k) for k in ("shortcode", "title", "locations", "published_on")},
            now=now,
        )
        return job.model_copy(update={"hints": hints})
