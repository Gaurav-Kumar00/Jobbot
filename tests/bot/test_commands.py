"""Telegram command handlers against an in-memory repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobbot.bot.commands import BotContext, handle
from jobbot.matching.preferences import load_profile
from jobbot.models import AlertTrigger, Job, MatchResult, RunSummary, SourceState
from jobbot.normalize import normalize_job
from jobbot.notify.base import OutgoingMessage
from jobbot.storage import MemoryRepository

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)  # 14:30 IST


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return str(len(self.sent))


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def ctx(repo):
    dispatched: list[bool] = []

    async def dispatch(force: bool) -> str:
        dispatched.append(force)
        return "https://github.com/x/y/actions/workflows/scan.yml"

    context = BotContext(
        repo=repo, notifier=Recorder(), dispatch_scan=dispatch, profile=load_profile(), now=NOW
    )
    context.dispatched = dispatched  # type: ignore[attr-defined]
    return context


async def text(command: str, ctx) -> str:
    replies = await handle(command, ctx)
    return "\n".join(r.text for r in replies)


def add_alerted_job(repo, native: str, *, title="Python Developer", sent_at=NOW, score=88):
    job = normalize_job(
        Job(
            id=f"lever:acme:{native}",
            source="lever",
            company="acme",
            company_name="Acme",
            title=title,
            url=f"https://jobs.lever.co/acme/{native}",
            location_raw="Bengaluru",
            description="0-1 years. Python, Django.",
        )
    )
    repo.upsert_job(job)
    repo.save_match(MatchResult(job_id=job.id, score=score, decision="match", prefs_version=1))
    repo.claim_alert(job.id, AlertTrigger.NEW, score)
    repo.mark_alert_sent(job.id, "1")
    repo.alerts[job.id] = repo.alerts[job.id].model_copy(update={"sent_at": sent_at})
    return job.id


# --- routing ------------------------------------------------------------------------------


async def test_help_lists_every_section(ctx):
    reply = await text("/help", ctx)
    for section in ("Jobs", "Control", "Preferences"):
        assert f"<b>{section}</b>" in reply
    assert "/set_salary" in reply and "/scan_now" in reply
    assert await text("/start", ctx) == reply


@pytest.mark.parametrize("message", ["hello", "", "   "])
async def test_non_commands_get_a_hint(ctx, message):
    assert "/help" in await text(message, ctx)


async def test_unknown_command_and_bot_suffix(ctx):
    assert "Unknown command /dance" in await text("/dance", ctx)
    assert "JobBot status" in await text("/status@Qeiroo_Bot", ctx)


# --- information -------------------------------------------------------------------------


async def test_status(ctx, repo):
    repo.record_run(
        RunSummary(
            started_at=NOW - timedelta(minutes=23),
            finished_at=NOW - timedelta(minutes=21),
            trigger="schedule",
            stats={"sources_ok": 183, "sources_due": 183},
        )
    )
    repo.save_source_state(
        SourceState(key="lever:broken", consecutive_failures=4, last_error="HTTP 500")
    )
    add_alerted_job(repo, "1")
    reply = await text("/status", ctx)
    assert "Last scan: 23 min ago (schedule) · 183/183 sources OK" in reply
    assert "1 failing (/sources)" in reply
    assert "Alerts today: 1" in reply
    assert "Alerts: ▶️ on" in reply


async def test_sources(ctx, repo):
    assert "All 0 sources healthy" in await text("/sources", ctx)
    repo.save_source_state(
        SourceState(key="lever:broken", consecutive_failures=4, last_error="HTTP <500>")
    )
    reply = await text("/sources", ctx)
    assert "lever:broken" in reply and "×4" in reply and "HTTP &lt;500&gt;" in reply


async def test_latest_today_history(ctx, repo):
    add_alerted_job(repo, "1", title="Newest Python Role")
    add_alerted_job(repo, "2", title="Yesterday Role", sent_at=NOW - timedelta(days=1))
    add_alerted_job(repo, "3", title="Old Role", sent_at=NOW - timedelta(days=20))
    latest = await text("/latest 2", ctx)
    assert latest.index("Newest Python Role") < latest.index("Yesterday Role")
    assert "Old Role" not in latest
    today = await text("/today", ctx)
    assert "Today: 1 alerts" in today and "Newest Python Role" in today
    week = await text("/history 7", ctx)
    assert "Last 7 days: 2 alerts" in week
    assert "Usage" in await text("/latest 999", ctx)


async def test_job_and_realert(ctx, repo):
    job_id = add_alerted_job(repo, "1")
    reply = await text(f"/job {job_id}", ctx)
    assert "<pre>" in reply and "role" in reply and "Apply ↗" in reply
    assert "No stored job" in await text("/job lever:acme:404", ctx)
    assert await handle(f"/realert {job_id}", ctx) == []  # the alert itself is the reply
    assert len(ctx.notifier.sent) == 1
    assert "Couldn't re-send" in await text("/realert lever:acme:404", ctx)


# --- control ---------------------------------------------------------------------------


async def test_pause_resume_and_scan_now(ctx, repo):
    assert "paused" in await text("/pause", ctx)
    assert repo.get_preferences().data["paused"] is True
    assert "Alerts: ⏸" in await text("/status", ctx)
    assert "resumed" in await text("/resume", ctx)
    assert repo.get_preferences().data["paused"] is False
    reply = await text("/scan_now", ctx)
    assert "Full scan started" in reply and ctx.dispatched == [True]
    await text("/rematch", ctx)
    assert ctx.dispatched == [True, True]


# --- preferences -------------------------------------------------------------------------


async def test_prefs_shows_defaults_then_versions(ctx, repo):
    assert "Preferences v0" in await text("/prefs", ctx)
    reply = await text("/set_salary 14 11", ctx)
    assert "Target base ₹14 LPA, floor ₹11 LPA" in reply and "Saved as v1" in reply
    data = repo.get_preferences().data
    assert (data["target_base_lpa"], data["floor_lpa"]) == (14, 11)
    assert "Preferences v1" in await text("/prefs", ctx)


@pytest.mark.parametrize(
    "command", ["/set_salary", "/set_salary abc", "/set_salary 10 12", "/set_salary 500"]
)
async def test_set_salary_validation(ctx, command):
    assert "Usage" in await text(command, ctx) or "Need" in await text(command, ctx)


async def test_set_exp_min_score_remote(ctx, repo):
    await text("/set_exp 2", ctx)
    await text("/set_min_score 60", ctx)
    await text("/set_remote off", ctx)
    data = repo.get_preferences().data
    assert (data["max_min_years"], data["min_score"], data["allow_remote"]) == (2, 60, False)
    assert "Usage" in await text("/set_min_score 101", ctx)
    assert "Usage" in await text("/set_remote maybe", ctx)


async def test_location_add_remove_with_aliases(ctx, repo):
    assert "pune" in await text("/location add Pune", ctx)
    assert "already" in await text("/location add pune", ctx)
    assert "mumbai" in (await text("/location add Bombay", ctx))  # alias -> canonical
    assert "I don't know the place" in await text("/location add Atlantis", ctx)
    removed = await text("/location remove ncr", ctx)
    remaining = removed.split("Locations:")[1].split("\n")[0]
    assert "ncr" not in remaining and "bengaluru" in remaining
    locations = repo.get_preferences().data["locations"]
    assert "pune" in locations and "mumbai" in locations and "ncr" not in locations


async def test_skill_and_role(ctx, repo):
    assert "added" in await text("/skill add Redis", ctx) or "already" in await text(
        "/skill add Redis", ctx
    )
    assert "added" in await text("/skill add graphql", ctx)
    assert "graphql" in repo.get_preferences().data["secondary_skills"]
    assert "removed" in await text("/skill remove GraphQL", ctx)
    assert "Unknown skill" in await text("/skill add cobolscript", ctx)
    assert "platform engineer" in await text("/role add Platform Engineer", ctx)
    assert "isn't a preferred title" in await text("/role remove astronaut", ctx)


async def test_set_interval(ctx, repo):
    repo.save_source_state(SourceState(key="greenhouse:stripe"))
    assert "every 180 minutes" in await text("/set_interval stripe 180", ctx)
    assert repo.get_source_state("greenhouse:stripe").interval_minutes == 180
    assert "between 30 and 1440" in await text("/set_interval stripe 5", ctx)
    assert "Unknown source" in await text("/set_interval nope 60", ctx)


async def test_concurrent_preference_edit_is_reported(ctx, repo, monkeypatch):
    from jobbot.storage import StaleVersionError

    def stale(*args, **kwargs):
        raise StaleVersionError("raced")

    monkeypatch.setattr(repo, "save_preferences", stale)
    assert "send the command again" in await text("/set_min_score 60", ctx)
