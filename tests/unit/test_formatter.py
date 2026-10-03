from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobbot.matching.preferences import default_preferences, load_profile
from jobbot.matching.scorer import match_job
from jobbot.models import Job
from jobbot.normalize import normalize_job
from jobbot.normalize.summary import summarize
from jobbot.notify.formatter import format_alert
from jobbot.notify.telegram import MAX_MESSAGE_LEN

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)  # 11:30 IST


def make(**fields) -> tuple[Job, object]:
    base = {
        "id": "greenhouse:razorpay:42",
        "source": "greenhouse",
        "company": "razorpay",
        "company_name": "Razorpay",
        "title": "SDE 1 - Backend",
        "url": "https://boards.greenhouse.io/razorpay/jobs/42",
        "location_raw": "Bengaluru (Hybrid)",
        "description": (
            "About Razorpay\nWe power payments for millions of businesses.\n"
            "What you'll do\n• Build high-throughput payment APIs in Python and Django\n"
            "• Own Kafka consumers end to end\n• Mentor nobody\n"
            "Requirements\n0-1 years of experience. Base salary: ₹16-20 LPA"
        ),
        "posted_at": NOW - timedelta(hours=3),
        "first_seen_at": NOW - timedelta(minutes=10),
    }
    job = normalize_job(Job(**{**base, **fields}))
    return job, match_job(job, default_preferences(), load_profile(), 1)


def test_alert_contains_every_required_field():
    job, match = make()
    text = format_alert(job, match, now=NOW).text
    for expected in [
        f"{match.score}/100",  # match score
        "SDE 1 - Backend",  # role
        "<b>Razorpay</b>",  # company
        "<b>Location:</b> <b>Bengaluru</b>",  # location, value in bold
        "<b>Work mode:</b> <b>Hybrid</b>",
        "<b>Experience:</b> <b>0–1 yrs</b> <i>(fresher-friendly)</i>",
        "<b>Salary:</b> <b>Base ₹16–20 LPA</b>",  # base vs CTC
        "<b>Posted:</b> <b>3h ago</b>",  # posted time
        "found 03 Oct, 11:20 IST",  # discovered time
        "✅ <b>Why it matches</b>\n• ",  # reasons as bullets
        "📝 <b>About the role</b>\nBuild high-throughput payment APIs",  # JD summary
        "Greenhouse",  # source
        "<code>greenhouse:razorpay:42</code>",  # id for /job and /realert
    ]:
        assert expected in text, expected


def test_sections_are_separated_by_blank_lines():
    job, match = make()
    text = format_alert(job, match, now=NOW).text
    sections = text.split("\n\n")
    assert sections[0].startswith(("🔥", "✅", "🟡"))
    assert sections[1].startswith("📍")
    assert sections[2].startswith("✅ <b>Why it matches</b>")
    assert sections[3].startswith("📝")
    assert sections[4].startswith("🔗")
    why_bullets = [line for line in sections[2].split("\n") if line.startswith("• ")]
    assert len(why_bullets) == len(match.reasons[:5])


def test_apply_button_points_to_official_url():
    job, match = make()
    message = format_alert(job, match, now=NOW)
    assert message.buttons[0].text == "Apply ↗"
    assert message.buttons[0].url == "https://boards.greenhouse.io/razorpay/jobs/42"


def test_html_is_escaped():
    job, match = make(title="Backend <Engineer> & APIs", company_name="A&B <Labs>")
    text = format_alert(job, match, now=NOW).text
    assert "Backend &lt;Engineer&gt; &amp; APIs" in text
    assert "A&amp;B &lt;Labs&gt;" in text
    assert "<Engineer>" not in text


def test_missing_fields_are_italic_placeholders():
    job, match = make(location_raw="", description="", posted_at=None)
    text = format_alert(job, match, now=NOW).text
    assert "<b>Location:</b> <i>Not stated</i>" in text
    assert "<b>Work mode:</b> <i>Not stated</i>" in text
    assert "<b>Experience:</b> <i>Not stated</i>" in text
    assert "<b>Salary:</b> <i>Not disclosed</i>" in text
    assert "<b>Posted:</b> <i>Not stated</i> · found 03 Oct" in text
    assert "📝" not in text


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        ("CTC 14 LPA", "<b>CTC ₹14 LPA</b> <i>(base not stated)</i>"),
        ("Salary: 12-15 LPA", "<b>₹12–15 LPA</b> <i>(base vs CTC not stated)</i>"),
        ("$120,000 - $150,000", "<b>USD 120,000–150,000/yr</b>"),
    ],
)
def test_salary_labels(desc, expected):
    job, match = make(description=desc)
    assert expected in format_alert(job, match, now=NOW).text


def test_remote_india_is_labelled():
    job, match = make(location_raw="Remote - India")
    text = format_alert(job, match, now=NOW).text
    assert "<b>Location:</b> <b>Remote - India</b> <i>(remote, open to India)</i>" in text
    assert "<b>Work mode:</b> <b>Remote</b>" in text


def test_message_never_exceeds_telegram_limit():
    job, match = make(title="Backend Engineer " * 400)
    assert len(format_alert(job, match, now=NOW).text) <= MAX_MESSAGE_LEN


@pytest.mark.parametrize(
    ("description", "expected_start"),
    [
        (
            "About us\nWe are a fintech.\nResponsibilities\n• Design APIs\n• Run Kafka\nPerks",
            "Design APIs Run Kafka",
        ),
        (
            "Acme builds rockets.\nAs a Backend Engineer you will own our order service.\n"
            "More text.",
            "As a Backend Engineer you will own our order service.",
        ),
        ("Just one line about the job.", "Just one line about the job."),
        ("", ""),
    ],
)
def test_summary(description, expected_start):
    assert summarize(description).startswith(expected_start)


def test_summary_is_bounded():
    assert len(summarize("You will " + "build things " * 200)) <= 242
