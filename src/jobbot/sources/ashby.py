"""Ashby public job board API (no auth).

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime
from jobbot.timeutil import utcnow

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
RAW_FIELDS = (
    "id", "title", "department", "team", "employmentType", "location", "secondaryLocations",
    "isRemote", "workplaceType", "publishedAt", "jobUrl",
)  # fmt: skip
EMPLOYMENT = {
    "fulltime": "full-time",
    "parttime": "part-time",
    "intern": "intern",
    "contract": "contract",
    "temporary": "temporary",
}


class AshbySource:
    name = "ashby"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        url = API_URL.format(slug=quote(target.slug, safe=""))
        data = await http.get_json(url, params={"includeCompensation": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FetchError(url, "unexpected response shape (no 'jobs' list)")
        now = utcnow()
        result = FetchResult()
        for item in data["jobs"]:
            if isinstance(item, dict) and item.get("isListed") is False:
                continue
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("id", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"ashby:{target.key}:{ident}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        places = [item.get("location") or ""]
        for extra in item.get("secondaryLocations") or []:
            places.append(extra.get("location") if isinstance(extra, dict) else str(extra))
        country = ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        location_raw = " / ".join(p for p in places if p)
        if country and country.lower() not in location_raw.lower():
            location_raw = f"{location_raw}, {country}" if location_raw else country
        workplace = item.get("workplaceType") or ("Remote" if item.get("isRemote") else None)
        hints = {
            key: str(value)
            for key, value in (
                ("workplace", workplace),
                ("employment", EMPLOYMENT.get(str(item.get("employmentType", "")).lower())),
                ("salary", _salary_hint(item.get("compensation"))),
            )
            if value
        }
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=str(item.get("jobUrl") or item["applyUrl"]),
            location_raw=location_raw,
            description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")),
            posted_at=parse_iso_datetime(item.get("publishedAt")),
            raw={k: item[k] for k in RAW_FIELDS if k in item},
            now=now,
        )
        return job.model_copy(update={"hints": hints})


def _salary_hint(compensation: Any) -> str | None:
    if not isinstance(compensation, dict):
        return None
    summary = compensation.get("scrapeableCompensationSalarySummary") or compensation.get(
        "compensationTierSummary"
    )
    return f"Salary: {summary}" if summary else None
