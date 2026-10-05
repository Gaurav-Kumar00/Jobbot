"""End-to-end scan behaviour with an in-memory repository and fake source/notifier."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobbot.http import FetchError, HttpClient
from jobbot.matching.preferences import default_preferences, load_profile
from jobbot.models import AlertStatus, CompanyTarget
from jobbot.notify.base import OutgoingMessage
from jobbot.notify.telegram import TelegramError
from jobbot.pipeline.scan import run_scan
from jobbot.sources.base import FetchResult, build_job
from jobbot.storage import MemoryRepository
from jobbot.timeutil import utcnow

GOOD = "0-2 years. Python, Django, FastAPI, Kafka, PostgreSQL, AWS. You will build REST APIs."


RECENT = "recent"


def posting(
    native_id: str,
    title: str = "Backend Engineer",
    loc: str = "Bengaluru",
    desc=GOOD,
    posted=RECENT,
):
    return {"id": native_id, "title": title, "loc": loc, "desc": desc, "posted": posted}


class FakeSource:
    name = "fake"

    def __init__(self) -> None:
        self.boards: dict[str, list[dict] | Exception] = {}

    async def fetch(self, target: CompanyTarget, http) -> FetchResult:
        board = self.boards[target.slug]
        if isinstance(board, Exception):
            raise board
        now = utcnow()
        return FetchResult(
            jobs=[
                build_job(
                    source=target.ats,
                    target=target,
                    native_id=p["id"],
                    title=p["title"],
                    url=f"https://jobs.example/{target.slug}/{p['id']}",
                    location_raw=p["loc"],
                    description=p["desc"],
                    posted_at=now - timedelta(hours=1) if p["posted"] == RECENT else p["posted"],
                    raw={},
                    now=now,
                )
                for p in board
            ]
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.fail_with: list[Exception] = []  # raised (once each) before delivering

    async def send(self, message: OutgoingMessage) -> str:
        if self.fail_with:
            raise self.fail_with.pop(0)
        self.sent.append(message)
        return str(len(self.sent))


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def source(monkeypatch) -> FakeSource:
    fake = FakeSource()
    monkeypatch.setattr("jobbot.pipeline.collect.get_source", lambda name: fake)
    return fake


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def target(slug: str) -> CompanyTarget:
    return CompanyTarget(key=slug, name=slug.title(), ats="fake", slug=slug)


async def scan(repo, notifier, slugs=("acme",), prefs=None, version=1, force=True):
    async with HttpClient() as http:
        return await run_scan(
            repo=repo,
            http=http,
            notifier=notifier,
            targets=[target(s) for s in slugs],
            prefs=prefs or default_preferences(),
            prefs_version=version,
            profile=load_profile(),
            force=force,
            sleep=no_sleep,
        )


# --- the core promise -----------------------------------------------------------------


async def test_matching_jobs_are_alerted_once_and_restart_sends_nothing(source, repo, notifier):
    source.boards["acme"] = [
        posting("1"),
        posting("2", title="Software Engineer I", loc="Gurugram"),
    ]
    first = await scan(repo, notifier)
    assert first.alerts_sent == 2
    assert len(notifier.sent) == 2

    second = await scan(repo, notifier)  # e.g. the next hourly run, or a restart
    assert second.alerts_sent == 0
    assert len(notifier.sent) == 2
    assert repo.get_alert("fake:acme:1").status is AlertStatus.SENT


async def test_rejected_jobs_are_stored_but_not_alerted(source, repo, notifier):
    source.boards["acme"] = [
        posting("1", title="Senior Backend Engineer"),
        posting("2", loc="Pune"),
    ]
    report = await scan(repo, notifier)
    assert report.alerts_sent == 0
    assert repo.get_match("fake:acme:1").reject_reason == "senior-level title"
    assert repo.get_job("fake:acme:2") is not None


async def test_foreign_jobs_are_not_stored(source, repo, notifier):
    source.boards["acme"] = [posting("1", loc="San Francisco, CA"), posting("2")]
    report = await scan(repo, notifier)
    assert report.skipped_foreign == 1
    assert repo.get_job("fake:acme:1") is None
    assert repo.get_job("fake:acme:2") is not None


# --- failures -------------------------------------------------------------------------


async def test_one_failing_source_does_not_stop_the_others(source, repo, notifier):
    source.boards["broken"] = FetchError("https://x", "HTTP 503", 503)
    source.boards["acme"] = [posting("1")]
    report = await scan(repo, notifier, slugs=("broken", "acme"))
    assert report.sources_ok == 1
    assert report.sources_failed == ["fake:broken: HTTP 503 for x"]
    assert report.alerts_sent == 1


async def test_a_crashing_adapter_is_isolated(source, repo, notifier):
    source.boards["buggy"] = RuntimeError("parser bug")
    source.boards["acme"] = [posting("1")]
    report = await scan(repo, notifier, slugs=("buggy", "acme"))
    assert "crashed" in report.sources_failed[0]
    assert report.alerts_sent == 1


async def test_crash_after_send_never_resends(source, repo, notifier, monkeypatch):
    source.boards["acme"] = [posting("1")]

    def db_down(*args, **kwargs):
        raise ConnectionError("db went away")

    monkeypatch.setattr(repo, "mark_alert_sent", db_down)
    with pytest.raises(ConnectionError):
        await scan(repo, notifier)
    assert len(notifier.sent) == 1  # it did reach Telegram
    monkeypatch.undo()

    report = await scan(repo, notifier)
    assert report.alerts_sent == 0
    assert len(notifier.sent) == 1
    assert repo.get_alert("fake:acme:1").status is AlertStatus.SENDING


async def test_definitive_send_failure_is_retried_next_run(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    notifier.fail_with = [TelegramError("sendMessage", "Bad Request", 400)]
    first = await scan(repo, notifier)
    assert first.alerts_failed == 1
    assert repo.get_alert("fake:acme:1").status is AlertStatus.FAILED

    second = await scan(repo, notifier)
    assert second.alerts_sent == 1
    assert repo.get_alert("fake:acme:1").attempts == 2


async def test_possibly_delivered_failure_is_never_retried(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    notifier.fail_with = [
        TelegramError("sendMessage", "network error: ReadTimeout", maybe_delivered=True)
    ]
    first = await scan(repo, notifier)
    assert first.alerts_uncertain == 1
    second = await scan(repo, notifier)
    assert second.alerts_sent == 0
    assert notifier.sent == []


# --- duplicates -----------------------------------------------------------------------


async def test_same_opening_posted_twice_alerts_once(source, repo, notifier):
    source.boards["acme"] = [posting("1"), posting("2")]  # identical title + city
    report = await scan(repo, notifier)
    assert report.alerts_sent == 1
    assert report.duplicates == 1
    assert repo.get_job("fake:acme:2").duplicate_of == "fake:acme:1"


async def test_reposted_opening_in_a_later_run_is_not_realerted(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    source.boards["acme"] = [posting("1"), posting("99")]  # re-posted under a new id
    report = await scan(repo, notifier)
    assert report.alerts_sent == 0
    assert repo.get_job("fake:acme:99").duplicate_of == "fake:acme:1"


async def test_same_opening_on_two_companies_boards_is_two_openings(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    source.boards["globex"] = [posting("1")]
    report = await scan(repo, notifier, slugs=("acme", "globex"))
    assert report.alerts_sent == 2


# --- lifecycle --------------------------------------------------------------------------


async def test_edited_posting_is_rescored_but_not_realerted(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    source.boards["acme"] = [posting("1", desc=GOOD + " Now with Celery.")]
    report = await scan(repo, notifier)
    assert report.changed == 1 and report.scored == 1
    assert report.alerts_sent == 0


async def test_closed_postings_become_inactive_and_are_never_alerted(source, repo, notifier):
    source.boards["acme"] = [posting("1"), posting("2", title="Senior Backend Engineer")]
    await scan(repo, notifier)
    source.boards["acme"] = [posting("1")]
    report = await scan(repo, notifier)
    assert report.closed == 1
    assert repo.get_job("fake:acme:2").is_active is False


async def test_failed_fetch_does_not_close_postings(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    source.boards["acme"] = FetchError("https://x", "HTTP 500", 500)
    report = await scan(repo, notifier)
    assert report.closed == 0
    assert repo.get_job("fake:acme:1").is_active is True


async def test_paused_alerts_are_held_then_sent_on_resume(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    paused = default_preferences().model_copy(update={"paused": True})
    held = await scan(repo, notifier, prefs=paused)
    assert held.held_paused == 1 and notifier.sent == []

    resumed = await scan(repo, notifier)
    assert resumed.alerts_sent == 1


async def test_preference_change_rescores_without_realerting(source, repo, notifier):
    source.boards["acme"] = [posting("1"), posting("2", title="Backend Engineer", loc="Pune")]
    await scan(repo, notifier, version=1)
    assert len(notifier.sent) == 1

    with_pune = default_preferences().model_copy(update={"locations": ["bengaluru", "pune"]})
    report = await scan(repo, notifier, prefs=with_pune, version=2)
    assert report.scored == 2  # everything re-evaluated under v2, no re-scrape needed
    assert report.alerts_sent == 1  # only the newly qualifying Pune job
    assert repo.get_match("fake:acme:1").prefs_version == 2


async def test_no_notifier_keeps_alerts_pending(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    report = await scan(repo, None)
    assert report.held_paused == 1
    assert repo.get_alert("fake:acme:1") is None
    later = await scan(repo, notifier)
    assert later.alerts_sent == 1


async def test_each_run_is_recorded(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    (run,) = repo.latest_runs()
    assert run.stats["alerts_sent"] == 1
    assert run.stats["fetched"] == 1
    assert run.finished_at is not None


async def test_alert_message_content(source, repo, notifier):
    source.boards["acme"] = [posting("1", title="SDE 1 - Backend", loc="Bengaluru (Hybrid)")]
    await scan(repo, notifier)
    (message,) = notifier.sent
    assert "SDE 1 - Backend" in message.text
    assert "Acme" in message.text
    assert message.buttons[0].url == "https://jobs.example/acme/1"


async def test_adapter_returning_wrong_source_name_is_rejected(source, repo, notifier, monkeypatch):
    source.boards["acme"] = [posting("1")]
    original = source.fetch

    async def mislabelled(target, http):
        result = await original(target, http)
        result.jobs = [j.model_copy(update={"source": "other"}) for j in result.jobs]
        return result

    monkeypatch.setattr(source, "fetch", mislabelled)
    report = await scan(repo, notifier)
    assert "adapter bug" in report.sources_failed[0]


async def test_realert_resends_explicitly_requested_job(source, repo, notifier):
    from jobbot.pipeline.scan import realert

    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    await realert(repo, notifier, "fake:acme:1", default_preferences(), 1, load_profile())
    assert len(notifier.sent) == 2
    alert = repo.get_alert("fake:acme:1")
    assert alert.trigger.value == "realert" and alert.attempts == 2
    # an explicit re-alert does not make the next scan send it again
    assert (await scan(repo, notifier)).alerts_sent == 0


async def test_realert_unknown_job(repo, notifier):
    from jobbot.pipeline.scan import realert

    with pytest.raises(LookupError):
        await realert(repo, notifier, "fake:acme:404", default_preferences(), 1, load_profile())


# --- Phase 7: schedules, first-scan backfill, health, storage --------------------------


async def test_sources_run_on_their_interval(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    first = await scan(repo, notifier, force=False)
    assert first.sources_due == 1
    again = await scan(repo, notifier, force=False)  # minutes later: not due yet
    assert again.sources_due == 0 and again.fetched == 0

    state = repo.get_source_state("fake:acme")
    repo.save_source_state(
        state.model_copy(update={"last_run_at": utcnow() - timedelta(minutes=55)})
    )
    later = await scan(repo, notifier, force=False)  # cron drift: 55 min counts as an hour
    assert later.sources_due == 1


async def test_custom_interval_is_respected(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    state = repo.get_source_state("fake:acme")
    repo.save_source_state(
        state.model_copy(
            update={"interval_minutes": 360, "last_run_at": utcnow() - timedelta(hours=2)}
        )
    )
    assert (await scan(repo, notifier, force=False)).sources_due == 0


async def test_first_scan_alerts_recent_posts_and_digests_older_ones(source, repo, notifier):
    old = utcnow() - timedelta(days=5)
    source.boards["acme"] = [
        posting("new", title="Backend Engineer"),
        posting("old", title="Software Engineer", posted=old),
        posting("undated", title="SDE 1", posted=None),
    ]
    report = await scan(repo, notifier)
    assert report.alerts_sent == 1  # the recent one: a full alert
    assert report.baseline == 2 and report.digested == 2  # older ones: one digest message
    assert len(notifier.sent) == 2
    digest = notifier.sent[1].text
    assert "2 more matching roles, still open" in digest
    assert "Software Engineer" in digest and "SDE 1" in digest
    assert repo.get_alert("fake:acme:old").status is AlertStatus.SENT
    # nothing is ever repeated
    assert (await scan(repo, notifier)).digested == 0
    assert len(notifier.sent) == 2
    # once bootstrapped, a brand-new posting without a date is a normal full alert
    source.boards["acme"].append(posting("later", title="Python Developer", posted=None))
    later = await scan(repo, notifier)
    assert later.alerts_sent == 1 and later.digested == 0


async def test_paused_holds_the_digest_too(source, repo, notifier):
    source.boards["acme"] = [posting("old", posted=utcnow() - timedelta(days=9))]
    paused = default_preferences().model_copy(update={"paused": True})
    held = await scan(repo, notifier, prefs=paused)
    assert held.baseline == 1 and held.digested == 0 and notifier.sent == []
    resumed = await scan(repo, notifier)
    assert resumed.digested == 1


async def test_a_newly_added_company_gets_its_own_backfill(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    await scan(repo, notifier)
    source.boards["globex"] = [posting("9", posted=utcnow() - timedelta(days=10))]
    report = await scan(repo, notifier, slugs=("acme", "globex"))
    assert report.baseline == 1 and report.alerts_sent == 0 and report.digested == 1


async def test_repeated_failures_warn_once_then_report_recovery(source, repo, notifier):
    source.boards["acme"] = FetchError("https://x", "HTTP 503", 503)
    for _ in range(2):
        await scan(repo, notifier)
    assert notifier.sent == []  # 2 failures: no noise yet
    await scan(repo, notifier)
    assert len(notifier.sent) == 1
    assert "failed 3 scans in a row" in notifier.sent[0].text
    await scan(repo, notifier)
    assert len(notifier.sent) == 1  # no repeat warning

    source.boards["acme"] = [posting("1", title="Senior Backend Engineer")]
    await scan(repo, notifier)
    assert "working again" in notifier.sent[-1].text
    state = repo.get_source_state("fake:acme")
    assert state.consecutive_failures == 0 and state.last_success_at is not None


async def test_source_state_records_counts(source, repo, notifier):
    source.boards["acme"] = [posting("1"), posting("2", loc="London, UK")]
    await scan(repo, notifier)
    state = repo.get_source_state("fake:acme")
    assert state.bootstrapped and state.last_counts == {"fetched": 2, "skipped": 0}


async def test_registry_interval_applies(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    slow = target("acme").model_copy(update={"interval_minutes": 180})
    async with HttpClient() as http:
        common = dict(
            repo=repo, http=http, notifier=notifier, targets=[slow],
            prefs=default_preferences(), prefs_version=1, profile=load_profile(), sleep=no_sleep,
        )  # fmt: skip
        await run_scan(**common)
        state = repo.get_source_state("fake:acme")
        repo.save_source_state(
            state.model_copy(update={"last_run_at": utcnow() - timedelta(hours=2)})
        )
        assert (await run_scan(**common)).sources_due == 0  # 2h < 3h
        repo.save_source_state(
            state.model_copy(update={"last_run_at": utcnow() - timedelta(hours=3)})
        )
        assert (await run_scan(**common)).sources_due == 1


async def test_one_unparseable_posting_does_not_break_the_scan(source, repo, notifier, monkeypatch):
    import jobbot.pipeline.collect as collect_module

    real = collect_module.normalize_job

    def flaky(job):
        if job.id.endswith(":bad"):
            raise ValueError("normaliser bug")
        return real(job)

    monkeypatch.setattr(collect_module, "normalize_job", flaky)
    source.boards["acme"] = [posting("bad"), posting("good", title="SDE 1")]
    source.boards["globex"] = [posting("1")]
    report = await scan(repo, notifier, slugs=("acme", "globex"))
    assert report.sources_ok == 2 and report.sources_failed == []
    assert report.alerts_sent == 2
    assert repo.get_job("fake:acme:bad") is None


# --- Phase 10: optional AI check -----------------------------------------------------------


class FakeLLM:
    """Stands in for LLMChain: answers per job title."""

    def __init__(self, by_title: dict) -> None:
        self.by_title, self.calls, self.errors, self.available = by_title, [], [], True

    async def extract(self, http, job):
        self.calls.append(job.title)
        return self.by_title.get(job.title)


async def ai_scan(repo, notifier, llm, slugs=("acme",)):
    async with HttpClient() as http:
        return await run_scan(
            repo=repo, http=http, notifier=notifier, targets=[target(s) for s in slugs],
            prefs=default_preferences(), prefs_version=1, profile=load_profile(),
            force=True, sleep=no_sleep, llm=llm, llm_run_budget=10, llm_daily_budget=50,
        )  # fmt: skip


async def test_ai_check_can_veto_a_generic_title_and_adds_summaries(source, repo, notifier):
    from jobbot.models import AIInsight

    source.boards["acme"] = [
        posting("1", title="Software Engineer", desc="0-1 years. Build the React UI."),
        posting("2", title="Backend Engineer"),
    ]
    llm = FakeLLM(
        {
            "Software Engineer": AIInsight(model="t", role_type="frontend"),
            "Backend Engineer": AIInsight(
                model="t", role_type="backend", summary="Owns payment APIs."
            ),
        }
    )
    report = await ai_scan(repo, notifier, llm)
    assert sorted(llm.calls) == ["Backend Engineer", "Software Engineer"]
    assert report.llm_calls == 2 and report.llm_flipped == 1
    assert report.alerts_sent == 1
    (alert,) = notifier.sent
    assert "Backend Engineer" in alert.text and "Owns payment APIs." in alert.text
    assert "AI summary" in alert.text
    assert repo.get_match("fake:acme:1").reject_reason == "frontend role"

    again = await ai_scan(repo, notifier, llm)  # cached: no new calls, verdicts kept
    assert again.llm_calls == 0 and len(llm.calls) == 2
    assert repo.get_job("fake:acme:2").normalized.ai.summary == "Owns payment APIs."


async def test_scan_works_when_the_ai_answers_nothing(source, repo, notifier):
    source.boards["acme"] = [posting("1")]
    report = await ai_scan(repo, notifier, FakeLLM({}))
    assert report.llm_failed == 1 and report.alerts_sent == 1


async def test_crashing_ai_layer_never_breaks_the_scan(source, repo, notifier):
    class Broken(FakeLLM):
        async def extract(self, http, job):
            raise RuntimeError("bug in provider")

    source.boards["acme"] = [posting("1")]
    report = await ai_scan(repo, notifier, Broken({}))
    assert report.alerts_sent == 1
