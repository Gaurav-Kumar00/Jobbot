"""Microsoft Careers search API (no auth). No descriptions in the listing.

GET https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=...
    &location=India&start=N&num=10&sort_by=timestamp
Options: `queries` (list), `max_results` (default 150 per query).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.sources.base import FetchResult, build_job, epoch_ms_to_datetime
from jobbot.timeutil import utcnow

API_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
SITE = "https://apply.careers.microsoft.com"
PAGE = 10  # fixed by the API
DEFAULT_QUERIES = ("software engineer",)


class MicrosoftSource:
    name = "microsoft"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        queries = target.options.get("queries") or DEFAULT_QUERIES
        limit = int(target.options.get("max_results", 150))
        items: dict[str, dict[str, Any]] = {}
        for query in queries:
            start = 0
            while start < limit:
                data = await http.get_json(
                    API_URL,
                    params={
                        "domain": "microsoft.com",
                        "query": query,
                        "location": "India",
                        "start": start,
                        "num": PAGE,
                        "sort_by": "timestamp",
                    },
                )
                body = data.get("data") if isinstance(data, dict) else None
                if not isinstance(body, dict) or not isinstance(body.get("positions"), list):
                    raise FetchError(API_URL, "unexpected response shape (no data.positions)")
                for item in body["positions"]:
                    if isinstance(item, dict):
                        items.setdefault(str(item.get("id")), item)
                start += PAGE
                if start >= int(body.get("count") or 0) or not body["positions"]:
                    break
        now = utcnow()
        result = FetchResult()
        for item in items.values():
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                result.errors.append(f"microsoft:{item.get('id')}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        workplace = item.get("workLocationOption")
        description = f"Department: {item['department']}" if item.get("department") else ""
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["name"]),
            url=SITE + str(item.get("positionUrl") or f"/careers/job/{item['id']}"),
            location_raw=" / ".join(
                item.get("locations") or item.get("standardizedLocations") or []
            ),
            description=description,
            posted_at=epoch_ms_to_datetime(int(item["postedTs"]) * 1000)
            if item.get("postedTs")
            else None,
            raw={k: item.get(k) for k in ("id", "displayJobId", "name", "locations", "department")},
            now=now,
        )
        hints = {"workplace": str(workplace)} if workplace else {}
        return job.model_copy(update={"hints": hints})
