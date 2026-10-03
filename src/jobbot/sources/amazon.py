"""Amazon Jobs public search JSON (no auth) — India software roles.

GET https://www.amazon.jobs/en/search.json?base_query=...&normalized_country_code[]=IND
    &category[]=software-development&result_limit=100&offset=N&sort=recent
Options: `queries` (list), `max_results` (default 300).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job
from jobbot.timeutil import utcnow

API_URL = "https://www.amazon.jobs/en/search.json"
SITE = "https://www.amazon.jobs"
PAGE = 100
DEFAULT_QUERIES = ("software development engineer",)


class AmazonSource:
    name = "amazon"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        queries = target.options.get("queries") or DEFAULT_QUERIES
        limit = int(target.options.get("max_results", 300))
        items: dict[str, dict[str, Any]] = {}
        for query in queries:
            offset = 0
            while offset < limit:
                data = await http.get_json(
                    API_URL,
                    params={
                        "base_query": query,
                        "normalized_country_code[]": "IND",
                        "category[]": "software-development",
                        "result_limit": PAGE,
                        "offset": offset,
                        "sort": "recent",
                    },
                )
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                    raise FetchError(API_URL, "unexpected response shape (no 'jobs' list)")
                for item in data["jobs"]:
                    if isinstance(item, dict):
                        items.setdefault(_native_id(item), item)
                offset += PAGE
                if offset >= int(data.get("hits") or 0) or not data["jobs"]:
                    break
        now = utcnow()
        result = FetchResult()
        for item in items.values():
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                result.errors.append(f"amazon:{item.get('id_icims')}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        description = "\n\n".join(
            text
            for text in (
                html_to_text(item.get("description")),
                "Basic qualifications\n" + html_to_text(item.get("basic_qualifications")),
                "Preferred qualifications\n" + html_to_text(item.get("preferred_qualifications")),
            )
            if text.strip()
        )
        job = build_job(
            source=self.name,
            target=target,
            native_id=_native_id(item),
            title=str(item["title"]),
            url=SITE + str(item["job_path"]),
            location_raw=str(item.get("normalized_location") or item.get("location") or ""),
            description=description,
            posted_at=_posted(item.get("posted_date")),
            raw={k: item.get(k) for k in ("id_icims", "title", "normalized_location", "team")},
            now=now,
        )
        hints = (
            {"employment": str(item["job_schedule_type"])} if item.get("job_schedule_type") else {}
        )
        return job.model_copy(update={"hints": hints})


def _native_id(item: dict[str, Any]) -> str:
    value = item.get("id_icims")
    return str(value if value is not None else item["id"])


def _posted(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
