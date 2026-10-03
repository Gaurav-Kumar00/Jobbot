"""End-to-end normalisation of whole jobs, including real recorded Greenhouse postings."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from jobbot.http import HttpClient
from jobbot.models import CompanyTarget, Job, RemoteScope, WorkMode
from jobbot.normalize import NORMALIZER_VERSION, needs_renormalize, normalize_job
from jobbot.sources.greenhouse import GreenhouseSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse"


def make_job(**fields) -> Job:
    base = {
        "id": "greenhouse:acme:1",
        "source": "greenhouse",
        "company": "acme",
        "company_name": "Acme",
        "title": "Backend Engineer",
        "url": "https://x/1",
    }
    return Job(**{**base, **fields})


def test_ideal_backend_fresher_job():
    job = normalize_job(
        make_job(
            title="SDE 1 - Backend",
            location_raw="Bengaluru, India (Hybrid)",
            description=(
                "We're hiring 2026 batch graduates (0-1 years of experience).\n"
                "• Build REST APIs in Python, Django and FastAPI\n"
                "• Work with Kafka, Celery, PostgreSQL and Redis on AWS\n"
                "Compensation: ₹16-20 LPA base"
            ),
        )
    )
    n = job.normalized
    assert n.version == NORMALIZER_VERSION
    assert n.role_category == "backend"
    assert n.seniority == "junior"
    assert n.employment_type == "full_time"
    assert (n.experience.min_years, n.experience.max_years, n.experience.fresher) == (0, 1, True)
    assert n.location.cities == ["bengaluru"]
    assert n.location.work_mode is WorkMode.HYBRID
    assert (n.salary.min, n.salary.max, n.salary.kind) == (16, 20, "base")
    assert {"python", "django", "fastapi", "kafka", "celery", "postgresql", "redis", "aws"} <= set(
        n.skills
    )
    assert job.fingerprint == "acme|software engineer 1 backend|bengaluru"


def test_hints_feed_normalisation():
    job = normalize_job(
        make_job(
            location_raw="",
            hints={
                "workplace": "remote",
                "employment": "Internship",
                "salary": "₹50,000 per month",
            },
        )
    )
    assert job.normalized.location.work_mode is WorkMode.REMOTE
    assert job.normalized.location.remote_scope is RemoteScope.UNKNOWN
    assert job.normalized.employment_type == "intern"
    assert job.normalized.salary.min == 6


def test_minimal_job_does_not_crash():
    n = normalize_job(make_job(title="?", location_raw="", description="")).normalized
    assert n.role_category == "unknown"
    assert n.salary is None
    assert n.experience.min_years is None
    assert n.skills == []


def test_needs_renormalize():
    raw = make_job()
    assert needs_renormalize(raw)
    done = normalize_job(raw)
    assert not needs_renormalize(done)
    stale = done.model_copy(
        update={"normalized": done.normalized.model_copy(update={"version": 0})}
    )
    assert needs_renormalize(stale)


async def test_real_greenhouse_jobs_normalise_sensibly():
    target = CompanyTarget(
        key="razorpay", name="Razorpay", ats="greenhouse", slug="razorpaysoftwareprivatelimited"
    )
    payload = json.loads((FIXTURES / "razorpay.json").read_text())

    async def no_sleep(_):
        return None

    with respx.mock:
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs"
        ).mock(return_value=httpx.Response(200, json=payload))
        async with HttpClient(sleep=no_sleep) as http:
            result = await GreenhouseSource().fetch(target, http)

    by_title = {j.title: normalize_job(j) for j in result.jobs}
    assert by_title["DevOps Enginer"].normalized.role_category == "devops"
    assert by_title["Product Manager II"].normalized.role_category == "non_software"
    assert (
        by_title["Associate Manager, Mid Market Sales"].normalized.role_category == "non_software"
    )
    for job in by_title.values():
        assert job.normalized.location.countries  # every Razorpay posting resolves to a country
        assert job.fingerprint.startswith("razorpay|")


def test_legacy_untyped_normalized_loads_as_needing_renormalise():
    # Phase-3 documents stored `normalized: {}` before the field was typed.
    job = Job.model_validate({**make_job().model_dump(), "normalized": {}})
    assert job.normalized is None
    assert needs_renormalize(job)
    old_shape = Job.model_validate({**make_job().model_dump(), "normalized": {"exp_min": 0}})
    assert old_shape.normalized is None
