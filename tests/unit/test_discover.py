from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.http import HttpClient
from jobbot.pipeline.discover import discover, slug_candidates, yaml_line


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Razorpay", ["razorpay", "Razorpay"]),
        ("Sarvam AI", ["sarvamai", "sarvam-ai", "sarvam", "SarvamAI"]),
        ("Pine Labs", ["pinelabs", "pine-labs", "pine", "PineLabs"]),
        ("Urban Company", ["urbancompany", "urban-company", "UrbanCompany"]),
    ],
)
def test_slug_candidates(name, expected):
    assert slug_candidates(name) == expected


async def no_sleep(_):
    return None


@respx.mock
async def test_discover_finds_board_and_counts_india_engineering():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/sarvam").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Backend Engineer",
                        "jobUrl": "u1",
                        "location": "Bengaluru",
                    },
                    {
                        "id": "2",
                        "title": "Marketing Intern",
                        "jobUrl": "u2",
                        "location": "Bengaluru",
                    },
                    {
                        "id": "3",
                        "title": "Backend Engineer",
                        "jobUrl": "u3",
                        "location": "London, UK",
                    },
                ]
            },
        )
    )
    respx.route().mock(return_value=httpx.Response(404))  # every other board: not found
    async with HttpClient(sleep=no_sleep, max_retries=0) as http:
        hits = await discover("Sarvam AI", http)
    assert [(h.ats, h.slug, h.total, h.india, h.india_engineering) for h in hits] == [
        ("ashby", "sarvam", 3, 2, 1)
    ]
    assert yaml_line("Sarvam AI", hits[0]).startswith(
        "  - {key: sarvamai, name: Sarvam AI, ats: ashby, slug: sarvam}"
    )


@respx.mock
async def test_discover_nothing_found():
    respx.route().mock(return_value=httpx.Response(404))
    async with HttpClient(sleep=no_sleep, max_retries=0) as http:
        assert await discover("Nonexistent Co", http) == []
