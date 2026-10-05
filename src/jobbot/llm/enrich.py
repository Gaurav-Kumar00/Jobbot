"""Where the LLM is used, and how its answers change a job (conservatively).

Used only for jobs that matter: would-be alerts and near-misses, not yet alerted, not yet
checked. Answers are cached by posting content, so each posting costs at most one call.
They only fill gaps — explicit numbers in the posting always win — and a few ambiguous
role labels ("Software Engineer", "Full Stack") may be refined. Everything else stays
deterministic, and the scan behaves exactly the same when the LLM is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from jobbot.llm.chain import LLMChain
from jobbot.models import (
    AIInsight,
    ExperienceInfo,
    Job,
    MatchResult,
    RemoteScope,
    SalaryInfo,
)
from jobbot.storage.base import Repository

log = logging.getLogger(__name__)

TASK_VERSION = "facts-v2"  # bump when the prompt/schema changes to re-check postings
NEAR_MISS_MARGIN = 12  # near-misses this close to min_score are worth a second look
META_KEY = "llm_usage"

AMBIGUOUS_ROLES = {"software_generic", "fullstack", "unknown", "ai_engineering"}
ROLE_MAP = {
    "backend": "backend",
    "fullstack_backend_heavy": "backend",
    "fullstack": "fullstack",
    "frontend": "frontend",
    "mobile": "mobile",
    "qa": "qa",
    "devops": "devops",
    "data_engineering": "data_engineering",
    "data_science": "data_science",
    "ml": "ai_engineering",
    "support": "non_software",
    "other": "non_software",
}


@dataclass
class EnrichReport:
    calls: int = 0
    applied: int = 0
    failed: int = 0
    enriched_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def insight_key(job: Job) -> str:
    return f"{TASK_VERSION}:{job.content_hash}"


def apply_insight(job: Job, insight: AIInsight) -> Job:
    """Return a copy of `job` whose normalised fields have gaps filled from `insight`."""
    if job.normalized is None:
        return job
    n = job.normalized.model_copy(deep=True)
    n.ai = insight

    exp = n.experience
    if exp.min_years is None and insight.min_years is not None:
        n.experience = ExperienceInfo(
            min_years=insight.min_years,
            max_years=insight.max_years,
            fresher=exp.fresher or (insight.fresher_friendly and insight.min_years <= 1),
            evidence="read by AI",
        )
    elif exp.min_years is None and insight.fresher_friendly:
        n.experience = ExperienceInfo(min_years=0, max_years=1, fresher=True, evidence="read by AI")

    if n.role_category in AMBIGUOUS_ROLES:
        n.role_category = ROLE_MAP.get(insight.role_type, n.role_category)

    if insight.python_used and "python" not in n.skills:
        n.skills = [*n.skills, "python"]

    if n.location.remote_scope is RemoteScope.UNKNOWN:
        if insight.remote_india == "yes":
            n.location.remote_scope = RemoteScope.INDIA
        elif insight.remote_india == "no":
            n.location.remote_scope = RemoteScope.RESTRICTED

    if n.salary is None and insight.salary_kind != "none" and insight.salary_min_lpa:
        n.salary = SalaryInfo(
            currency="INR",
            min=insight.salary_min_lpa,
            max=insight.salary_max_lpa,
            kind=insight.salary_kind,
            evidence="read by AI",
        )
    return job.model_copy(update={"normalized": n})


def apply_cached(repo: Repository, jobs: list[Job]) -> list[Job]:
    """Re-apply cached insights (normalisation is recomputed on every fetch)."""
    if not jobs:
        return jobs
    cached = repo.get_insights({insight_key(j) for j in jobs})
    return [
        apply_insight(j, cached[insight_key(j)]) if insight_key(j) in cached else j for j in jobs
    ]


def candidates(
    jobs: list[Job], matches: dict[str, MatchResult], alerted: set[str], min_score: int
) -> list[Job]:
    """Unchecked jobs that would alert, or nearly would. Best first."""
    picked = []
    for job in jobs:
        match = matches.get(job.id)
        if match is None or job.id in alerted or (job.normalized and job.normalized.ai):
            continue
        near_miss = (match.reject_reason or "").startswith("score ") and (
            match.score >= min_score - NEAR_MISS_MARGIN
        )
        if match.decision == "match" or near_miss:
            picked.append((match.score, job))
    picked.sort(key=lambda pair: -pair[0])
    return [job for _, job in picked]


async def enrich(
    repo: Repository,
    chain: LLMChain,
    jobs: list[Job],
    *,
    run_budget: int,
    daily_budget: int,
    now: datetime,
) -> tuple[list[Job], EnrichReport]:
    """Ask the LLM about `jobs` (already filtered/ordered), within budget. Returns the
    updated jobs (only those that changed) and a report."""
    report = EnrichReport()
    today = now.date().isoformat()
    usage = repo.get_meta(META_KEY) or {}
    used_today = usage.get("calls", 0) if usage.get("date") == today else 0
    allowance = max(0, min(run_budget, daily_budget - used_today))
    updated: list[Job] = []
    async with httpx.AsyncClient() as http:
        for job in jobs[:allowance]:
            if not chain.available:
                break
            report.calls += 1
            insight = await chain.extract(http, job)
            if insight is None:
                report.failed += 1
                continue
            repo.save_insight(insight_key(job), insight)
            updated.append(apply_insight(job, insight))
            report.applied += 1
            report.enriched_ids.append(job.id)
    report.errors = chain.errors[-5:]
    if report.calls:
        repo.set_meta(META_KEY, {"date": today, "calls": used_today + report.calls})
    return updated, report
