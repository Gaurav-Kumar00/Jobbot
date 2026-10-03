from __future__ import annotations

import pytest

from jobbot.normalize.experience import extract_experience


@pytest.mark.parametrize(
    ("title", "description", "min_years", "max_years", "fresher"),
    [
        # explicit ranges
        ("Backend Engineer", "Experience: 0-2 years", 0, 2, False),
        ("Backend Engineer", "0 - 2 yrs of experience in Python", 0, 2, False),
        ("Backend Engineer", "1-3 years of experience", 1, 3, False),
        ("Backend Engineer", "2 to 4 years building APIs", 2, 4, False),
        ("Backend Engineer", "Requires 0–1 Years of experience", 0, 1, True),
        ("Backend Engineer", "3–5+ years of backend experience", 3, 5, False),
        ("Software Engineer (0-2 Years)", "", 0, 2, False),
        ("Backend Engineer", "Experience required: 1.5-3 years", 1.5, 3, False),
        ("Backend Engineer", "5-3 years (typo)", 3, 5, False),
        # first range is the overall requirement; later ranges are skill-specific
        ("Backend Engineer", "0-2 years overall. 1-2 years with Kafka is a plus.", 0, 2, False),
        # plus / minimum forms
        ("Backend Engineer", "3+ years of experience", 3, None, False),
        ("Backend Engineer", "1+ yrs exp", 1, None, False),
        ("Backend Engineer", "At least 2 years of professional experience", 2, None, False),
        ("Backend Engineer", "Minimum of 4 years in software development", 4, None, False),
        ("Backend Engineer", "more than 6 years of experience", 6, None, False),
        ("Backend Engineer", "2 years of experience with Django", 2, None, False),
        ("Backend Engineer", "1 year experience preferred", 1, None, False),
        ("Backend Engineer", "2 years' experience", 2, None, False),
        # strictest "N+" wins when there are several
        ("Backend Engineer", "5+ years of experience; 2+ years with Python", 5, None, False),
        # months
        ("Backend Engineer", "6 months of internship experience", 0.5, None, True),
        # fresher signals without numbers
        ("Backend Engineer", "Freshers are welcome to apply!", 0, 1, True),
        ("Software Engineer - New Grad", "", 0, 1, True),
        ("Backend Engineer", "Open to 2026 batch graduates", 0, 1, True),
        ("Backend Engineer", "2025 pass-outs only", 0, 1, True),
        ("Backend Engineer", "This is an entry-level position", 0, 1, True),
        ("Graduate Engineer Trainee", "", 0, 1, True),
        ("GET - Software", "", 0, 1, True),
        ("Backend Engineer", "Class of 2026 students", 0, 1, True),
        ("Backend Engineer", "No prior experience required", 0, 1, True),
        ("Backend Engineer", "Recent graduates with strong DSA", 0, 1, True),
        # fresher keyword plus a number: number decides, fresher flag kept
        ("Backend Engineer", "Freshers or 0-1 years of experience", 0, 1, True),
        # nothing stated
        ("Backend Engineer", "Build great APIs with Python and Kafka.", None, None, False),
        ("Backend Engineer", "", None, None, False),
    ],
)
def test_experience_table(title, description, min_years, max_years, fresher):
    info = extract_experience(title, description)
    assert info.min_years == min_years
    assert info.max_years == max_years
    assert info.fresher is fresher


@pytest.mark.parametrize(
    "description",
    [
        "Founded in 2014, we have grown 10x.",
        "Serving customers since 2010-2024",
        "We get things done.",  # "get" only counts in titles (Graduate Engineer Trainee)
        "Our 500+ engineers",
        "Contract of 12 months",
    ],
)
def test_non_experience_numbers_are_ignored(description):
    info = extract_experience("Backend Engineer", description)
    assert info.min_years is None
    assert info.fresher is False


def test_implausible_values_are_ignored():
    assert (
        extract_experience("Engineer", "with 50 years of experience in fintech").min_years is None
    )


def test_evidence_is_recorded():
    info = extract_experience("Backend Engineer", "We need 1-3 years of experience.")
    assert info.evidence == "1-3 years"
