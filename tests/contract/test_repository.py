"""Behaviour every Repository backend must share."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobbot.models import (
    AlertStatus,
    AlertTrigger,
    ExperienceInfo,
    Job,
    LocationInfo,
    MatchResult,
    Normalized,
    RunSummary,
    SourceState,
    UpsertOutcome,
)
from jobbot.storage import StaleVersionError

# Whole-millisecond timestamps: MongoDB stores datetimes at millisecond precision.
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def make_job(native_id: str = "1", *, company: str = "razorpay", **overrides) -> Job:
    fields = {
        "id": f"greenhouse:{company}:{native_id}",
        "source": "greenhouse",
        "company": company,
        "title": "Backend Engineer",
        "url": f"https://boards.greenhouse.io/{company}/jobs/{native_id}",
        "location_raw": "Bengaluru, India",
        "description": "Python, Django, Kafka",
        "posted_at": at(-60),
        "first_seen_at": at(0),
        "last_seen_at": at(0),
        "content_hash": "h1",
        "fingerprint": f"{company}|backend engineer|bengaluru",
        "normalized": Normalized(
            version=1,
            location=LocationInfo(cities=["bengaluru"], countries=["IN"]),
            experience=ExperienceInfo(min_years=0, max_years=2),
            skills=["python", "django"],
        ),
        "hints": {"workplace": "hybrid"},
        "raw": {"id": native_id, "nested": {"k": [1, 2]}},
    }
    fields.update(overrides)
    return Job(**fields)


# --- lifecycle ---


def test_ensure_indexes_is_idempotent(repo):
    repo.ensure_indexes()
    repo.ensure_indexes()
    repo.ping()
    assert repo.storage_bytes() >= 0


# --- jobs ---


def test_new_job_roundtrip(repo):
    job = make_job()
    assert repo.upsert_job(job) is UpsertOutcome.NEW
    stored = repo.get_job(job.id)
    assert stored == job
    assert stored.posted_at.tzinfo is not None


def test_get_missing_job_returns_none(repo):
    assert repo.get_job("greenhouse:nope:0") is None


def test_unchanged_job_refreshes_last_seen_but_keeps_first_seen(repo):
    repo.upsert_job(make_job())
    outcome = repo.upsert_job(make_job(first_seen_at=at(60), last_seen_at=at(60)))
    stored = repo.get_job("greenhouse:razorpay:1")
    assert outcome is UpsertOutcome.UNCHANGED
    assert stored.first_seen_at == at(0)
    assert stored.last_seen_at == at(60)


def test_changed_content_is_replaced_and_reported(repo):
    repo.upsert_job(make_job())
    outcome = repo.upsert_job(
        make_job(title="Software Engineer I", content_hash="h2", last_seen_at=at(60))
    )
    stored = repo.get_job("greenhouse:razorpay:1")
    assert outcome is UpsertOutcome.CHANGED
    assert stored.title == "Software Engineer I"
    assert stored.content_hash == "h2"
    assert stored.first_seen_at == at(0)


def test_reseen_inactive_job_becomes_active(repo):
    repo.upsert_job(make_job())
    repo.mark_missing_inactive("greenhouse", "razorpay", seen_ids=[])
    assert repo.get_job("greenhouse:razorpay:1").is_active is False
    repo.upsert_job(make_job(last_seen_at=at(120)))
    assert repo.get_job("greenhouse:razorpay:1").is_active is True


def test_mark_missing_inactive_is_scoped_to_source_and_company(repo):
    repo.upsert_job(make_job("1"))
    repo.upsert_job(make_job("2"))
    repo.upsert_job(make_job("9", company="groww", id="greenhouse:groww:9"))
    changed = repo.mark_missing_inactive(
        "greenhouse", "razorpay", seen_ids=["greenhouse:razorpay:1"]
    )
    assert changed == 1
    assert repo.get_job("greenhouse:razorpay:1").is_active
    assert not repo.get_job("greenhouse:razorpay:2").is_active
    assert repo.get_job("greenhouse:groww:9").is_active


def test_find_by_fingerprint_respects_window_and_exclusion(repo):
    repo.upsert_job(make_job("1", first_seen_at=at(0)))
    repo.upsert_job(make_job("2", first_seen_at=at(30)))
    repo.upsert_job(make_job("3", first_seen_at=at(-60 * 24 * 60)))  # 60 days earlier
    repo.upsert_job(make_job("4", fingerprint="other"))

    found = repo.find_by_fingerprint(
        "razorpay|backend engineer|bengaluru", since=at(-60), exclude_id="greenhouse:razorpay:2"
    )
    assert [j.id for j in found] == ["greenhouse:razorpay:1"]


def test_set_duplicate_of(repo):
    repo.upsert_job(make_job("1"))
    repo.set_duplicate_of("greenhouse:razorpay:1", "lever:razorpay:x")
    assert repo.get_job("greenhouse:razorpay:1").duplicate_of == "lever:razorpay:x"


def test_list_jobs_filters_orders_and_limits(repo):
    for i in range(5):
        repo.upsert_job(make_job(str(i), first_seen_at=at(i)))
    repo.mark_missing_inactive(
        "greenhouse", "razorpay", seen_ids=[f"greenhouse:razorpay:{i}" for i in range(4)]
    )

    active = repo.list_jobs()
    assert [j.id[-1] for j in active] == ["3", "2", "1", "0"]  # newest first, #4 inactive
    assert len(repo.list_jobs(active_only=False)) == 5
    assert [j.id[-1] for j in repo.list_jobs(since=at(2))] == ["3", "2"]
    assert len(repo.list_jobs(limit=2)) == 2


def test_save_derived_updates_only_normalisation(repo):
    repo.upsert_job(make_job(normalized=None, fingerprint=""))
    derived = Normalized(version=2, role_category="backend", skills=["python"])
    repo.save_derived("greenhouse:razorpay:1", derived, "razorpay|backend engineer|bengaluru")
    stored = repo.get_job("greenhouse:razorpay:1")
    assert stored.normalized == derived
    assert stored.fingerprint == "razorpay|backend engineer|bengaluru"
    assert stored.first_seen_at == at(0) and stored.content_hash == "h1"


# --- matches ---


def test_matches_save_overwrite_and_filter(repo):
    repo.save_match(
        MatchResult(
            job_id="a", score=40, decision="reject", reject_reason="senior", prefs_version=1
        )
    )
    repo.save_match(
        MatchResult(job_id="b", score=70, decision="match", reasons=["python"], prefs_version=1)
    )
    repo.save_match(MatchResult(job_id="c", score=90, decision="match", prefs_version=1))
    repo.save_match(MatchResult(job_id="a", score=60, decision="match", prefs_version=2))  # rematch

    assert repo.get_match("a").prefs_version == 2
    assert repo.get_match("missing") is None
    assert [m.job_id for m in repo.list_matches(decision="match")] == ["c", "b", "a"]
    assert [m.job_id for m in repo.list_matches(min_score=65)] == ["c", "b"]
    assert len(repo.list_matches(limit=1)) == 1
    assert repo.get_match("b").reasons == ["python"]


# --- alerts: the no-duplicate guarantee ---


def test_claim_alert_only_once(repo):
    assert repo.claim_alert("j1", AlertTrigger.NEW, 80) is True
    assert repo.claim_alert("j1", AlertTrigger.NEW, 80) is False  # still sending
    repo.mark_alert_sent("j1", "123")
    assert repo.claim_alert("j1", AlertTrigger.REMATCH, 85) is False  # already sent

    alert = repo.get_alert("j1")
    assert alert.status is AlertStatus.SENT
    assert alert.message_id == "123"
    assert alert.sent_at is not None
    assert alert.trigger is AlertTrigger.NEW


def test_sending_is_never_reclaimed_even_with_force(repo):
    # A crash mid-send leaves SENDING; we prefer missing over duplicating.
    repo.claim_alert("j1", AlertTrigger.NEW, 80)
    assert repo.claim_alert("j1", AlertTrigger.REALERT, 80, force=True) is False


def test_failed_alert_can_be_retried(repo):
    repo.claim_alert("j1", AlertTrigger.NEW, 80)
    repo.mark_alert_failed("j1", "chat not found")
    assert repo.get_alert("j1").error == "chat not found"
    assert repo.claim_alert("j1", AlertTrigger.NEW, 80) is True
    alert = repo.get_alert("j1")
    assert alert.status is AlertStatus.SENDING
    assert alert.attempts == 2
    assert alert.error is None


def test_force_realert_over_sent(repo):
    repo.claim_alert("j1", AlertTrigger.NEW, 80)
    repo.mark_alert_sent("j1", "1")
    assert repo.claim_alert("j1", AlertTrigger.REALERT, 80, force=True) is True
    assert repo.get_alert("j1").trigger is AlertTrigger.REALERT


def test_list_alerts_by_status(repo):
    for job_id in ("a", "b", "c"):
        repo.claim_alert(job_id, AlertTrigger.NEW, 70)
    repo.mark_alert_sent("a", "1")
    repo.mark_alert_sent("b", "2")
    assert {a.job_id for a in repo.list_alerts(status=AlertStatus.SENT)} == {"a", "b"}
    assert {a.job_id for a in repo.list_alerts(status=AlertStatus.SENDING)} == {"c"}
    assert len(repo.list_alerts(limit=2)) == 2
    assert repo.list_alerts(since=datetime.now(UTC) + timedelta(hours=1)) == []
    assert repo.get_alert("zzz") is None


# --- source health ---


def test_source_state_roundtrip(repo):
    assert repo.get_source_state("greenhouse:razorpay") is None
    state = SourceState(
        key="greenhouse:razorpay",
        bootstrapped=True,
        last_success_at=at(0),
        consecutive_failures=2,
        last_error="HTTP 503",
        last_counts={"fetched": 21},
    )
    repo.save_source_state(state)
    repo.save_source_state(SourceState(key="adzuna"))
    assert repo.get_source_state("greenhouse:razorpay") == state
    assert [s.key for s in repo.list_source_states()] == ["adzuna", "greenhouse:razorpay"]


# --- runs ---


def test_runs_latest_first(repo):
    repo.record_run(RunSummary(started_at=at(0), stats={"alerts": 1}))
    repo.record_run(RunSummary(started_at=at(60), stats={"alerts": 3}, errors=["lever: timeout"]))
    runs = repo.latest_runs(limit=5)
    assert [r.stats["alerts"] for r in runs] == [3, 1]
    assert runs[0].errors == ["lever: timeout"]


# --- preferences ---


def test_preferences_versioning(repo):
    assert repo.get_preferences() is None
    v1 = repo.save_preferences({"min_score": 55}, expected_version=0, note="seed")
    assert v1.version == 1
    v2 = repo.save_preferences({"min_score": 60}, expected_version=1, note="/set_min_score")
    assert v2.version == 2
    current = repo.get_preferences()
    assert current.version == 2
    assert current.data == {"min_score": 60}


def test_preferences_stale_write_rejected(repo):
    repo.save_preferences({"a": 1}, expected_version=0)
    with pytest.raises(StaleVersionError):
        repo.save_preferences({"a": 2}, expected_version=0)
    repo.save_preferences({"a": 2}, expected_version=1)
    with pytest.raises(StaleVersionError):
        repo.save_preferences({"a": 3}, expected_version=1)
    assert repo.get_preferences().data == {"a": 2}


# --- batch operations (one round trip per batch in MongoDB) ---


def test_upsert_jobs_batch_reports_each_outcome(repo):
    repo.upsert_job(make_job("1"))
    repo.upsert_job(make_job("2"))
    outcomes = repo.upsert_jobs(
        [
            make_job("1", last_seen_at=at(60)),  # unchanged
            make_job("2", content_hash="h2", title="SDE 1", last_seen_at=at(60)),  # changed
            make_job("3", first_seen_at=at(60), last_seen_at=at(60)),  # new
        ]
    )
    assert outcomes == [UpsertOutcome.UNCHANGED, UpsertOutcome.CHANGED, UpsertOutcome.NEW]
    assert repo.get_job("greenhouse:razorpay:1").last_seen_at == at(60)
    assert repo.get_job("greenhouse:razorpay:2").title == "SDE 1"
    assert repo.get_job("greenhouse:razorpay:3") is not None
    assert repo.upsert_jobs([]) == []


def test_unchanged_upsert_refreshes_derived_fields(repo):
    repo.upsert_job(make_job("1"))
    newer = Normalized(version=99, role_category="backend")
    repo.upsert_jobs([make_job("1", normalized=newer, fingerprint="fp-v2")])
    stored = repo.get_job("greenhouse:razorpay:1")
    assert stored.normalized.version == 99
    assert stored.fingerprint == "fp-v2"


def test_batch_reads(repo):
    repo.upsert_jobs([make_job("1"), make_job("2")])
    repo.save_matches(
        [
            MatchResult(
                job_id="greenhouse:razorpay:1", score=80, decision="match", prefs_version=1
            ),
            MatchResult(
                job_id="greenhouse:razorpay:2", score=20, decision="reject", prefs_version=1
            ),
        ]
    )
    repo.claim_alert("greenhouse:razorpay:1", AlertTrigger.NEW, 80)
    ids = ["greenhouse:razorpay:1", "greenhouse:razorpay:2", "missing"]
    assert set(repo.get_jobs(ids)) == set(ids[:2])
    assert repo.get_matches(ids)["greenhouse:razorpay:2"].decision == "reject"
    assert set(repo.get_alerts(ids)) == {"greenhouse:razorpay:1"}
    repo.save_matches([])  # no-op


def test_alerted_fingerprints_only_counts_live_alerts(repo):
    repo.claim_alert("a", AlertTrigger.NEW, 80, fingerprint="acme|sde 1|bengaluru")
    repo.mark_alert_sent("a", "1")
    repo.claim_alert("b", AlertTrigger.NEW, 80, fingerprint="acme|sde 1|hyderabad")  # in flight
    repo.claim_alert("c", AlertTrigger.NEW, 80, fingerprint="acme|backend|pune")
    repo.mark_alert_failed("c", "boom")  # failed -> may be retried, not "alerted"
    found = repo.alerted_fingerprints(
        ["acme|sde 1|bengaluru", "acme|sde 1|hyderabad", "acme|backend|pune", "nope"]
    )
    assert found == {"acme|sde 1|bengaluru": "a", "acme|sde 1|hyderabad": "b"}
    assert repo.alerted_fingerprints([]) == {}
    assert repo.get_alert("a").fingerprint == "acme|sde 1|bengaluru"


# --- baseline alerts & housekeeping ---


def test_baseline_alert_blocks_sending_and_counts_as_handled(repo):
    assert repo.suppress_alert("j1", fingerprint="acme|sde 1|bengaluru") is True
    assert repo.get_alert("j1").status is AlertStatus.BASELINE
    assert repo.claim_alert("j1", AlertTrigger.NEW, 80) is False  # never sent by a scan
    assert repo.alerted_fingerprints(["acme|sde 1|bengaluru"]) == {"acme|sde 1|bengaluru": "j1"}
    # explicit /realert may still send it
    assert repo.claim_alert("j1", AlertTrigger.REALERT, 80, force=True) is False
    repo.mark_alert_sent("j2", "x")  # unrelated id: no-op


def test_suppress_never_overwrites_an_existing_alert(repo):
    repo.claim_alert("j1", AlertTrigger.NEW, 80)
    repo.mark_alert_sent("j1", "1")
    assert repo.suppress_alert("j1") is False
    assert repo.get_alert("j1").status is AlertStatus.SENT


def test_stale_compact_and_delete(repo):
    repo.upsert_jobs([make_job("1"), make_job("2"), make_job("3")])
    repo.mark_missing_inactive("greenhouse", "razorpay", seen_ids=["greenhouse:razorpay:3"])
    stale = repo.stale_job_ids(inactive_before=at(1))
    assert sorted(stale) == ["greenhouse:razorpay:1", "greenhouse:razorpay:2"]
    assert repo.stale_job_ids(inactive_before=at(-1)) == []  # last seen after cutoff

    assert repo.compact_jobs(["greenhouse:razorpay:1"]) == 1
    assert repo.compact_jobs(["greenhouse:razorpay:1"]) == 0  # idempotent
    compacted = repo.get_job("greenhouse:razorpay:1")
    assert compacted.compacted and compacted.description == "" and compacted.raw == {}
    assert compacted.fingerprint  # identity kept for dedup
    assert repo.stale_job_ids(inactive_before=at(1)) == ["greenhouse:razorpay:2"]
    assert len(repo.stale_job_ids(inactive_before=at(1), include_compacted=True)) == 2

    repo.save_match(
        MatchResult(job_id="greenhouse:razorpay:2", score=1, decision="reject", prefs_version=1)
    )
    assert repo.delete_jobs(["greenhouse:razorpay:2", "missing"]) == 1
    assert repo.get_job("greenhouse:razorpay:2") is None
    assert repo.get_match("greenhouse:razorpay:2") is None
    assert repo.compact_jobs([]) == 0 and repo.delete_jobs([]) == 0


def test_compacted_job_seen_again_is_restored(repo):
    repo.upsert_job(make_job("1"))
    repo.compact_jobs(["greenhouse:razorpay:1"])
    outcome = repo.upsert_jobs([make_job("1", last_seen_at=at(90))])[0]
    assert outcome is UpsertOutcome.CHANGED
    restored = repo.get_job("greenhouse:razorpay:1")
    assert restored.compacted is False
    assert restored.description == "Python, Django, Kafka"


def test_meta(repo):
    assert repo.get_meta("storage_warning") is None
    repo.set_meta("storage_warning", {"last_pct": 72})
    repo.set_meta("storage_warning", {"last_pct": 75})
    assert repo.get_meta("storage_warning") == {"last_pct": 75}


def test_insight_cache_roundtrip(repo):
    from jobbot.models import AIInsight

    assert repo.get_insights(["k1"]) == {}
    insight = AIInsight(
        model="m", min_years=0, max_years=1, role_type="backend", summary="Builds APIs."
    )
    repo.save_insight("k1", insight)
    repo.save_insight("k1", insight.model_copy(update={"summary": "Updated."}))
    got = repo.get_insights(["k1", "missing"])
    assert list(got) == ["k1"] and got["k1"].summary == "Updated."
    assert got["k1"].role_type == "backend"
