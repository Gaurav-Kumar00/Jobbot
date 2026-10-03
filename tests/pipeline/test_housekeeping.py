"""Storage safety net."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobbot.models import AlertTrigger, Job, MatchResult
from jobbot.notify.base import OutgoingMessage
from jobbot.pipeline.housekeeping import housekeeping
from jobbot.storage import MemoryRepository

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "1"


def add(repo, native_id, *, days_ago, active=False, decision="reject", alerted=False):
    job = Job(
        id=f"gh:x:{native_id}",
        source="gh",
        company="x",
        title="t",
        url="u",
        description="long description",
        raw={"k": "v"},
        fingerprint=f"x|t|{native_id}",
        first_seen_at=NOW - timedelta(days=days_ago),
        last_seen_at=NOW - timedelta(days=days_ago),
        is_active=active,
    )
    repo.upsert_job(job)
    if not active:
        repo.jobs[job.id] = repo.jobs[job.id].model_copy(update={"is_active": False})
    repo.save_match(MatchResult(job_id=job.id, score=50, decision=decision, prefs_version=1))
    if alerted:
        repo.claim_alert(job.id, AlertTrigger.NEW, 80)
        repo.mark_alert_sent(job.id, "1")
    return job.id


class SizedRepo(MemoryRepository):
    def __init__(self, used_bytes: int) -> None:
        super().__init__()
        self.used_bytes = used_bytes

    def storage_bytes(self) -> int:
        return self.used_bytes


MB = 1024 * 1024


async def test_compaction_rules():
    repo = SizedRepo(10 * MB)
    rejected_8d = add(repo, "r8", days_ago=8)
    rejected_3d = add(repo, "r3", days_ago=3)
    matched_8d = add(repo, "m8", days_ago=8, decision="match", alerted=True)
    matched_31d = add(repo, "m31", days_ago=31, decision="match", alerted=True)
    active_old = add(repo, "a", days_ago=40, active=True)

    report = await housekeeping(repo, Recorder(), now=NOW)

    assert repo.jobs[rejected_8d].compacted  # rejected + inactive > 7 days
    assert not repo.jobs[rejected_3d].compacted
    assert not repo.jobs[matched_8d].compacted  # alerted jobs keep details for 30 days
    assert repo.jobs[matched_31d].compacted
    assert not repo.jobs[active_old].compacted  # active postings are never touched
    assert report.compacted == 2
    assert repo.jobs[rejected_8d].fingerprint == "x|t|r8"  # dedup still works


async def test_retention_deletes_only_never_matched_ancient_jobs():
    repo = SizedRepo(10 * MB)
    junk = add(repo, "junk", days_ago=200)
    alerted = add(repo, "kept", days_ago=200, decision="match", alerted=True)
    report = await housekeeping(repo, Recorder(), now=NOW)
    assert report.deleted == 1
    assert junk not in repo.jobs and junk not in repo.matches
    assert alerted in repo.jobs


async def test_storage_guard_warns_at_most_daily():
    repo = SizedRepo(int(0.72 * 512 * MB))
    notifier = Recorder()
    first = await housekeeping(repo, notifier, now=NOW)
    assert first.warned and first.used_pct == 72.0
    assert "72% full" in notifier.sent[0].text
    again = await housekeeping(repo, notifier, now=NOW + timedelta(hours=3))
    assert not again.warned and len(notifier.sent) == 1
    tomorrow = await housekeeping(repo, notifier, now=NOW + timedelta(hours=25))
    assert tomorrow.warned and len(notifier.sent) == 2


async def test_no_warning_below_threshold():
    notifier = Recorder()
    report = await housekeeping(SizedRepo(100 * MB), notifier, now=NOW)
    assert not report.warned and notifier.sent == []


async def test_emergency_compacts_every_inactive_job():
    repo = SizedRepo(int(0.86 * 512 * MB))
    fresh_inactive = add(repo, "f", days_ago=1, decision="match", alerted=True)
    notifier = Recorder()
    report = await housekeeping(repo, notifier, now=NOW)
    assert repo.jobs[fresh_inactive].compacted
    assert report.compacted == 1
    assert "emergency mode" in notifier.sent[0].text


async def test_critical_compacts_active_rejected_jobs_but_alerting_continues():
    repo = SizedRepo(int(0.96 * 512 * MB))
    active_rejected = add(repo, "ar", days_ago=0, active=True)
    active_matched = add(repo, "am", days_ago=0, active=True, decision="match")
    await housekeeping(repo, Recorder(), now=NOW)
    assert repo.jobs[active_rejected].compacted
    assert not repo.jobs[active_matched].compacted
