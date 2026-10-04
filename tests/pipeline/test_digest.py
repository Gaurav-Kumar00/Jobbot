from __future__ import annotations

from datetime import UTC, datetime

from jobbot.models import AlertStatus, Job, MatchResult
from jobbot.normalize import normalize_job
from jobbot.notify.base import OutgoingMessage
from jobbot.pipeline.digest import MAX_CHARS, digest_messages, held_back, send_held_back
from jobbot.storage import MemoryRepository


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return str(len(self.sent))


def add(repo, native, *, title="Backend Engineer", city="Bengaluru", score=70, decision="match",
        active=True, company="Acme"):  # fmt: skip
    job = normalize_job(
        Job(
            id=f"greenhouse:{company.lower()}:{native}",
            source="greenhouse",
            company=company.lower(),
            company_name=company,
            title=title,
            url=f"https://jobs.example/{native}?a=1&b=<2>",
            location_raw=city,
            description="0-1 years. Python, Django.",
            first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    )
    repo.upsert_job(job)
    if not active:
        repo.mark_missing_inactive("greenhouse", company.lower(), [])
    repo.save_match(MatchResult(job_id=job.id, score=score, decision=decision, prefs_version=1))
    repo.suppress_alert(job.id, job.fingerprint)
    return job.id


def test_held_back_filters_and_dedupes():
    repo = MemoryRepository()
    best = add(repo, "1", score=90)
    add(repo, "2", score=90)  # same opening reposted -> collapsed
    add(repo, "3", title="SDE 1", city="Hyderabad", score=60)
    add(repo, "4", title="Python Developer", decision="reject")
    add(repo, "5", title="Django Developer", company="Closed", active=False)
    entries = held_back(repo)
    assert [job.id for job, _ in entries] == [best, "greenhouse:acme:3"]
    assert repo.get_job("greenhouse:acme:2").duplicate_of == best


async def test_send_marks_entries_sent_and_escapes_html():
    repo = MemoryRepository()
    job_id = add(repo, "1", title="Backend <Engineer> & APIs")
    notifier = Recorder()
    assert await send_held_back(repo, notifier) == 1
    text = notifier.sent[0].text
    assert "Backend &lt;Engineer&gt; &amp; APIs" in text
    assert 'href="https://jobs.example/1?a=1&amp;b=&lt;2&gt;"' in text
    assert "Acme · Bengaluru · 0–1 yrs" in text
    assert repo.get_alert(job_id).status is AlertStatus.SENT
    assert await send_held_back(repo, notifier) == 0  # never repeated


def test_long_digests_are_split_under_telegram_limit():
    repo = MemoryRepository()
    for i in range(120):
        add(repo, str(i), title=f"Backend Engineer {i:03d} " + "x" * 40, score=60 + i % 30)
    chunks = digest_messages(held_back(repo))
    assert len(chunks) > 1
    assert all(len(text) <= MAX_CHARS + 300 for text, _ in chunks)
    assert sum(len(ids) for _, ids in chunks) == 120
    first_scores = [
        int(line.split("<b>")[1].split("</b>")[0])
        for line in chunks[0][0].split("\n")
        if line.startswith(("🔥", "✅", "🟡"))
    ]
    assert first_scores == sorted(first_scores, reverse=True)  # best first
