from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobbot.llm.enrich import apply_insight, candidates, enrich, insight_key
from jobbot.models import AIInsight, Job, MatchResult, RemoteScope
from jobbot.normalize import normalize_job
from jobbot.storage import MemoryRepository

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
LOW = "is below your minimum of 55"


def job(title="Software Engineer", desc="Build services.", loc="Bengaluru", native="1") -> Job:
    return normalize_job(
        Job(id=f"lever:acme:{native}", source="lever", company="acme", company_name="Acme",
            title=title, url="u", location_raw=loc, description=desc)
    )  # fmt: skip


def insight(**fields) -> AIInsight:
    return AIInsight(model="test", **fields)


# --- gap filling ----------------------------------------------------------------------------


def test_fills_unknown_experience_but_never_overrides_stated_numbers():
    unknown = apply_insight(job(), insight(min_years=0, max_years=1, fresher_friendly=True))
    assert (unknown.normalized.experience.min_years, unknown.normalized.experience.fresher) == (
        0,
        True,
    )
    stated = apply_insight(job(desc="3+ years of experience"), insight(min_years=0, max_years=1))
    assert stated.normalized.experience.min_years == 3


def test_fresher_flag_alone_fills_an_unknown_experience():
    n = apply_insight(job(), insight(fresher_friendly=True)).normalized
    assert (n.experience.min_years, n.experience.max_years, n.experience.fresher) == (0, 1, True)


@pytest.mark.parametrize(
    ("title", "role_type", "expected"),
    [
        ("Software Engineer", "frontend", "frontend"),  # generic title refined
        ("Software Engineer", "fullstack_backend_heavy", "backend"),
        ("Full Stack Developer", "backend", "backend"),
        ("Member of Technical Staff", "support", "non_software"),
        ("Backend Engineer", "frontend", "backend"),  # explicit title wins
        ("QA Engineer", "backend", "qa"),  # never turns an excluded role into a match
    ],
)
def test_role_refined_only_when_ambiguous(title, role_type, expected):
    assert (
        apply_insight(job(title=title), insight(role_type=role_type)).normalized.role_category
        == expected
    )


def test_python_remote_and_salary_gaps():
    n = apply_insight(
        job(loc="Remote"),
        insight(
            python_used=True,
            remote_india="yes",
            salary_kind="base",
            salary_min_lpa=14,
            salary_max_lpa=18,
        ),
    ).normalized
    assert "python" in n.skills
    assert n.location.remote_scope is RemoteScope.INDIA
    assert (n.salary.min, n.salary.max, n.salary.kind) == (14, 18, "base")
    assert n.ai.model == "test"


def test_stated_salary_and_known_remote_scope_are_kept():
    n = apply_insight(
        job(loc="Remote (US only)", desc="CTC 9 LPA"),
        insight(remote_india="yes", salary_kind="base", salary_min_lpa=20),
    ).normalized
    assert n.location.remote_scope is RemoteScope.RESTRICTED
    assert n.salary.min == 9


# --- selection & budget -------------------------------------------------------------------


def test_candidates_are_matches_and_near_misses_not_yet_alerted_or_checked():
    jobs = [job(native=str(i)) for i in range(6)]
    checked = apply_insight(jobs[5], insight())
    jobs[5] = checked
    m = {
        jobs[0].id: MatchResult(job_id=jobs[0].id, score=70, decision="match", prefs_version=1),
        jobs[1].id: MatchResult(job_id=jobs[1].id, score=50, decision="reject",
                                reject_reason=f"score 50 {LOW}", prefs_version=1),
        jobs[2].id: MatchResult(job_id=jobs[2].id, score=30, decision="reject",
                                reject_reason=f"score 30 {LOW}", prefs_version=1),
        jobs[3].id: MatchResult(job_id=jobs[3].id, score=90, decision="reject",
                                reject_reason="senior-level title", prefs_version=1),
        jobs[4].id: MatchResult(job_id=jobs[4].id, score=95, decision="match", prefs_version=1),
        jobs[5].id: MatchResult(job_id=jobs[5].id, score=80, decision="match", prefs_version=1),
    }  # fmt: skip
    picked = candidates(jobs, m, alerted={jobs[4].id}, min_score=55)
    assert [j.id for j in picked] == [jobs[0].id, jobs[1].id]  # best first


class FakeChain:
    def __init__(self, answer: AIInsight | None = None) -> None:
        self.answer, self.calls, self.errors, self.available = answer, 0, [], True

    async def extract(self, http, job):
        self.calls += 1
        return self.answer


async def test_enrich_caches_and_respects_budgets():
    repo = MemoryRepository()
    jobs = [job(native=str(i)) for i in range(10)]
    chain = FakeChain(insight(role_type="backend", summary="Builds APIs."))
    updated, report = await enrich(repo, chain, jobs, run_budget=4, daily_budget=6, now=NOW)
    assert chain.calls == 4 and report.applied == 4 and len(updated) == 4
    assert repo.get_insights([insight_key(jobs[0])])
    assert updated[0].normalized.ai.summary == "Builds APIs."
    _, second = await enrich(repo, chain, jobs[4:], run_budget=4, daily_budget=6, now=NOW)
    assert second.calls == 2  # only 2 left of the daily 6
    _, third = await enrich(repo, chain, jobs[6:], run_budget=4, daily_budget=6, now=NOW)
    assert third.calls == 0
    tomorrow = NOW.replace(day=6)
    _, fourth = await enrich(repo, chain, jobs[6:], run_budget=4, daily_budget=6, now=tomorrow)
    assert fourth.calls == 4  # budget resets daily


async def test_enrich_survives_llm_returning_nothing():
    repo = MemoryRepository()
    updated, report = await enrich(
        repo, FakeChain(None), [job()], run_budget=5, daily_budget=5, now=NOW
    )
    assert updated == [] and report.failed == 1
