"""Start the GitHub Actions scan workflow (for /scan_now and the hourly tick)."""

from __future__ import annotations

import httpx

from jobbot.settings import ConfigError, Settings

API = "https://api.github.com/repos/{repo}/actions/workflows/scan.yml/dispatches"


class DispatchError(Exception):
    pass


async def dispatch_scan(settings: Settings, *, force: bool, trigger: str = "bot") -> str:
    """Trigger scan.yml on main. Returns the Actions page URL for the workflow."""
    if settings.gh_dispatch_token is None or not settings.gh_dispatch_token.get_secret_value():
        raise ConfigError("GH_DISPATCH_TOKEN is not set")
    repo = settings.github_repository
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            API.format(repo=repo),
            headers={
                "Authorization": f"Bearer {settings.gh_dispatch_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main", "inputs": {"force": "true" if force else "false"}},
        )
    if resp.status_code != 204:
        raise DispatchError(f"GitHub refused the dispatch (HTTP {resp.status_code})")
    return f"https://github.com/{repo}/actions/workflows/scan.yml"


RUNS = "https://api.github.com/repos/{repo}/actions/workflows/scan.yml/runs"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


async def scan_in_flight(settings: Settings) -> bool:
    """True if a scan run is already queued or running (so a tick shouldn't add another)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.gh_dispatch_token is not None and settings.gh_dispatch_token.get_secret_value():
        headers["Authorization"] = f"Bearer {settings.gh_dispatch_token.get_secret_value()}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            RUNS.format(repo=settings.github_repository), headers=headers, params={"per_page": 5}
        )
    if resp.status_code != 200:
        return False  # unknown: let the 50-minute rule decide
    return any(run.get("status") in ACTIVE for run in resp.json().get("workflow_runs", []))
