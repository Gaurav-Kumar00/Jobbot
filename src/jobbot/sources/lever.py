"""Lever public postings API (no auth).

Docs: https://github.com/lever/postings-api
GET https://api.lever.co/v0/postings/{company}?mode=json
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job, epoch_ms_to_datetime
from jobbot.timeutil import utcnow

API_URL = "https://api.lever.co/v0/postings/{slug}"
RAW_FIELDS = ("id", "text", "categories", "country", "workplaceType", "createdAt", "hostedUrl")
INTERVALS = {"per-year-salary": "per annum", "per-month-salary": "per month"}


class LeverSource:
    name = "lever"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        url = API_URL.format(slug=quote(target.slug, safe=""))
        data = await http.get_json(url, params={"mode": "json"})
        if not isinstance(data, list):
            raise FetchError(url, "unexpected response shape (expected a list)")
        now = utcnow()
        result = FetchResult()
        for item in data:
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("id", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"lever:{target.key}:{ident}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        categories = item.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location") or ""]
        location_raw = " / ".join(str(loc) for loc in locations if loc)
        if item.get("country") == "IN" and "india" not in location_raw.lower():
            location_raw = f"{location_raw}, India" if location_raw else "India"
        hints = {
            key: str(value)
            for key, value in (
                ("workplace", item.get("workplaceType")),
                ("employment", categories.get("commitment")),
                ("salary", _salary_hint(item.get("salaryRange"))),
            )
            if value and value != "unspecified"
        }
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["text"]),
            url=str(item["hostedUrl"]),
            location_raw=location_raw,
            description=_description(item),
            posted_at=epoch_ms_to_datetime(item.get("createdAt")),
            raw={k: item[k] for k in RAW_FIELDS if k in item},
            now=now,
        )
        return job.model_copy(update={"hints": hints})


def _description(item: dict[str, Any]) -> str:
    parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
    for section in item.get("lists") or []:
        heading = (section.get("text") or "").strip()
        body = html_to_text(section.get("content"))
        parts.append(f"{heading}\n{body}" if heading else body)
    parts.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _salary_hint(salary: Any) -> str | None:
    if not isinstance(salary, dict) or salary.get("min") is None:
        return None
    period = INTERVALS.get(str(salary.get("interval")), "per annum")
    currency = salary.get("currency") or "INR"
    high = f" - {int(salary['max']):,}" if salary.get("max") else ""
    return f"Salary: {currency} {int(salary['min']):,}{high} {period}"
