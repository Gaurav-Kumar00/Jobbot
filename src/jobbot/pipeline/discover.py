"""Find which public ATS board a company uses (`jobbot discover <name>`)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget, RemoteScope
from jobbot.normalize import normalize_job
from jobbot.sources import get_source

DISCOVERABLE = ("greenhouse", "lever", "ashby", "workable", "keka", "smartrecruiters")
ENGINEERING = {"backend", "software_generic", "fullstack", "ai_engineering", "data_engineering"}
_SUFFIXES = re.compile(
    r"\b(technologies|technology|tech|labs|software|solutions|private|pvt|limited|ltd|inc|india"
    r"|hq|ai|app|io|co)\b"
)


@dataclass
class Hit:
    ats: str
    slug: str
    total: int
    india: int
    india_engineering: int


def slug_candidates(name: str) -> list[str]:
    lowered = name.strip().lower()
    alnum = re.sub(r"[^a-z0-9]", "", lowered)
    hyphen = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    core = re.sub(r"[^a-z0-9]", "", _SUFFIXES.sub(" ", lowered))
    camel = re.sub(r"[^A-Za-z0-9]", "", name.strip())
    return list(dict.fromkeys(c for c in (alnum, hyphen, core, camel) if c))


async def discover(name: str, http: HttpClient, extra_slugs: tuple[str, ...] = ()) -> list[Hit]:
    candidates = list(dict.fromkeys([*extra_slugs, *slug_candidates(name)]))
    probes = list(
        dict.fromkeys(
            # only SmartRecruiters ids are case-sensitive
            (ats, slug if ats == "smartrecruiters" else slug.lower())
            for ats in DISCOVERABLE
            for slug in candidates
        )
    )
    results = await asyncio.gather(*(_probe(http, name, ats, slug) for ats, slug in probes))
    hits = [hit for hit in results if hit is not None]
    return sorted(hits, key=lambda h: (-h.india_engineering, -h.india, -h.total))


async def _probe(http: HttpClient, name: str, ats: str, slug: str) -> Hit | None:
    target = CompanyTarget(
        key=slug.lower(), name=name, ats=ats, slug=slug, options={"details": False}
    )
    try:
        result = await get_source(ats).fetch(target, http)
    except FetchError:
        return None
    if not result.jobs:
        return None
    india = engineering = 0
    for job in map(normalize_job, result.jobs):
        loc = job.normalized.location
        in_india = "IN" in loc.countries or loc.remote_scope in (
            RemoteScope.INDIA,
            RemoteScope.GLOBAL,
        )
        india += in_india
        engineering += in_india and job.normalized.role_category in ENGINEERING
    return Hit(ats, slug, len(result.jobs), india, engineering)


def yaml_line(name: str, hit: Hit) -> str:
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
    return (
        f"  - {{key: {key}, name: {name}, ats: {hit.ats}, slug: {hit.slug}}}"
        f"  # india={hit.india}, india_eng={hit.india_engineering}"
    )
