from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobbot.models import Job
from jobbot.normalize import normalize_job
from jobbot.normalize.dedup import choose_canonical, normalize_company, normalize_title

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Razorpay Software Private Limited", "razorpay"),
        ("Razorpay", "razorpay"),
        ("RAZORPAY SOFTWARE PVT. LTD.", "razorpay"),
        ("Groww", "groww"),
        ("Atlassian Pty Ltd", "atlassianpty"),
        ("PhonePe Private Ltd", "phonepe"),
        ("Zepto (KiranaKart Technologies)", "zeptokiranakart"),
        ("Amazon Development Centre India", "amazondevelopmentcentre"),
        ("Tech", "tech"),  # never collapse to an empty string
    ],
)
def test_normalize_company(name, expected):
    assert normalize_company(name) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Backend Engineer", "Back-end Engineer"),
        ("Backend Engineer", "Backend Developer"),
        ("SDE-1", "Software Development Engineer I"),
        ("SDE 1", "SWE 1"),
        ("Sr. Backend Engineer", "Senior Backend Engineer"),
        ("Backend Engineer - Bangalore", "Backend Engineer"),
        ("Backend Engineer (Remote)", "Backend Engineer"),
        ("Full Stack Developer", "Full-Stack Engineer"),
    ],
)
def test_equivalent_titles_normalise_equally(a, b):
    assert normalize_title(a) == normalize_title(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Software Engineer I", "Software Engineer II"),
        ("Backend Engineer", "Frontend Engineer"),
        ("Backend Engineer", "Senior Backend Engineer"),
    ],
)
def test_different_titles_stay_different(a, b):
    assert normalize_title(a) != normalize_title(b)


def job(
    source: str, native: str, *, company_name: str, title: str, location: str, minutes=0
) -> Job:
    return Job(
        id=f"{source}:x:{native}",
        source=source,
        company="x",
        company_name=company_name,
        title=title,
        url=f"https://{source}.example/{native}",
        location_raw=location,
        first_seen_at=T0 + timedelta(minutes=minutes),
    )


def test_same_job_on_ats_and_aggregator_shares_fingerprint():
    ats = normalize_job(
        job(
            "greenhouse",
            "1",
            company_name="Razorpay",
            title="Backend Engineer",
            location="Bengaluru",
        )
    )
    aggregator = normalize_job(
        job(
            "adzuna",
            "a9",
            company_name="Razorpay Software Private Limited",
            title="Back-end Developer - Bangalore",
            location="Bangalore, Karnataka",
        )
    )
    assert ats.fingerprint == aggregator.fingerprint == "razorpay|backend engineer|bengaluru"


def test_different_city_is_a_different_opening():
    blr = normalize_job(
        job("greenhouse", "1", company_name="Acme", title="SDE 1", location="Bengaluru")
    )
    hyd = normalize_job(
        job("greenhouse", "2", company_name="Acme", title="SDE 1", location="Hyderabad")
    )
    assert blr.fingerprint != hyd.fingerprint


def test_remote_and_unknown_places():
    remote = normalize_job(job("lever", "1", company_name="Acme", title="SDE 1", location="Remote"))
    unknown = normalize_job(job("lever", "2", company_name="Acme", title="SDE 1", location=""))
    assert remote.fingerprint.endswith("|remote")
    assert unknown.fingerprint.endswith("|any")


def test_official_source_is_canonical_even_if_seen_later():
    aggregator = job("adzuna", "a", company_name="A", title="t", location="", minutes=0)
    official = job("greenhouse", "g", company_name="A", title="t", location="", minutes=30)
    hn = job("hn", "h", company_name="A", title="t", location="", minutes=-60)
    assert choose_canonical([aggregator, official, hn]).id == official.id


def test_earliest_copy_wins_among_equally_authoritative_sources():
    first = job("greenhouse", "1", company_name="A", title="t", location="", minutes=0)
    later = job("lever", "2", company_name="A", title="t", location="", minutes=10)
    assert choose_canonical([later, first]).id == first.id
