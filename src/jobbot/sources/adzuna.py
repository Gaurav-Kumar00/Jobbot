"""Adzuna India job search API (free key; ToS asks for "Jobs by Adzuna" attribution).

GET https://api.adzuna.com/v1/api/jobs/in/search/1?app_id=..&app_key=..&what=..
    &category=it-jobs&results_per_page=50&max_days_old=3&sort_by=date
Free tier: 25/min, 250/day, 2500/month -> default 4 queries per run, every 2 hours.
Options: `queries` (list), `max_days_old` (default 3).
Descriptions are short snippets and links go through Adzuna, so the employer's own
posting (if we also scan its ATS) wins deduplication.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.settings import Settings
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime
from jobbot.timeutil import utcnow

API_URL = "https://api.adzuna.com/v1/api/jobs/in/search/1"
DEFAULT_QUERIES = ("python developer", "backend developer", "django developer", "software engineer")
EMPLOYMENT = {"full_time": "full-time", "part_time": "part-time", "contract": "contract"}


class AdzunaSource:
    name = "adzuna"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        settings = Settings()
        if not settings.adzuna_app_id or not settings.adzuna_app_key:
            raise FetchError(API_URL, "not configured (set ADZUNA_APP_ID and ADZUNA_APP_KEY)")
        items: dict[str, dict[str, Any]] = {}
        for query in target.options.get("queries") or DEFAULT_QUERIES:
            data = await http.get_json(
                API_URL,
                params={
                    "app_id": settings.adzuna_app_id,
                    "app_key": settings.adzuna_app_key.get_secret_value(),
                    "what": query,
                    "category": "it-jobs",
                    "results_per_page": 50,
                    "max_days_old": target.options.get("max_days_old", 3),
                    "sort_by": "date",
                    "content-type": "application/json",
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise FetchError(API_URL, "unexpected response shape (no 'results' list)")
            for item in data["results"]:
                if isinstance(item, dict):
                    items.setdefault(str(item.get("id")), item)
        now = utcnow()
        result = FetchResult()
        for item in items.values():
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                result.errors.append(f"adzuna:{item.get('id')}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        location = item.get("location") or {}
        location_raw = str(location.get("display_name") or "")
        if "india" not in location_raw.lower():
            location_raw = f"{location_raw}, India" if location_raw else "India"
        hints = {
            key: value
            for key, value in (
                ("employment", EMPLOYMENT.get(str(item.get("contract_type") or ""))
                 or EMPLOYMENT.get(str(item.get("contract_time") or ""))),
                ("salary", _salary_hint(item)),
            )
            if value
        }  # fmt: skip
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=str(item["redirect_url"]),
            location_raw=location_raw,
            description=html_to_text(item.get("description")),
            posted_at=parse_iso_datetime(item.get("created")),
            raw={k: item.get(k) for k in ("id", "title", "company", "location", "created")},
            now=now,
            company_name=str((item.get("company") or {}).get("display_name") or "Unknown"),
        )
        return job.model_copy(update={"hints": hints})


def _salary_hint(item: dict[str, Any]) -> str | None:
    # Adzuna estimates salaries when none is posted; only real figures are used.
    if str(item.get("salary_is_predicted")) != "0" or not item.get("salary_min"):
        return None
    high = f" - {int(item['salary_max']):,}" if item.get("salary_max") else ""
    return f"Salary: INR {int(item['salary_min']):,}{high} per annum"
