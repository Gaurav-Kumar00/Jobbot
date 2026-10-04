"""Adzuna and SerpApi (Google Jobs) adapters."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget
from jobbot.normalize import normalize_job
from jobbot.sources.adzuna import API_URL as ADZUNA_URL
from jobbot.sources.adzuna import AdzunaSource
from jobbot.sources.serpapi import API_URL as SERP_URL
from jobbot.sources.serpapi import SerpApiGoogleJobsSource, _best_link, _relative_time, _rotation

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(path: str):
    return json.loads((FIXTURES / path).read_text())


async def no_sleep(_):
    return None


@pytest.fixture
async def http():
    async with HttpClient(sleep=no_sleep, jitter=lambda: 0.0) as client:
        yield client


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "app-id-test")
    monkeypatch.setenv("ADZUNA_APP_KEY", "app-key-test")
    monkeypatch.setenv("SERPAPI_KEY", "serp-key-test")


def target(ats: str, **options) -> CompanyTarget:
    return CompanyTarget(key=ats, name=ats, ats=ats, slug=ats, options=options)


# --- Adzuna --------------------------------------------------------------------------------


async def test_adzuna_requires_keys(http):
    with pytest.raises(FetchError, match="not configured"):
        await AdzunaSource().fetch(target("adzuna"), http)


@respx.mock
async def test_adzuna_recorded(http, keys):
    payload = fixture("adzuna/python.json")
    route = respx.get(ADZUNA_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await AdzunaSource().fetch(target("adzuna", queries=["python developer"]), http)
    params = route.calls.last.request.url.params
    assert params["app_key"] == "app-key-test" and params["category"] == "it-jobs"
    assert len(result.jobs) == len(payload["results"])
    job = result.jobs[0]
    assert job.company_name == payload["results"][0]["company"]["display_name"]
    assert job.url.startswith("https://www.adzuna.in/")
    assert "IN" in normalize_job(job).normalized.location.countries


@respx.mock
async def test_adzuna_ignores_predicted_salaries_and_dedupes(http, keys):
    base = {
        "id": "1",
        "title": "Python Developer",
        "redirect_url": "https://www.adzuna.in/details/1",
        "company": {"display_name": "Acme"},
        "location": {"display_name": "Bangalore, Karnataka"},
        "created": "2026-10-04T11:54:03Z",
        "contract_time": "full_time",
    }
    real = {**base, "salary_min": 1200000, "salary_max": 1500000, "salary_is_predicted": "0"}
    guess = {**base, "id": "2", "salary_min": 900000, "salary_is_predicted": "1"}
    respx.get(ADZUNA_URL).mock(return_value=httpx.Response(200, json={"results": [real, guess]}))
    result = await AdzunaSource().fetch(target("adzuna", queries=["a", "b"]), http)
    assert len(result.jobs) == 2  # same results for both queries, kept once each
    by_id = {j.id: j for j in result.jobs}
    assert by_id["adzuna:adzuna:1"].hints["salary"] == "Salary: INR 1,200,000 - 1,500,000 per annum"
    assert "salary" not in by_id["adzuna:adzuna:2"].hints
    assert by_id["adzuna:adzuna:1"].location_raw == "Bangalore, Karnataka, India"


@respx.mock
async def test_adzuna_errors_never_leak_the_key(http, keys):
    respx.get(ADZUNA_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(FetchError) as exc:
        await AdzunaSource().fetch(target("adzuna", queries=["x"]), http)
    assert "app-key-test" not in str(exc.value)


# --- SerpApi / Google Jobs ----------------------------------------------------------------


async def test_serpapi_requires_key(http):
    with pytest.raises(FetchError, match="not configured"):
        await SerpApiGoogleJobsSource().fetch(target("serpapi"), http)


@respx.mock
async def test_serpapi_recorded(http, keys):
    payload = fixture("serpapi/bengaluru.json")
    route = respx.get(SERP_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await SerpApiGoogleJobsSource().fetch(target("serpapi", per_run=1), http)
    params = route.calls.last.request.url.params
    assert params["engine"] == "google_jobs" and params["gl"] == "in"
    assert route.call_count == 1  # budget: exactly per_run searches
    assert len(result.jobs) == len(payload["jobs_results"])
    job = result.jobs[0]
    assert (
        job.source == "serpapi" and job.company_name == payload["jobs_results"][0]["company_name"]
    )
    assert job.url.startswith("http")
    assert job.description.startswith("Listed on:")


def test_rotation_cycles_through_all_searches():
    searches = [{"q": str(i)} for i in range(4)]
    t0 = datetime(2026, 10, 4, tzinfo=UTC)
    seen = set()
    for step in range(4):
        picked = _rotation(searches, 2, 360, t0 + timedelta(hours=6 * step))
        assert len(picked) == 2
        seen.update(p["q"] for p in picked)
    assert seen == {"0", "1", "2", "3"}


def test_best_link_prefers_employer_site():
    options = [
        {"title": "LinkedIn", "link": "https://linkedin.com/jobs/1"},
        {"title": "Acme Careers", "link": "https://acme.com/careers/1"},
    ]
    assert _best_link(options) == "https://acme.com/careers/1"
    assert _best_link(options[:1]) == "https://linkedin.com/jobs/1"
    assert _best_link([]) is None


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("5 days ago", 120),
        ("3 hours ago", 3),
        ("1 week ago", 168),
        ("30+ days ago", 720),
        ("", None),
    ],
)
def test_relative_time(text, hours):
    now = datetime(2026, 10, 4, tzinfo=UTC)
    got = _relative_time(text, now)
    assert (now - got) == timedelta(hours=hours) if hours is not None else got is None


@respx.mock
async def test_serpapi_salary_and_schedule_hints(http, keys):
    item = {
        "title": "Python Developer",
        "company_name": "Acme",
        "location": "Bengaluru, Karnataka",
        "via": "Instahyre",
        "description": "0-1 years. Django.",
        "job_id": "abc",
        "detected_extensions": {
            "posted_at": "2 days ago",
            "schedule_type": "Full–time",
            "salary": "₹12L–₹16L a year",
        },
        "apply_options": [{"title": "Instahyre", "link": "https://instahyre.com/job-1"}],
    }
    respx.get(SERP_URL).mock(return_value=httpx.Response(200, json={"jobs_results": [item]}))
    (job,) = (await SerpApiGoogleJobsSource().fetch(target("serpapi", per_run=1), http)).jobs
    n = normalize_job(job).normalized
    assert (n.salary.min, n.salary.max) == (12, 16)
    assert n.employment_type == "full_time"
    assert job.url == "https://instahyre.com/job-1"


@respx.mock
async def test_serpapi_api_error_is_a_fetch_error(http, keys):
    respx.get(SERP_URL).mock(
        return_value=httpx.Response(200, json={"error": "Your account has run out of searches."})
    )
    with pytest.raises(FetchError, match="run out of searches"):
        await SerpApiGoogleJobsSource().fetch(target("serpapi", per_run=1), http)
