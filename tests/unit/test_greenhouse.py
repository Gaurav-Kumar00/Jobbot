from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.http import FetchError, HttpClient
from jobbot.models import CompanyTarget
from jobbot.sources.base import MAX_DESCRIPTION_CHARS
from jobbot.sources.greenhouse import GreenhouseSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse"
RAZORPAY = CompanyTarget(
    key="razorpay", name="Razorpay", ats="greenhouse", slug="razorpaysoftwareprivatelimited"
)
API = "https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def http():
    async with HttpClient(sleep=no_sleep, jitter=lambda: 0.0) as client:
        yield client


async def fetch(http, payload, status=200):
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(status, json=payload))
        return await GreenhouseSource().fetch(RAZORPAY, http)


async def test_parses_recorded_razorpay_board(http):
    result = await fetch(http, fixture("razorpay.json"))
    assert result.skipped == 0
    assert len(result.jobs) == 3

    job = next(j for j in result.jobs if j.title == "DevOps Enginer")
    assert job.id == f"greenhouse:razorpay:{job.raw['id']}"
    assert job.source == "greenhouse"
    assert job.company == "razorpay"
    assert job.company_name == "Razorpay"
    assert job.url.startswith("https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/")
    assert job.location_raw
    assert job.posted_at is not None and job.posted_at.tzinfo is UTC
    assert "<" not in job.description and "&lt;" not in job.description
    assert len(job.content_hash) == 16
    assert "content" not in job.raw  # description is stored once, not twice


async def test_sends_content_param(http):
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json={"jobs": []}))
        await GreenhouseSource().fetch(RAZORPAY, http)
    assert route.calls.last.request.url.params["content"] == "true"


async def test_parses_second_real_board_shape(http):
    groww = CompanyTarget(key="groww", name="Groww", ats="greenhouse", slug="groww")
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/groww/jobs").mock(
            return_value=httpx.Response(200, json=fixture("groww.json"))
        )
        result = await GreenhouseSource().fetch(groww, http)
    assert len(result.jobs) == 7
    assert all(j.id.startswith("greenhouse:groww:") for j in result.jobs)


async def test_posted_at_uses_first_published_with_offset(http):
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "absolute_url": "https://x/1",
                "location": {"name": "Bengaluru"},
                "first_published": "2026-09-21T02:32:37-04:00",
                "updated_at": "2026-09-28T07:02:10-04:00",
            }
        ]
    }
    (job,) = (await fetch(http, payload)).jobs
    assert job.posted_at == datetime(2026, 9, 21, 6, 32, 37, tzinfo=UTC)


async def test_falls_back_to_updated_at_then_none(http):
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "A",
                "absolute_url": "https://x/1",
                "updated_at": "2026-09-28T07:02:10Z",
            },
            {"id": 2, "title": "B", "absolute_url": "https://x/2", "first_published": "not a date"},
        ]
    }
    jobs = {j.title: j for j in (await fetch(http, payload)).jobs}
    assert jobs["A"].posted_at == datetime(2026, 9, 28, 7, 2, 10, tzinfo=UTC)
    assert jobs["B"].posted_at is None
    assert jobs["B"].location_raw == ""


async def test_malformed_items_are_skipped_not_fatal(http):
    payload = {
        "jobs": [
            {"id": 1, "title": "Good", "absolute_url": "https://x/1"},
            {"title": "No id", "absolute_url": "https://x/2"},
            {"id": 3, "absolute_url": "https://x/3"},  # no title
            {"id": 4, "title": "   ", "absolute_url": "https://x/4"},  # blank title
            "not even a dict",
            {"id": 5, "title": "Bad location", "absolute_url": "https://x/5", "location": "Pune"},
        ]
    }
    result = await fetch(http, payload)
    assert [j.title for j in result.jobs] == ["Good"]
    assert result.skipped == 5
    assert len(result.errors) == 5


async def test_long_descriptions_are_capped(http):
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "absolute_url": "https://x/1",
                "content": "<p>" + "python " * 5000 + "</p>",
            }
        ]
    }
    (job,) = (await fetch(http, payload)).jobs
    assert len(job.description) <= MAX_DESCRIPTION_CHARS + 2


async def test_content_hash_changes_with_content(http):
    base = {"id": 1, "title": "Backend Engineer", "absolute_url": "https://x/1", "content": "a"}
    (first,) = (await fetch(http, {"jobs": [base]})).jobs
    (same,) = (await fetch(http, {"jobs": [dict(base)]})).jobs
    (edited,) = (await fetch(http, {"jobs": [{**base, "content": "b"}]})).jobs
    assert first.content_hash == same.content_hash != edited.content_hash


async def test_empty_board(http):
    result = await fetch(http, {"jobs": [], "meta": {"total": 0}})
    assert result.jobs == [] and result.skipped == 0


@pytest.mark.parametrize("payload", [{"error": "x"}, [], {"jobs": "nope"}])
async def test_unexpected_shape_raises(http, payload):
    with pytest.raises(FetchError, match="unexpected response shape"):
        await fetch(http, payload)


async def test_missing_board_raises_404(http):
    with pytest.raises(FetchError) as exc:
        await fetch(http, {"status": 404, "error": "Job not found"}, status=404)
    assert exc.value.status_code == 404
