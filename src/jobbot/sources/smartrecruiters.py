"""SmartRecruiters public postings API (no auth).

Docs: https://developers.smartrecruiters.com/docs/posting-api
GET https://api.smartrecruiters.com/v1/companies/{id}/postings?country=in&limit=100&offset=N
GET https://api.smartrecruiters.com/v1/companies/{id}/postings/{postingId}   (full job ad)

The list has no descriptions, so details are fetched only for engineering-looking titles.
Options (companies.yaml `options`): `country` (default "in"), `query` (free-text filter).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import quote

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime, worth_details
from jobbot.timeutil import utcnow

LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
PAGE_SIZE = 100
MAX_POSTINGS = 1000
COUNTRY_NAMES = {"in": "India", "us": "United States", "gb": "United Kingdom", "sg": "Singapore"}
SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


class SmartRecruitersSource:
    name = "smartrecruiters"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        company = quote(target.slug, safe="")
        url = LIST_URL.format(company=company)
        params: dict[str, Any] = {
            "limit": PAGE_SIZE,
            "country": target.options.get("country", "in"),
        }
        if query := target.options.get("query"):
            params["q"] = query
        postings: list[dict[str, Any]] = []
        offset = 0
        while offset < MAX_POSTINGS:
            data = await http.get_json(url, params={**params, "offset": offset})
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise FetchError(url, "unexpected response shape (no 'content' list)")
            postings.extend(data["content"])
            offset += PAGE_SIZE
            if offset >= int(data.get("totalFound") or 0) or not data["content"]:
                break

        self._with_details = target.options.get("details", True)
        details = await asyncio.gather(
            *(self._details(http, company, p) for p in postings), return_exceptions=True
        )
        now = utcnow()
        result = FetchResult()
        for posting, detail in zip(postings, details, strict=True):
            try:
                result.jobs.append(
                    self._parse(posting, detail if isinstance(detail, dict) else {}, target, now)
                )
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                result.errors.append(f"smartrecruiters:{target.key}:{posting.get('id')}: {exc}")
        return result

    async def _details(self, http: HttpClient, company: str, posting: dict) -> dict | None:
        if not self._with_details or not worth_details(str(posting.get("name", ""))):
            return None
        detail = await http.get_json(f"{LIST_URL.format(company=company)}/{posting['id']}")
        return detail if isinstance(detail, dict) else None

    def _parse(
        self, item: dict[str, Any], detail: dict[str, Any], target: CompanyTarget, now: datetime
    ) -> Job:
        loc = item.get("location") or {}
        country = str(loc.get("country") or "").lower()
        location_raw = ", ".join(
            part
            for part in (loc.get("city"), loc.get("region"), COUNTRY_NAMES.get(country, country))
            if part
        )
        workplace = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else None
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(
            html_to_text((sections.get(name) or {}).get("text")) for name in SECTIONS
        ).strip()
        level = (item.get("experienceLevel") or {}).get("label")
        if level:
            description = f"{description}\n\nExperience level: {level}".strip()
        url = detail.get("postingUrl") or (
            f"https://jobs.smartrecruiters.com/{quote(target.slug)}/{item['id']}"
        )
        hints = {
            key: str(value)
            for key, value in (
                ("workplace", workplace),
                ("employment", (item.get("typeOfEmployment") or {}).get("label")),
            )
            if value
        }
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["name"]),
            url=url,
            location_raw=location_raw,
            description=description,
            posted_at=parse_iso_datetime(item.get("releasedDate")),
            raw={k: item.get(k) for k in ("id", "name", "location", "releasedDate", "ref")},
            now=now,
        )
        return job.model_copy(update={"hints": hints})
