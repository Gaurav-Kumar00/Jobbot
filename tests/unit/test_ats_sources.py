"""Lever, Ashby, SmartRecruiters and Workable adapters against recorded real responses."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget
from jobbot.normalize import normalize_job
from jobbot.sources import SOURCES
from jobbot.sources.ashby import AshbySource
from jobbot.sources.lever import LeverSource
from jobbot.sources.smartrecruiters import SmartRecruitersSource
from jobbot.sources.workable import WorkableSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(path: str):
    return json.loads((FIXTURES / path).read_text())


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def http():
    async with HttpClient(sleep=no_sleep, jitter=lambda: 0.0) as client:
        yield client


def target(ats: str, slug: str, **options) -> CompanyTarget:
    return CompanyTarget(key=slug.lower(), name=slug, ats=ats, slug=slug, options=options)


def test_all_adapters_registered():
    assert {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"} <= set(SOURCES)
    for name, source in SOURCES.items():
        assert source.name == name  # pipeline relies on job.source == ats name


# --- Lever ------------------------------------------------------------------------------

LEVER_URL = "https://api.lever.co/v0/postings/paytm"


@respx.mock
async def test_lever_recorded_paytm(http):
    payload = fixture("lever/paytm.json")
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await LeverSource().fetch(target("lever", "paytm"), http)
    assert result.skipped == 0 and len(result.jobs) == len(payload)
    job = result.jobs[0]
    assert job.id == f"lever:paytm:{payload[0]['id']}"
    assert job.source == "lever"
    assert job.url == payload[0]["hostedUrl"]
    assert job.posted_at is not None and job.posted_at.tzinfo is UTC
    assert "India" in job.location_raw or "india" in job.location_raw.lower()
    assert len(job.description) > 100  # description + lists sections merged
    assert "<" not in job.description


@respx.mock
async def test_lever_fields_and_hints(http):
    item = {
        "id": "abc",
        "text": "Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/abc",
        "createdAt": 1790573982021,
        "country": "IN",
        "workplaceType": "hybrid",
        "categories": {"commitment": "Full-time", "allLocations": ["Bengaluru", "Hyderabad"]},
        "descriptionPlain": "Build APIs.",
        "lists": [{"text": "Requirements", "content": "<li>0-2 years</li><li>Python</li>"}],
        "additionalPlain": "Equal opportunity.",
        "salaryRange": {
            "min": 1200000,
            "max": 1800000,
            "currency": "INR",
            "interval": "per-year-salary",
        },
    }
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json=[item]))
    (job,) = (await LeverSource().fetch(target("lever", "paytm"), http)).jobs
    assert job.location_raw == "Bengaluru / Hyderabad, India"
    assert job.hints == {
        "workplace": "hybrid",
        "employment": "Full-time",
        "salary": "Salary: INR 1,200,000 - 1,800,000 per annum",
    }
    assert "Requirements\n• 0-2 years" in job.description
    n = normalize_job(job).normalized
    assert n.location.cities == ["bengaluru", "hyderabad"]
    assert n.location.work_mode.value == "hybrid"
    assert (n.salary.min, n.salary.max) == (12, 18)
    assert (n.experience.min_years, n.experience.max_years) == (0, 2)


@respx.mock
async def test_lever_unspecified_workplace_is_not_a_hint(http):
    item = {"id": "1", "text": "SDE", "hostedUrl": "u", "workplaceType": "unspecified"}
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json=[item]))
    (job,) = (await LeverSource().fetch(target("lever", "paytm"), http)).jobs
    assert "workplace" not in job.hints


@respx.mock
async def test_lever_malformed_and_bad_shape(http):
    respx.get(LEVER_URL).mock(
        return_value=httpx.Response(200, json=[{"id": "1"}, {"text": "x", "hostedUrl": "u"}])
    )
    result = await LeverSource().fetch(target("lever", "paytm"), http)
    assert result.jobs == [] and result.skipped == 2
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json={"ok": False}))
    with pytest.raises(FetchError, match="expected a list"):
        await LeverSource().fetch(target("lever", "paytm"), http)


# --- Ashby -------------------------------------------------------------------------------

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/atlan"


@respx.mock
async def test_ashby_recorded_atlan(http):
    payload = fixture("ashby/atlan.json")
    route = respx.get(ASHBY_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await AshbySource().fetch(target("ashby", "atlan"), http)
    assert route.calls.last.request.url.params["includeCompensation"] == "true"
    listed = [j for j in payload["jobs"] if j.get("isListed", True)]
    assert len(result.jobs) == len(listed) and result.skipped == 0
    india = [j for j in result.jobs if "India" in j.location_raw]
    assert india, "Atlan lists India roles"
    remote = [j for j in result.jobs if j.hints.get("workplace") == "Remote"]
    assert remote and normalize_job(remote[0]).normalized.location.work_mode.value == "remote"


@respx.mock
async def test_ashby_fields(http):
    payload = {
        "jobs": [
            {
                "id": "a1",
                "title": "Software Engineer I",
                "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
                "location": "Bengaluru",
                "secondaryLocations": [{"location": "Remote - India"}],
                "address": {"postalAddress": {"addressCountry": "India"}},
                "employmentType": "FullTime",
                "workplaceType": "Hybrid",
                "publishedAt": "2026-09-30T10:00:00.000+00:00",
                "descriptionPlain": "Python and FastAPI.",
                "compensation": {"scrapeableCompensationSalarySummary": "₹18L – ₹24L"},
            },
            {"id": "hidden", "title": "Unlisted", "jobUrl": "u", "isListed": False},
        ]
    }
    respx.get(ASHBY_URL).mock(return_value=httpx.Response(200, json=payload))
    (job,) = (await AshbySource().fetch(target("ashby", "atlan"), http)).jobs
    assert job.location_raw == "Bengaluru / Remote - India"
    assert job.hints == {
        "workplace": "Hybrid",
        "employment": "full-time",
        "salary": "Salary: ₹18L – ₹24L",
    }
    n = normalize_job(job).normalized
    assert (n.salary.min, n.salary.max) == (18, 24)
    assert n.employment_type == "full_time"


@respx.mock
async def test_ashby_bad_shape(http):
    respx.get(ASHBY_URL).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(FetchError):
        await AshbySource().fetch(target("ashby", "atlan"), http)


# --- SmartRecruiters --------------------------------------------------------------------

SR_LIST = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"


@respx.mock
async def test_smartrecruiters_recorded_bosch_with_details(http):
    listing = fixture("smartrecruiters/list.json")
    detail = fixture("smartrecruiters/detail.json")
    list_route = respx.get(SR_LIST).mock(return_value=httpx.Response(200, json=listing))
    detail_routes = {
        item["id"]: respx.get(f"{SR_LIST}/{item['id']}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        for item in listing["content"]
    }
    result = await SmartRecruitersSource().fetch(target("smartrecruiters", "BoschGroup"), http)
    assert list_route.calls.last.request.url.params["country"] == "in"
    assert len(result.jobs) == len(listing["content"]) and result.skipped == 0
    job = next(j for j in result.jobs if "Python" in j.title)
    assert job.url.startswith("https://jobs.smartrecruiters.com/BoschGroup/")
    assert "India" in job.location_raw
    assert job.description  # filled from the detail endpoint
    # "Senior ..." titles are not worth a detail request
    senior = next(i for i in listing["content"] if "Senior" in i["name"])
    assert detail_routes[senior["id"]].call_count == 0


@respx.mock
async def test_smartrecruiters_paginates_and_uses_query(http):
    def page(offset: int, n: int):
        return {
            "offset": offset,
            "limit": 100,
            "totalFound": 150,
            "content": [
                {"id": str(offset + i), "name": "Account Manager", "location": {"country": "in"}}
                for i in range(n)
            ],
        }

    route = respx.get(SR_LIST).mock(
        side_effect=[
            httpx.Response(200, json=page(0, 100)),
            httpx.Response(200, json=page(100, 50)),
        ]
    )
    result = await SmartRecruitersSource().fetch(
        target("smartrecruiters", "BoschGroup", query="software"), http
    )
    assert len(result.jobs) == 150
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "100"]
    assert route.calls.last.request.url.params["q"] == "software"


@respx.mock
async def test_smartrecruiters_failed_detail_still_lists_job(http):
    listing = {
        "totalFound": 1,
        "content": [
            {"id": "9", "name": "Backend Engineer", "location": {"city": "Pune", "country": "in"}}
        ],
    }
    respx.get(SR_LIST).mock(return_value=httpx.Response(200, json=listing))
    respx.get(f"{SR_LIST}/9").mock(return_value=httpx.Response(404))
    (job,) = (
        await SmartRecruitersSource().fetch(target("smartrecruiters", "BoschGroup"), http)
    ).jobs
    assert job.description == "" and job.location_raw == "Pune, India"


# --- Workable ----------------------------------------------------------------------------

WORKABLE_URL = "https://apply.workable.com/api/v1/widget/accounts/huggingface"


@respx.mock
async def test_workable_recorded(http):
    payload = fixture("workable/huggingface.json")
    respx.get(WORKABLE_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await WorkableSource().fetch(target("workable", "huggingface"), http)
    assert len(result.jobs) == len(payload["jobs"]) and result.skipped == 0
    job = result.jobs[0]
    assert job.id == f"workable:huggingface:{payload['jobs'][0]['shortcode']}"
    assert job.hints.get("workplace") == "remote"
    assert "Experience level:" in job.description


@respx.mock
async def test_workable_rate_limit_is_retried(http):
    respx.get(WORKABLE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )
    result = await WorkableSource().fetch(target("workable", "huggingface"), http)
    assert result.jobs == []


# --- Keka ---------------------------------------------------------------------------------

KEKA_URL = "https://cars24.keka.com/careers/api/jobs/default/active"


@respx.mock
async def test_keka_recorded_cars24(http):
    from jobbot.sources.keka import KekaSource

    payload = fixture("keka/cars24.json")
    respx.get(KEKA_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await KekaSource().fetch(target("keka", "cars24"), http)
    assert len(result.jobs) == len(payload) and result.skipped == 0
    job = result.jobs[0]
    assert job.url == f"https://cars24.keka.com/careers/jobdetails/{payload[0]['id']}"
    assert job.posted_at is not None
    assert "<" not in job.description


@respx.mock
async def test_keka_experience_and_location(http):
    from jobbot.sources.keka import KekaSource

    item = {
        "id": 7,
        "title": "SDE 1 - Backend",
        "description": "<p>Python, Django</p>",
        "experience": "0-1",
        "skillNames": ["Python", "Kafka"],
        "jobLocations": [{"city": "Gurugram", "countryName": "India"}],
        "publishedOn": "2026-10-02T08:00:29.197Z",
    }
    respx.get(KEKA_URL).mock(return_value=httpx.Response(200, json=[item]))
    (job,) = (await KekaSource().fetch(target("keka", "cars24"), http)).jobs
    n = normalize_job(job).normalized
    assert job.location_raw == "Gurugram, India"
    assert (n.experience.min_years, n.experience.max_years) == (0, 1)
    assert {"python", "django", "kafka"} <= set(n.skills)


@respx.mock
async def test_keka_bad_shape(http):
    from jobbot.sources.keka import KekaSource

    respx.get(KEKA_URL).mock(return_value=httpx.Response(200, json={"error": 1}))
    with pytest.raises(FetchError):
        await KekaSource().fetch(target("keka", "cars24"), http)


# --- Amazon / Microsoft / Atlassian --------------------------------------------------------

AMAZON_URL = "https://www.amazon.jobs/en/search.json"
MS_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
ATL_URL = "https://www.atlassian.com/endpoint/careers/listings"


@respx.mock
async def test_amazon_recorded(http):
    from jobbot.sources.amazon import AmazonSource

    payload = fixture("amazon/search.json")
    route = respx.get(AMAZON_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await AmazonSource().fetch(target("amazon", "amazon"), http)
    params = route.calls.last.request.url.params
    assert params["normalized_country_code[]"] == "IND" and params["sort"] == "recent"
    assert len(result.jobs) == len(payload["jobs"])
    job = result.jobs[0]
    assert job.url.startswith("https://www.amazon.jobs/en/jobs/")
    assert job.posted_at is not None
    assert "Basic qualifications" in job.description
    n = normalize_job(job).normalized
    assert "IN" in n.location.countries and n.location.cities == ["bengaluru"]


@respx.mock
async def test_amazon_paginates_and_dedupes_across_queries(http):
    from jobbot.sources.amazon import AmazonSource

    def page(ids):
        return {
            "hits": 150,
            "jobs": [
                {"id_icims": i, "title": "SDE", "job_path": f"/en/jobs/{i}", "posted_date": "x"}
                for i in ids
            ],
        }

    route = respx.get(AMAZON_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(range(100))),
            httpx.Response(200, json=page(range(100, 150))),
            httpx.Response(200, json={**page(range(140, 160)), "hits": 20}),
        ]
    )
    result = await AmazonSource().fetch(
        target("amazon", "amazon", queries=["sde", "backend"]), http
    )
    assert route.call_count == 3
    assert len(result.jobs) == 160  # ids 140-149 seen twice, kept once


@respx.mock
async def test_microsoft_recorded(http):
    from jobbot.sources.microsoft import MicrosoftSource

    payload = fixture("microsoft/search.json")
    respx.get(MS_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await MicrosoftSource().fetch(target("microsoft", "microsoft"), http)
    assert len(result.jobs) == 4
    job = result.jobs[0]
    assert job.url.startswith("https://apply.careers.microsoft.com/careers/job/")
    assert job.posted_at is not None
    n = normalize_job(job).normalized
    assert "IN" in n.location.countries
    assert {j.normalized.seniority for j in map(normalize_job, result.jobs)} >= {"senior", "mid"}


@respx.mock
async def test_microsoft_bad_shape(http):
    from jobbot.sources.microsoft import MicrosoftSource

    respx.get(MS_URL).mock(return_value=httpx.Response(200, json={"status": 500}))
    with pytest.raises(FetchError):
        await MicrosoftSource().fetch(target("microsoft", "microsoft"), http)


@respx.mock
async def test_atlassian_recorded(http):
    from jobbot.sources.atlassian import AtlassianSource

    payload = fixture("atlassian/listings.json")
    respx.get(ATL_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await AtlassianSource().fetch(target("atlassian", "atlassian"), http)
    assert len(result.jobs) == len(payload) and result.skipped == 0
    eng = result.jobs[0]
    # India roles live on Atlassian's APAC iCIMS portal, others on the global one
    assert eng.url.startswith("https://careers-apac-atlassian.icims.com/jobs/")
    assert eng.posted_at is not None
    assert "Category: Engineering" in eng.description
    n = normalize_job(eng).normalized
    assert "IN" in n.location.countries


@respx.mock
async def test_atlassian_remote_india_location(http):
    from jobbot.sources.atlassian import AtlassianSource

    item = {
        "id": 1,
        "title": "Software Engineer",
        "type": "Full-Time",
        "category": "Engineering",
        "locations": ["Bengaluru - India -   Bengaluru,  560071 India", "Remote - India - Remote"],
        "overview": "<p>Python services</p>",
        "applyUrl": "https://x/apply",
        "portalJobPost": {"portalUrl": "https://x/job", "updatedDate": "2026-09-24 03:33 PM"},
    }
    respx.get(ATL_URL).mock(return_value=httpx.Response(200, json=[item]))
    (job,) = (await AtlassianSource().fetch(target("atlassian", "atlassian"), http)).jobs
    loc = normalize_job(job).normalized.location
    assert loc.cities == ["bengaluru"] and loc.remote_scope.value == "india"


# --- Unstop -------------------------------------------------------------------------------

UNSTOP_URL = "https://unstop.com/api/public/opportunity/search-result"


@respx.mock
async def test_unstop_recorded_and_deduped_across_queries(http):
    from jobbot.sources.unstop import UnstopSource

    payload = fixture("unstop/python.json")
    route = respx.get(UNSTOP_URL).mock(return_value=httpx.Response(200, json=payload))
    result = await UnstopSource().fetch(target("unstop", "unstop"), http)
    assert route.calls.last.request.url.params["opportunity"] == "jobs"
    assert len(result.jobs) == len(payload["data"]["data"])  # same page for 4 queries, kept once
    job = result.jobs[0]
    assert job.source == "unstop" and job.url.startswith("https://unstop.com/")
    assert job.company_name and job.company_name != "unstop"  # the real employer
    assert job.posted_at is not None


@respx.mock
async def test_unstop_fields(http):
    from jobbot.sources.unstop import UnstopSource

    item = {
        "id": 1,
        "title": "Python Developer",
        "status": "LIVE",
        "seo_url": "https://unstop.com/jobs/python-developer-acme-1",
        "organisation": {"name": "Acme Labs"},
        "locations": [{"city": "Gurugram", "country": "India"}],
        "jobDetail": {
            "type": "hybrid",
            "timing": "full_time",
            "min_experience": 0,
            "max_experience": 1,
            "show_salary": 1,
            "min_salary": 1200000,
            "max_salary": 1600000,
            "pay_in": "annually",
        },
        "filters": [{"type": "eligible", "name": "Fresher"}],
        "required_skills": [{"skill_name": "Django"}, {"skill_name": "PostgreSQL"}],
        "details": "<p>Build APIs</p>",
        "approved_date": "2026-09-23 16:39:30 GMT+0530",
    }
    body = {"data": {"data": [item], "last_page": 1}}
    respx.get(UNSTOP_URL).mock(return_value=httpx.Response(200, json=body))
    (job,) = (await UnstopSource().fetch(target("unstop", "unstop", queries=["python"]), http)).jobs
    n = normalize_job(job).normalized
    assert job.company_name == "Acme Labs"
    assert n.location.cities == ["gurugram"] and n.location.work_mode.value == "hybrid"
    assert (n.experience.min_years, n.experience.max_years, n.experience.fresher) == (0, 1, True)
    assert (n.salary.min, n.salary.max) == (12, 16)
    assert {"django", "postgresql"} <= set(n.skills)
    assert job.posted_at.isoformat() == "2026-09-23T16:39:30+05:30"
