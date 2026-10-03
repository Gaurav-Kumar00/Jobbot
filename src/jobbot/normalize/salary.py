"""Salary extraction with base-vs-CTC awareness (Indian and USD formats).

INR amounts are expressed in LPA (lakhs per annum); other currencies in annual units.
"₹12 LPA CTC with ₹8 LPA base" is not "₹12 LPA base": when both are stated, the base
figure wins, and variable/bonus components are ignored.
"""

from __future__ import annotations

import re

from jobbot.models import SalaryInfo

_CUR = r"(?:₹|rs\.?|inr|\$|usd|us\$)"
_AMT = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNIT = r"(lpa|lakhs?|lacs?|l|k|m)"
_SALARY = re.compile(
    rf"(?<![\w.,])(?P<cur1>{_CUR})?\s*(?P<a>{_AMT[1:-1]})\s*(?P<ua>{_UNIT[1:-1]})?(?![a-z])"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<cur2>{_CUR})?\s*(?P<b>{_AMT[1:-1]})\s*(?P<ub>{_UNIT[1:-1]})?(?![a-z]))?"
    r"(?:\s*(?P<cur3>inr|usd)\b)?"
    r"(?:\s*(?P<per>lpa|per\s+annum|p\.\s?a\.?|pa\b|per\s+year|/\s*(?:year|yr|annum)|annually"
    r"|a\s+year|per\s+month|/\s*(?:month|mo)|monthly|pm)(?![a-z]))?"
)
_INR_CURRENCIES = {"₹", "rs", "rs.", "inr"}
_USD_CURRENCIES = {"$", "usd", "us$"}
_LAKH_UNITS = {"lpa", "lakh", "lakhs", "lac", "lacs", "l"}
_MONTHLY = re.compile(r"month|mo\b|pm")

_CONTEXT = re.compile(
    r"\b(salary|ctc|compensation|package|pay|payscale|stipend|base|fixed|remuneration|lpa"
    r"|per annum|budget|ote|earnings?|offer)\b"
)
_NOT_SALARY_AFTER = re.compile(
    r"\s*\+?\s*(crores?|cr\b|million|mn\b|billion|bn\b|users|customers|merchants|businesses"
    r"|downloads|transactions|members|people|employees|sellers|partners|companies|startups"
    r"|developers|students|learners|orders|deliveries|stores|cities|countries|years?|yrs?)"
)
# Money that isn't pay: insurance cover, funding, revenue...
_NOT_PAY_NEARBY = re.compile(
    r"\b(insurance|cover(?:age)?|mediclaim|medical|health|loan|funding|raised|valuation|revenue"
    r"|gmv|arr|turnover|investment|grant|prize|reward)\b"
)
_SENTENCE_BREAK = re.compile(r"[.;\n](?:\s|$)|\n")
_AFTER_BASE = re.compile(r"\s*[(\-,:]?\s*(fixed|base|basic)\b")
_AFTER_CTC = re.compile(r"\s*[(\-,:]?\s*(ctc|total|package)\b")
_AFTER_VARIABLE = re.compile(r"\s*[(\-,:]?\s*(variable|bonus|incentives?|esops?|stocks?|equity)\b")
_BEFORE = {
    "base": re.compile(r"\b(base|fixed|basic|guaranteed)\b"),
    "ctc": re.compile(
        r"\b(ctc|cost to company|total comp(?:ensation)?|package|ote|on[- ]target)\b"
    ),
    "variable": re.compile(
        r"\b(variable|bonus|incentives?|esops?|stock|equity|joining|relocation|retention)\b"
    ),
}
_KIND_RANK = {"base": 0, "ctc": 1, "unspecified": 2}


def extract_salary(text: str) -> SalaryInfo | None:
    if not text:
        return None
    lowered = text.lower()
    candidates: list[SalaryInfo] = []
    for m in _SALARY.finditer(lowered):
        if (info := _candidate(text, lowered, m)) is not None:
            candidates.append(info)
    if not candidates:
        return None
    return min(candidates, key=lambda c: _KIND_RANK[c.kind])  # stable: first of best kind


def _candidate(text: str, lowered: str, m: re.Match[str]) -> SalaryInfo | None:
    cur = m.group("cur1") or m.group("cur2") or m.group("cur3")
    unit = m.group("ua") or m.group("ub")
    per = m.group("per")
    if per and per.startswith("lpa"):
        unit = unit or "lpa"
    if unit == "m" and not cur:
        return None  # "5m" alone is too ambiguous

    if _NOT_SALARY_AFTER.match(lowered, m.end()):
        return None
    before = lowered[max(0, m.start() - 80) : m.start()]
    after = lowered[m.end() : m.end() + 60]
    has_context = bool(_CONTEXT.search(before) or _CONTEXT.search(after) or per)

    if cur in _USD_CURRENCIES:
        currency = "USD"
    elif cur in _INR_CURRENCIES or unit in _LAKH_UNITS:
        currency = "INR"
    elif has_context and unit is None and _number(m.group("a")) >= 100_000:
        currency = "INR"  # "salary 12,00,000 per annum"
    elif has_context and unit == "k" and per:
        currency = "INR"  # "Salary: 80k/month" (no symbol; India-focused search)
    else:
        return None
    # Same-sentence check only, so "Medical cover ₹10L. CTC: 16 LPA" keeps the CTC.
    clause_before = _SENTENCE_BREAK.split(before[-40:])[-1]
    clause_after = _SENTENCE_BREAK.split(after[:40])[0]
    if _NOT_PAY_NEARBY.search(clause_before) or _NOT_PAY_NEARBY.search(clause_after):
        return None
    # A currency plus a lakh unit, a range or a period is clearly an amount of money.
    explicit_money = cur is not None and bool(unit in _LAKH_UNITS or m.group("b") or per)
    if unit != "lpa" and not has_context and not explicit_money:
        return None  # e.g. "₹500" with no salary wording around it

    kind = _kind(lowered, m.end(), before)
    if kind is None:
        return None  # a variable / bonus component

    monthly = bool(per and _MONTHLY.search(per))
    low = _annualise(_number(m.group("a")), unit, currency, monthly)
    high = _annualise(_number(m.group("b")), unit, currency, monthly) if m.group("b") else None
    if not _plausible(low, currency) or (high is not None and not _plausible(high, currency)):
        return None
    if high is not None and high < low:
        low, high = high, low
    return SalaryInfo(
        currency=currency,
        min=low,
        max=high,
        kind=kind,
        period="monthly" if monthly else "annual",
        evidence=" ".join(text[m.start() : m.end()].split()),
    )


def _kind(lowered: str, end: int, before: str) -> str | None:
    if _AFTER_BASE.match(lowered, end):
        return "base"
    if _AFTER_VARIABLE.match(lowered, end):
        return None
    if _AFTER_CTC.match(lowered, end):
        return "ctc"
    nearest: tuple[int, str] | None = None
    for kind, pattern in _BEFORE.items():
        for hit in pattern.finditer(before):
            if nearest is None or hit.start() > nearest[0]:
                nearest = (hit.start(), kind)
    if nearest is None:
        return "unspecified"
    return None if nearest[1] == "variable" else nearest[1]


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _annualise(value: float, unit: str | None, currency: str, monthly: bool) -> float:
    if currency == "INR":
        if unit in _LAKH_UNITS:
            lakhs = value
        elif unit == "k":
            lakhs = value * 1_000 / 100_000
        elif unit == "m":
            lakhs = value * 10
        else:
            lakhs = value / 100_000
        return round(lakhs * 12 if monthly else lakhs, 2)
    multiplier = {"k": 1_000, "m": 1_000_000}.get(unit or "", 1)
    amount = value * multiplier
    return round(amount * 12 if monthly else amount, 2)


def _plausible(value: float, currency: str) -> bool:
    if currency == "INR":
        return 0.5 <= value <= 300  # LPA
    return 1_000 <= value <= 10_000_000
