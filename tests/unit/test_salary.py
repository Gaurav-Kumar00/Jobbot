from __future__ import annotations

import pytest

from jobbot.normalize.salary import extract_salary


@pytest.mark.parametrize(
    ("text", "currency", "low", "high", "kind", "period"),
    [
        # LPA forms
        ("Salary: ₹12-18 LPA", "INR", 12, 18, "unspecified", "annual"),
        ("CTC: 14 LPA", "INR", 14, None, "ctc", "annual"),
        ("INR 15 LPA", "INR", 15, None, "unspecified", "annual"),
        ("Package upto 20 LPA", "INR", 20, None, "ctc", "annual"),
        ("12 to 15 lakhs per annum", "INR", 12, 15, "unspecified", "annual"),
        ("₹12L - ₹18L", "INR", 12, 18, "unspecified", "annual"),
        ("Compensation: 10.5 - 14 LPA", "INR", 10.5, 14, "unspecified", "annual"),
        ("12 lacs p.a.", "INR", 12, None, "unspecified", "annual"),
        # full rupee amounts (Indian and western grouping)
        ("Compensation: ₹12,00,000 – ₹18,00,000 per annum", "INR", 12, 18, "unspecified", "annual"),
        ("Salary 12,00,000 per annum", "INR", 12, None, "unspecified", "annual"),
        ("Rs. 1,500,000 per year", "INR", 15, None, "unspecified", "annual"),
        # base vs CTC
        ("Base salary: ₹16 LPA", "INR", 16, None, "base", "annual"),
        ("Fixed pay of 13 LPA", "INR", 13, None, "base", "annual"),
        ("We offer 12 LPA fixed + 3 LPA variable", "INR", 12, None, "base", "annual"),
        ("CTC of 12 LPA (8 LPA base + 4 LPA variable)", "INR", 8, None, "base", "annual"),
        ("Joining bonus of 2 LPA. Base salary: 15 LPA", "INR", 15, None, "base", "annual"),
        ("Total compensation 20 LPA including ESOPs", "INR", 20, None, "ctc", "annual"),
        ("Medical cover up to ₹10L. CTC: 16 LPA", "INR", 16, None, "ctc", "annual"),
        # monthly amounts are annualised
        ("Stipend: ₹50,000 per month", "INR", 6, None, "unspecified", "monthly"),
        ("pays ₹1.2 lakh per month", "INR", 14.4, None, "unspecified", "monthly"),
        ("Salary: 80k/month", "INR", 9.6, None, "unspecified", "monthly"),
        # USD
        ("$120,000 - $150,000", "USD", 120000, 150000, "unspecified", "annual"),
        ("USD 100k-140k base", "USD", 100000, 140000, "base", "annual"),
        ("$85k/year", "USD", 85000, None, "unspecified", "annual"),
    ],
)
def test_salary_table(text, currency, low, high, kind, period):
    info = extract_salary(text)
    assert info is not None, text
    assert (info.currency, info.min, info.max, info.kind, info.period) == (
        currency,
        low,
        high,
        kind,
        period,
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5+ years of experience",
        "Founded in 2014",
        "We serve 10 lakh merchants and process ₹12,000 crore annually",
        "Health insurance cover of ₹5 lakh for family",
        "Raised $50M in funding",
        "₹500 meal vouchers",
        "Over 2 lakh businesses use us",
        "Annual bonus of 2 LPA",
        "ESOPs worth 5 LPA",
        "We have 300+ engineers across 12 cities",
        "Python 3.12, Django 5",
    ],
)
def test_non_salary_text_is_ignored(text):
    assert extract_salary(text) is None


def test_base_is_preferred_over_ctc_regardless_of_order():
    info = extract_salary("CTC 18 LPA. Of this, base is 14 LPA.")
    assert (info.min, info.kind) == (14, "base")


def test_evidence_keeps_original_text():
    assert extract_salary("Salary: ₹12-18 LPA").evidence == "₹12-18 LPA"
