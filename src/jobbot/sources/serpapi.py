"""Google Jobs via SerpApi (free key: 250 searches/month).

GET https://serpapi.com/search.json?engine=google_jobs&q=..&location=..&gl=in&hl=en&api_key=..
This is the legitimate window onto listings that also live on LinkedIn, Naukri,
Instahyre, Foundit, Cutshort, Shine, Internshala, ... (we never scrape those sites).

Budget: each run uses `per_run` searches (default 2), rotating through `searches` so every
query/city gets its turn. At the default 6-hour interval: 2 x 4 = 8 searches/day ≈ 240/month.
Options: `searches` (list of {q, location}), `per_run`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, Job
from jobbot.settings import Settings
from jobbot.sources.base import FetchResult, build_job
from jobbot.timeutil import utcnow

API_URL = "https://serpapi.com/search.json"
DEFAULT_SEARCHES = (
    {"q": "python backend developer", "location": "Bengaluru, Karnataka, India"},
    {"q": "python django developer fresher", "location": "India"},
    {"q": "python developer", "location": "Gurugram, Haryana, India"},
    {"q": "backend software engineer python", "location": "Hyderabad, Telangana, India"},
)
# Job boards (vs. the employer's own careers site) as named in Google's apply options.
BOARDS = {
    "linkedin", "naukri", "naukri.com", "indeed", "glassdoor", "instahyre", "foundit",
    "cutshort", "shine", "shine.com", "internshala", "jooble", "jobrapido.com", "bebee",
    "kit job", "jobleads", "simplyhired", "hirist", "iimjobs", "wellfound", "apna",
    "timesjobs", "freshersworld", "unstop", "talent.com", "adzuna", "monster",
}  # fmt: skip
_AGO = re.compile(r"(\d+)\s*\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)
_EMPLOYMENT = {"full-time": "full-time", "part-time": "part-time", "internship": "intern",
               "contractor": "contract", "contract": "contract"}  # fmt: skip


class SerpApiGoogleJobsSource:
    name = "serpapi"

    async def fetch(self, target: CompanyTarget, http: HttpClient) -> FetchResult:
        key = Settings().serpapi_key
        if key is None or not key.get_secret_value().strip():
            raise FetchError(API_URL, "not configured (set SERPAPI_KEY)")
        searches = list(target.options.get("searches") or DEFAULT_SEARCHES)
        per_run = max(1, int(target.options.get("per_run", 2)))
        now = utcnow()
        result = FetchResult()
        seen: set[str] = set()
        for search in _rotation(searches, per_run, target.interval_minutes or 360, now):
            data = await http.get_json(
                API_URL,
                params={
                    "engine": "google_jobs",
                    "q": search["q"],
                    "location": search.get("location", "India"),
                    "gl": "in",
                    "hl": "en",
                    "api_key": key.get_secret_value(),
                },
            )
            if not isinstance(data, dict):
                raise FetchError(API_URL, "unexpected response shape")
            if data.get("error") and "hasn't returned any results" not in str(data["error"]):
                raise FetchError(API_URL, f"SerpApi error: {data['error']}")
            for item in data.get("jobs_results") or []:
                try:
                    job = self._parse(item, target, now)
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    result.skipped += 1
                    result.errors.append(f"serpapi:{str(item)[:40]}: {exc}")
                    continue
                if job.id not in seen:
                    seen.add(job.id)
                    result.jobs.append(job)
        return result

    def _parse(self, item: dict[str, Any], target: CompanyTarget, now: datetime) -> Job:
        extensions = item.get("detected_extensions") or {}
        url = _best_link(item.get("apply_options") or []) or item.get("share_link")
        if not url:
            raise ValueError("no apply link")
        schedule = str(extensions.get("schedule_type") or "").lower().replace("–", "-")
        hints = {
            key: value
            for key, value in (
                ("employment", _EMPLOYMENT.get(schedule)),
                ("salary", f"Salary: {extensions['salary']}" if extensions.get("salary") else None),
            )
            if value
        }
        posted = _relative_time(str(extensions.get("posted_at") or ""), now) or next(
            (_relative_time(str(e), now) for e in item.get("extensions") or [] if "ago" in str(e)),
            None,
        )
        via = str(item.get("via") or "").removeprefix("via ").strip()
        description = str(item.get("description") or "")
        if via:
            description = f"Listed on: {via}\n\n{description}"
        job = build_job(
            source=self.name,
            target=target,
            native_id=hashlib.sha1(str(item["job_id"]).encode()).hexdigest()[:16],
            title=str(item["title"]),
            url=url,
            location_raw=str(item.get("location") or ""),
            description=description,
            posted_at=posted,
            raw={"title": item.get("title"), "company": item.get("company_name"), "via": via},
            now=now,
            company_name=str(item.get("company_name") or "Unknown"),
        )
        return job.model_copy(update={"hints": hints})


def _rotation(searches: list[dict], per_run: int, interval_minutes: int, now: datetime):
    """Pick `per_run` searches, advancing each interval so all searches take turns."""
    if not searches:
        return []
    slot = int(now.timestamp() // (max(interval_minutes, 1) * 60))
    start = (slot * per_run) % len(searches)
    return [searches[(start + i) % len(searches)] for i in range(min(per_run, len(searches)))]


def _best_link(options: list[dict[str, Any]]) -> str | None:
    """Prefer the employer's own careers page over job boards."""
    links = [(str(o.get("title", "")).strip().lower(), o.get("link")) for o in options]
    for title, link in links:
        if link and title not in BOARDS:
            return str(link)
    return str(links[0][1]) if links and links[0][1] else None


def _relative_time(text: str, now: datetime) -> datetime | None:
    m = _AGO.search(text)
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2).lower()
    days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[unit]
    return now - timedelta(days=amount * days)
