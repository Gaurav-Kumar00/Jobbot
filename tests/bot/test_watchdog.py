from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobbot.bot.watchdog import tick
from jobbot.models import RunSummary
from jobbot.notify.base import OutgoingMessage
from jobbot.storage import MemoryRepository

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "1"


def run(repo, minutes_ago: int, ok: int = 10) -> None:
    started = NOW - timedelta(minutes=minutes_ago)
    repo.record_run(
        RunSummary(
            started_at=started, finished_at=started + timedelta(minutes=2), stats={"sources_ok": ok}
        )
    )


async def dispatcher(calls):
    async def dispatch(force: bool) -> str:
        calls.append(force)
        return "url"

    return dispatch


async def test_recent_scan_means_no_dispatch_and_no_alert():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 20)
    result = await tick(repo, notifier, await dispatcher(calls), NOW)
    assert result == {"dispatched": False, "last_success_minutes": 18, "stale": False}
    assert calls == [] and notifier.sent == []


async def test_skipped_github_schedule_is_backfilled():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 75)
    result = await tick(repo, notifier, await dispatcher(calls), NOW)
    assert result["dispatched"] and calls == [False]  # normal scan, intervals apply
    assert notifier.sent == []  # 73 min is not yet "stuck"


async def test_stuck_scans_alert_once_then_recover():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 200)
    await tick(repo, notifier, await dispatcher(calls), NOW)
    await tick(repo, notifier, await dispatcher(calls), NOW + timedelta(hours=1))
    assert len(notifier.sent) == 1 and "No successful scan for 3 hours" in notifier.sent[0].text
    run(repo, -60)  # a scan succeeded later
    await tick(repo, notifier, await dispatcher(calls), NOW + timedelta(hours=1, minutes=5))
    assert "healthy again" in notifier.sent[-1].text


async def test_runs_where_every_source_failed_do_not_count_as_success():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 300, ok=10)
    run(repo, 30, ok=0)  # recent, but nothing worked
    result = await tick(repo, notifier, await dispatcher(calls), NOW)
    assert result["stale"] and len(notifier.sent) == 1


async def test_dispatch_failure_is_reported_not_raised():
    repo, notifier = MemoryRepository(), Recorder()

    async def broken(force: bool) -> str:
        raise RuntimeError("GitHub down")

    result = await tick(repo, notifier, broken, NOW)
    assert result["dispatch_error"] == "GitHub down" and result["stale"]


async def test_no_second_dispatch_while_a_scan_is_queued_or_running():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 75)

    async def busy() -> bool:
        return True

    result = await tick(repo, notifier, await dispatcher(calls), NOW, in_flight=busy)
    assert result["in_flight"] is True and result["dispatched"] is False and calls == []


async def test_idle_github_allows_dispatch():
    repo, notifier, calls = MemoryRepository(), Recorder(), []
    run(repo, 75)

    async def idle() -> bool:
        return False

    result = await tick(repo, notifier, await dispatcher(calls), NOW, in_flight=idle)
    assert result["dispatched"] is True and calls == [False]
