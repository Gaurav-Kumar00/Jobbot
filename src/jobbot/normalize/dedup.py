"""Cross-source duplicate detection.

The same opening often appears on the company's ATS and on aggregators. Jobs share a
fingerprint when company, title and primary city normalise to the same values; the
copy from the most authoritative source (the official ATS) is kept as canonical.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from jobbot.models import Job, LocationInfo
from jobbot.normalize.role import clean_title

_COMPANY_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|co|company|gmbh"
    r"|technologies|technology|tech|labs|software|solutions|services|systems|india|global"
    r"|the)\b"
)
_TITLE_SYNONYMS = [
    (re.compile(r"\bsr\b"), "senior"),
    (re.compile(r"\bjr\b"), "junior"),
    (re.compile(r"\bsde\b|\bsoftware development engineer\b"), "software engineer"),
    (re.compile(r"\bswe\b"), "software engineer"),
    (re.compile(r"\bback[\s-]?end\b"), "backend"),
    (re.compile(r"\bfront[\s-]?end\b"), "frontend"),
    (re.compile(r"\bfull[\s-]?stack\b"), "fullstack"),
    (re.compile(r"\bdeveloper\b"), "engineer"),
    (re.compile(r"\b(?:i|1)\b"), "1"),
    (re.compile(r"\b(?:ii|2)\b"), "2"),
    (re.compile(r"\b(?:iii|3)\b"), "3"),
]
_TITLE_NOISE_WORDS = re.compile(
    r"\b(remote|hybrid|onsite|on-site|wfh|india|bengaluru|bangalore|hyderabad|gurugram|gurgaon"
    r"|noida|delhi|ncr|pune|mumbai|chennai|full[\s-]?time|permanent|urgent|hiring|immediate"
    r"|joiner|joiners)\b"
)

# Lower number = more authoritative. Official ATS / company sites first, aggregators last.
SOURCE_PRECEDENCE: dict[str, int] = {
    "greenhouse": 0,
    "lever": 0,
    "ashby": 0,
    "smartrecruiters": 0,
    "workday": 0,
    "workable": 0,
    "recruitee": 0,
    "amazon": 0,
    "microsoft": 0,
    "atlassian": 0,
    "adzuna": 2,
    "serpapi": 2,
    "jsearch": 2,
    "himalayas": 2,
    "remotive": 2,
    "remoteok": 2,
    "hn": 3,
    "email": 3,
}
DEFAULT_PRECEDENCE = 1
DEDUP_WINDOW_DAYS = 45


def normalize_company(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    cleaned = _COMPANY_SUFFIXES.sub(" ", cleaned)
    return "".join(cleaned.split()) or name.lower().strip()


def normalize_title(title: str) -> str:
    cleaned = clean_title(title)
    cleaned = re.sub(r"[(),/|:.\-–]+", " ", cleaned)
    cleaned = _TITLE_NOISE_WORDS.sub(" ", cleaned)
    for pattern, replacement in _TITLE_SYNONYMS:
        cleaned = pattern.sub(replacement, cleaned)
    return " ".join(cleaned.split())


def make_fingerprint(company_name: str, title: str, location: LocationInfo) -> str:
    if location.cities:
        place = sorted(location.cities)[0]
    elif location.work_mode.value == "remote":
        place = "remote"
    else:
        place = "any"
    return f"{normalize_company(company_name)}|{normalize_title(title)}|{place}"


def precedence(source: str) -> int:
    return SOURCE_PRECEDENCE.get(source, DEFAULT_PRECEDENCE)


def choose_canonical(jobs: Iterable[Job]) -> Job:
    """Most authoritative source wins; ties go to the earliest discovered copy."""
    return min(jobs, key=lambda j: (precedence(j.source), j.first_seen_at, j.id))
