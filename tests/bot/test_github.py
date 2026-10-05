from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.bot.github import DispatchError, dispatch_scan, scan_in_flight
from jobbot.settings import ConfigError, Settings

DISPATCH = (
    "https://api.github.com/repos/Gaurav-Kumar00/Jobbot/actions/workflows/scan.yml/dispatches"
)
RUNS = "https://api.github.com/repos/Gaurav-Kumar00/Jobbot/actions/workflows/scan.yml/runs"


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "github_pat_test")
    return Settings()


@respx.mock
async def test_dispatch_sends_force_input(settings):
    route = respx.post(DISPATCH).mock(return_value=httpx.Response(204))
    url = await dispatch_scan(settings, force=True)
    assert url.endswith("/actions/workflows/scan.yml")
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer github_pat_test"
    assert b'"force":"true"' in request.content and b'"ref":"main"' in request.content


@respx.mock
async def test_dispatch_permission_error_is_clear_and_hides_token(settings):
    respx.post(DISPATCH).mock(return_value=httpx.Response(403, json={"message": "nope"}))
    with pytest.raises(DispatchError, match="HTTP 403") as exc:
        await dispatch_scan(settings, force=False)
    assert "github_pat_test" not in str(exc.value)


async def test_dispatch_requires_token():
    with pytest.raises(ConfigError):
        await dispatch_scan(Settings(), force=True)


@respx.mock
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [(["completed", "completed"], False), (["queued"], True), (["completed", "in_progress"], True)],
)
async def test_scan_in_flight(settings, statuses, expected):
    respx.get(RUNS).mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"status": s} for s in statuses]})
    )
    assert await scan_in_flight(settings) is expected


@respx.mock
async def test_scan_in_flight_unknown_on_api_error(settings):
    respx.get(RUNS).mock(return_value=httpx.Response(500))
    assert await scan_in_flight(settings) is False
