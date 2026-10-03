"""Turns a raw `Job` into structured, comparable fields.

Normalisation is pure and runs on stored data, so improved extractors can be re-applied
to every stored job without scraping again (bump NORMALIZER_VERSION when logic changes).
"""

from __future__ import annotations

from jobbot.models import Job, Normalized
from jobbot.normalize.dedup import make_fingerprint
from jobbot.normalize.experience import extract_experience
from jobbot.normalize.location import normalize_location
from jobbot.normalize.role import classify_employment, classify_role, classify_seniority
from jobbot.normalize.salary import extract_salary
from jobbot.normalize.skills import extract_skills

NORMALIZER_VERSION = 3


def normalize(job: Job) -> Normalized:
    location = normalize_location(
        job.location_raw,
        workplace_hint=job.hints.get("workplace"),
        description=job.description,
    )
    skills, title_skills = extract_skills(job.title, job.description)
    return Normalized(
        version=NORMALIZER_VERSION,
        location=location,
        experience=extract_experience(job.title, job.description),
        salary=extract_salary(job.hints.get("salary", "") or job.description),
        role_category=classify_role(job.title),
        seniority=classify_seniority(job.title),
        employment_type=classify_employment(
            job.title, job.description, hint=job.hints.get("employment")
        ),
        skills=skills,
        title_skills=title_skills,
    )


def normalize_job(job: Job) -> Job:
    """Return a copy of `job` with `normalized` and `fingerprint` filled in."""
    normalized = normalize(job)
    fingerprint = make_fingerprint(job.company_name or job.company, job.title, normalized.location)
    return job.model_copy(update={"normalized": normalized, "fingerprint": fingerprint})


def needs_renormalize(job: Job) -> bool:
    return job.normalized is None or job.normalized.version != NORMALIZER_VERSION


__all__ = ["NORMALIZER_VERSION", "needs_renormalize", "normalize", "normalize_job"]
