"""Unstop public opportunity search (no auth) — India jobs posted directly by employers.

GET https://unstop.com/api/public/opportunity/search-result?opportunity=jobs&searchTerm=...
    &per_page=50&page=N
unstop.com/robots.txt explicitly allows /api/public/*. Requests are kept small and polite.
Options: `queries` (list), `max_pages` per query (default 2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.normalize.text import html_to_text
from jobbot.sources.base import FetchResult, build_job, parse_iso_datetime
from jobbot.timeutil import utcnow

API_URL = "https://unstop.com/api/public/opportunity/search-result"
PAGE_SIZE = 50
DEFAULT_QUERIES = ("python", "backend developer", "django", "software engineer")
WORKPLACE = {"wfh": "remote", "in_office": "onsite", "hybrid": "hybrid"}
EMPLOYMENT = {"full_time": "full-time", "part_time": "part-time"}


class UnstopSource:
    name = "unstop"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        queries = target.options.get("queries") or DEFAULT_QUERIES
        max_pages = int(target.options.get("max_pages", 2))
        items: dict[str, dict[str, Any]] = {}
        for query in queries:
            for page in range(1, max_pages + 1):
                data = await http.get_json(
                    API_URL,
                    params={
                        "opportunity": "jobs",
                        "searchTerm": query,
                        "per_page": PAGE_SIZE,
                        "page": page,
                    },
                )
                body = data.get("data") if isinstance(data, dict) else None
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise FetchError(API_URL, "unexpected response shape (no data.data list)")
                for item in body["data"]:
                    if isinstance(item, dict) and item.get("status", "LIVE") == "LIVE":
                        items.setdefault(str(item.get("id")), item)
                if page >= int(body.get("last_page") or 1):
                    break
        now = utcnow()
        result = FetchResult()
        for item in items.values():
            try:
                result.jobs.append(self._parse(item, target, now))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                result.skipped += 1
                result.errors.append(f"unstop:{item.get('id')}: {exc}")
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        detail = item.get("jobDetail") or {}
        places = [
            ", ".join(p for p in (loc.get("city"), loc.get("country")) if p)
            for loc in item.get("locations") or []
            if isinstance(loc, dict)
        ] or [str(city) for city in detail.get("locations") or []]
        lines = []
        low, high = detail.get("min_experience"), detail.get("max_experience")
        if low is not None:
            lines.append(f"Experience: {low}-{high} years" if high is not None else f"{low}+ years")
        eligibility = [
            f.get("name") for f in item.get("filters") or [] if f.get("type") == "eligible"
        ]
        if eligibility:
            lines.append("Eligible: " + ", ".join(str(e) for e in eligibility if e))
        skills = [s.get("skill_name") or s.get("skill") for s in item.get("required_skills") or []]
        if skills:
            lines.append("Skills: " + ", ".join(str(s) for s in skills if s))
        description = "\n".join(lines + [html_to_text(item.get("details"))])
        hints = {
            key: value
            for key, value in (
                ("workplace", WORKPLACE.get(str(detail.get("type")))),
                ("employment", EMPLOYMENT.get(str(detail.get("timing")))),
                ("salary", _salary_hint(detail)),
            )
            if value
        }
        organisation = item.get("organisation") or {}
        job = build_job(
            source=self.name,
            target=target,
            native_id=str(item["id"]),
            title=str(item["title"]),
            url=str(item.get("seo_url") or f"https://unstop.com/{item['public_url']}"),
            location_raw=" / ".join(places),
            description=description,
            posted_at=_approved(item.get("approved_date"))
            or parse_iso_datetime(item.get("updated_at")),
            raw={k: item.get(k) for k in ("id", "title", "organisation", "jobDetail")},
            now=now,
            company_name=str(organisation.get("name") or target.name),
        )
        return job.model_copy(update={"hints": hints})


def _salary_hint(detail: dict[str, Any]) -> str | None:
    if not detail.get("show_salary") or detail.get("not_disclosed") or not detail.get("min_salary"):
        return None
    period = "per month" if detail.get("pay_in") == "monthly" else "per annum"
    high = f" - {int(detail['max_salary']):,}" if detail.get("max_salary") else ""
    return f"Salary: INR {int(detail['min_salary']):,}{high} {period}"


def _approved(value: Any) -> datetime | None:
    # e.g. "2026-09-23 16:39:30 GMT+0530"
    try:
        return datetime.strptime(str(value).replace(" GMT", ""), "%Y-%m-%d %H:%M:%S%z")
    except ValueError:
        return None
