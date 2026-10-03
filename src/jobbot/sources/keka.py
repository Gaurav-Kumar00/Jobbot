"""Keka careers API (no auth) — popular with Indian startups.

GET https://{tenant}.keka.com/careers/api/jobs/{portal}/active
robots.txt of keka tenants allows /careers/ only, which this endpoint lives under.
Option (companies.yaml `options`): `portal` (default "default").
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

API_URL = "https://{tenant}.keka.com/careers/api/jobs/{portal}/active"
JOB_URL = "https://{tenant}.keka.com/careers/jobdetails/{id}"


class KekaSource:
    name = "keka"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        tenant = quote(target.slug, safe="")
        portal = quote(str(target.options.get("portal", "default")), safe="")
        url = API_URL.format(tenant=tenant, portal=portal)
        data = await http.get_json(url)
        if not isinstance(data, list):
            raise FetchError(url, "unexpected response shape (expected a list)")
        now = utcnow()
        result = FetchResult()
        for item in data:
            try:
                result.jobs.append(self._parse(item, target, tenant, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                ident = item.get("id", "?") if isinstance(item, dict) else "?"
                result.errors.append(f"keka:{target.key}:{ident}: {exc}")
        return result

    def _parse(
        self, item: dict[str, Any], target: CompanyTarget, tenant: str, now: datetime
    ) -> Job:
        location_raw = " / ".join(
            ", ".join(p for p in (loc.get("city"), loc.get("countryName")) if p)
            or str(loc.get("name") or "")
            for loc in item.get("jobLocations") or []
            if isinstance(loc, dict)
        )
        description = html_to_text(item.get("description"))
        if experience := item.get("experience"):
            # e.g. "0-1", "5+" -> phrased so the experience extractor reads it.
            description = f"Experience: {experience} years\n\n{description}"
        if skills := item.get("skillNames"):
            description += "\n\nSkills: " + ", ".join(str(s) for s in skills)
        return build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=JOB_URL.format(tenant=tenant, id=item["id"]),
            location_raw=location_raw,
            description=description,
            posted_at=parse_iso_datetime(item.get("publishedOn")),
            raw={k: item.get(k) for k in ("id", "title", "jobLocations", "experience", "jobType")},
            now=now,
        )
