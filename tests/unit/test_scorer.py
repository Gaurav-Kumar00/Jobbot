"""Matching rules from the spec, one test each."""

from __future__ import annotations

import pytest

from jobbot.matching.explain import explain_text
from jobbot.matching.preferences import Preferences, default_preferences, load_profile
from jobbot.matching.scorer import match_job, tier
from jobbot.models import Job
from jobbot.normalize import normalize_job

PROFILE = load_profile()
GOOD_STACK = "Python, Django, FastAPI, Kafka, Celery, PostgreSQL, AWS, Docker, REST APIs"


@pytest.fixture
def prefs() -> Preferences:
    return default_preferences()


def job(
    title: str = "Backend Engineer",
    location: str = "Bengaluru",
    description: str = f"0-2 years of experience. {GOOD_STACK}.",
    **fields,
) -> Job:
    return normalize_job(
        Job(
            id=f"greenhouse:acme:{abs(hash((title, location, description))) % 10**8}",
            source="greenhouse",
            company="acme",
            company_name="Acme",
            title=title,
            url="https://acme.example/jobs/1",
            location_raw=location,
            description=description,
            **fields,
        )
    )


def score(j: Job, prefs: Preferences, version: int = 1):
    return match_job(j, prefs, PROFILE, version)


# --- the ideal job -------------------------------------------------------------------


def test_ideal_fresher_python_backend_job_scores_top_tier(prefs):
    m = score(
        job(
            "SDE 1 - Backend",
            "Bengaluru (Hybrid)",
            f"2026 batch, 0-1 years. {GOOD_STACK}. Compensation: ₹16-20 LPA base",
        ),
        prefs,
    )
    assert m.decision == "match"
    assert m.score >= 90
    assert tier(m.score) == "🔥"
    assert m.reject_reason is None
    assert sum(m.breakdown.values()) in range(m.score - 3, m.score + 4)  # rounding only


# --- experience ----------------------------------------------------------------------


def test_fresher_ranks_above_one_to_two_years_when_otherwise_equal(prefs):
    fresher = score(job(description=f"Freshers / 0-1 years. {GOOD_STACK}"), prefs)
    one_two = score(job(description=f"1-2 years of experience. {GOOD_STACK}"), prefs)
    assert fresher.score > one_two.score
    assert one_two.decision == "match"  # 1-2 YOE is not auto-rejected


@pytest.mark.parametrize("years", ["3+ years", "3-5 years", "minimum 4 years"])
def test_significantly_more_experience_is_excluded(prefs, years):
    m = score(job(description=f"{years} of experience. {GOOD_STACK}"), prefs)
    assert m.decision == "reject"
    assert "years of experience" in m.reject_reason


@pytest.mark.parametrize("years", ["2+ years", "2-4 years", "minimum 2 years"])
def test_two_year_minimum_is_excluded(prefs, years):
    m = score(job(description=f"{years} of experience. {GOOD_STACK}"), prefs)
    assert m.decision == "reject"


@pytest.mark.parametrize("years", ["1-2 years", "1-3 years", "1+ years"])
def test_one_year_minimum_is_allowed(prefs, years):
    assert score(job(description=f"{years} of experience. {GOOD_STACK}"), prefs).decision == "match"


def test_two_year_minimum_allowed_when_preference_widened(prefs):
    loose = prefs.model_copy(update={"max_min_years": 2})
    m = score(job(description=f"2+ years of experience. {GOOD_STACK}"), loose)
    assert m.decision == "match"
    assert m.breakdown["experience"] <= 6


# --- role ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Backend Engineer",
        "Staff Software Engineer",
        "Lead Backend Engineer",
        "SDE III",
        "SDE II",
        "Software Engineer II",
        "Intermediate Backend Engineer",
    ],
)
def test_senior_titles_are_excluded(prefs, title):
    assert score(job(title), prefs).decision == "reject"


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Frontend Engineer", "frontend role"),
        ("Android Developer", "mobile role"),
        ("SDET", "qa role"),
        ("Data Scientist", "data science role"),
        ("DevOps Engineer", "devops role"),
        ("Security Engineer", "security role"),
        ("Product Manager", "non software role"),
        ("Technical Support Engineer", "non software role"),
    ],
)
def test_excluded_role_families(prefs, title, reason):
    m = score(job(title), prefs)
    assert m.decision == "reject"
    assert m.reject_reason == reason


def test_fullstack_with_substantial_backend_is_included(prefs):
    m = score(job("Full Stack Developer", description=f"0-2 years. React, {GOOD_STACK}"), prefs)
    assert m.decision == "match"
    assert "backend-heavy" in m.details["role"]


def test_kernel_roles_score_low_on_role(prefs):
    m = score(job("Member of Technical Staff, Kernel", description="C++, Linux, Python"), prefs)
    assert m.breakdown["role"] <= 8


# --- technology ----------------------------------------------------------------------


def test_python_backend_without_django_still_scores_well(prefs):
    m = score(job(description="0-2 years. Python, Flask, Kafka, Redis, microservices"), prefs)
    assert m.decision == "match"
    assert m.score >= 70


@pytest.mark.parametrize(
    ("stack", "named"),
    [
        ("Java, Spring Boot, Kafka", "Java"),
        ("Golang microservices", "Go"),
        ("Ruby on Rails", "Ruby"),
    ],
)
def test_non_python_stacks_are_excluded(prefs, stack, named):
    m = score(job(description=f"0-2 years. {stack}"), prefs)
    assert m.decision == "reject"
    assert m.reject_reason.startswith("no Python in the stack")
    assert named in m.reject_reason


def test_no_languages_listed_is_not_excluded(prefs):
    m = score(job(description="0-2 years. Build scalable REST APIs and microservices."), prefs)
    assert m.decision == "match"


def test_non_python_allowed_when_preference_off(prefs):
    loose = prefs.model_copy(update={"require_python": False})
    assert score(job(description="0-2 years. Java, Kafka"), loose).decision == "match"


def test_python_scores_above_non_python(prefs):
    python = score(job(description="0-2 years. Python, PostgreSQL, Kafka"), prefs)
    java = score(job(description="0-2 years. Java, PostgreSQL, Kafka"), prefs)
    assert python.breakdown["tech"] > java.breakdown["tech"]


def test_python_mentioned_after_cpp_is_secondary(prefs):
    primary = score(job(description="Python and C++"), prefs)
    secondary = score(job(description="C++ systems; some Python scripting"), prefs)
    assert primary.breakdown["tech"] > secondary.breakdown["tech"]
    assert "secondary" in secondary.details["tech"]


# --- location --------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["Bengaluru", "Gurugram", "Noida", "New Delhi", "Hyderabad"])
def test_preferred_locations_match(prefs, location):
    assert score(job(location=location), prefs).decision == "match"


@pytest.mark.parametrize("location", ["Pune", "Mumbai", "Chennai"])
def test_other_indian_cities_are_excluded_until_added(prefs, location):
    m = score(job(location=location), prefs)
    assert m.decision == "reject"
    assert "not preferred" in m.reject_reason


@pytest.mark.parametrize(
    ("location", "decision"),
    [
        ("Remote - India", "match"),
        ("Remote (Worldwide)", "match"),
        ("Remote (US only)", "reject"),
        ("Remote - EMEA", "reject"),
    ],
)
def test_remote_eligibility(prefs, location, decision):
    assert score(job(location=location), prefs).decision == decision


def test_remote_india_gets_a_boost_over_onsite(prefs):
    remote = score(job(location="Remote - India"), prefs)
    onsite = score(job(location="Bengaluru"), prefs)
    assert remote.breakdown["location"] > onsite.breakdown["location"]


@pytest.mark.parametrize("location", ["San Francisco, CA", "London, UK", "Santa Clara, California"])
def test_foreign_locations_are_excluded(prefs, location):
    assert score(job(location=location), prefs).decision == "reject"


def test_unknown_location_rejected_unless_jd_names_an_indian_city(prefs):
    assert score(job(location="N/A"), prefs).decision == "reject"
    in_jd = job(location="N/A", description=f"Join our Bengaluru office. 0-2 years. {GOOD_STACK}")
    assert score(in_jd, prefs).decision == "match"


def test_unknown_location_allowed_when_preference_says_so(prefs):
    loose = prefs.model_copy(update={"allow_unknown_location": True})
    assert score(job(location="N/A"), loose).decision == "match"


# --- salary ----------------------------------------------------------------------------


def test_undisclosed_salary_does_not_exclude(prefs):
    m = score(job(), prefs)
    assert m.decision == "match"
    assert m.details["salary"] == "Salary not disclosed"


def test_base_vs_ctc_ranking(prefs):
    base = score(job(description=f"0-2 years. {GOOD_STACK}. Base salary 14 LPA"), prefs)
    ctc = score(job(description=f"0-2 years. {GOOD_STACK}. CTC 14 LPA"), prefs)
    unknown = score(job(), prefs)
    assert base.breakdown["salary"] > ctc.breakdown["salary"] > unknown.breakdown["salary"]


def test_12_ctc_with_8_base_is_not_12_base(prefs):
    m = score(
        job(description=f"0-2 years. {GOOD_STACK}. CTC 12 LPA (8 LPA base + 4 LPA variable)"), prefs
    )
    assert m.decision == "reject"
    assert "base ₹8 LPA" in m.reject_reason


def test_base_between_floor_and_target_is_lower_priority(prefs):
    m = score(job(description=f"0-2 years. {GOOD_STACK}. Base 11 LPA"), prefs)
    assert m.decision == "match"
    assert m.breakdown["salary"] == 6


def test_stated_pay_below_floor_is_excluded(prefs):
    m = score(job(description=f"0-2 years. {GOOD_STACK}. CTC 7 LPA"), prefs)
    assert m.decision == "reject"
    assert "below your ₹10 LPA floor" in m.reject_reason


# --- job type --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Backend Engineering Intern", "Backend Developer - Contract", "Part-time Backend Engineer"],
)
def test_non_full_time_excluded(prefs, title):
    assert score(job(title), prefs).decision == "reject"


# --- preferences & explanations --------------------------------------------------------------


def test_preference_change_alters_outcome_without_refetching(prefs):
    pune = job(location="Pune")
    assert score(pune, prefs).decision == "reject"
    widened = prefs.model_copy(update={"locations": [*prefs.locations, "pune"]})
    m = score(pune, widened, version=2)
    assert m.decision == "match"
    assert m.prefs_version == 2


def test_min_score_threshold(prefs):
    j = job()
    strict = prefs.model_copy(update={"min_score": 99})
    m = score(j, strict)
    assert m.decision == "reject" and m.reject_reason.startswith("score ")


def test_weights_are_configurable(prefs):
    no_salary = prefs.model_copy(
        update={"weights": prefs.weights.model_copy(update={"salary": 0, "role": 35})}
    )
    m = score(job(), no_salary)
    assert m.breakdown["salary"] == 0
    assert m.breakdown["role"] > 25


def test_duplicate_jobs_never_match(prefs):
    dup = job().model_copy(update={"duplicate_of": "lever:acme:1"})
    assert score(dup, prefs).reject_reason == "duplicate of lever:acme:1"


def test_reasons_explain_the_match_and_skip_neutral_notes(prefs):
    m = score(job(description=f"Freshers welcome. {GOOD_STACK}"), prefs)
    assert any("Fresher" in r for r in m.reasons)
    assert any(r.startswith("Python + Django") for r in m.reasons)
    assert "Salary not disclosed" not in m.reasons
    text = explain_text(job(), m, prefs)
    assert "role" in text and "/25" in text


def test_unnormalised_job_is_normalised_on_the_fly(prefs):
    raw = job().model_copy(update={"normalized": None})
    assert score(raw, prefs).decision == "match"
