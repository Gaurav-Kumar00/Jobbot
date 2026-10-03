"""Years-of-experience extraction from job titles and descriptions."""

from __future__ import annotations

import re

from jobbot.models import ExperienceInfo

_NUM = r"(?<![\d.])(\d{1,2}(?:\.\d)?)"
_YEARS = r"(?:years?|yrs?)\b"
_DASH = r"(?:-|–|—|to)"

# "0-2 years", "1 to 3 yrs", "2–4+ years"
_RANGE = re.compile(rf"{_NUM}\s*\+?\s*{_DASH}\s*{_NUM}\s*\+?\s*{_YEARS}", re.I)
# "3+ years"
_PLUS = re.compile(rf"{_NUM}\s*\+\s*{_YEARS}", re.I)
# "at least 2 years", "minimum of 3 years", "more than 5 years"
_MIN = re.compile(
    rf"\b(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than)\s*{_NUM}\s*\+?\s*{_YEARS}", re.I
)
# "2 years of professional experience", "2 years' experience", "1 year experience"
_PLAIN = re.compile(
    rf"{_NUM}\s*{_YEARS}[’'`]?(?:\s+of)?(?:\s+[\w/+.-]+){{0,4}}?\s+(?:experience|exp)\b", re.I
)
# "6 months of experience"
_MONTHS = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:-|to)?\s*(?:\d{1,2}\s*)?months?\s+(?:of\s+)?(?:[\w-]+\s+){0,3}"
    r"experience\b",
    re.I,
)
_FRESHER = re.compile(
    r"\b(freshers?|new[\s-]?grad(?:uate)?s?|recent\s+grad(?:uate)?s?|entry[\s-]level|early[\s-]career"
    r"|campus\s+(?:hire|hiring|recruit\w*|placement)|graduate\s+engineer\s+trainee"
    r"|20(?:24|25|26|27)\s+(?:batch|pass[\s-]?outs?|graduates?|grads?)"
    r"|class\s+of\s+20(?:25|26|27)|university\s+grad(?:uate)?s?"
    r"|no\s+(?:prior\s+)?(?:work\s+)?experience\s+(?:is\s+)?required)\b",
    re.I,
)
# Words that signal a fresher role only when they appear in the title.
_TITLE_FRESHER = re.compile(r"\b(get|graduate|trainee|apprentice)\b", re.I)

MAX_PLAUSIBLE_YEARS = 25.0


def extract_experience(title: str, description: str) -> ExperienceInfo:
    text = f"{title}\n{description}"
    fresher = _fresher_signal(title, description)

    ranges = [
        (float(m.group(1)), float(m.group(2)), m.group(0))
        for m in _RANGE.finditer(text)
        if _plausible(float(m.group(1))) and _plausible(float(m.group(2)))
    ]
    if ranges:
        low, high, evidence = ranges[0]  # the first range usually states the overall ask
        if low > high:
            low, high = high, low
        return ExperienceInfo(
            min_years=low, max_years=high, fresher=fresher or high <= 1, evidence=evidence.strip()
        )

    mins: list[tuple[float, str]] = []
    for pattern in (_PLUS, _MIN, _PLAIN):
        for m in pattern.finditer(text):
            value = float(m.group(1))
            if _plausible(value):
                mins.append((value, m.group(0)))
    if mins:
        # Several "N+ years" mentions -> the strictest is the real requirement.
        value, evidence = max(mins, key=lambda item: item[0])
        return ExperienceInfo(min_years=value, fresher=fresher, evidence=evidence.strip())

    if months := _MONTHS.search(text):
        return ExperienceInfo(
            min_years=round(int(months.group(1)) / 12, 2),
            fresher=True,
            evidence=months.group(0).strip(),
        )

    if fresher:
        return ExperienceInfo(min_years=0, max_years=1, fresher=True, evidence=_fresher_text(text))
    return ExperienceInfo()


def _fresher_signal(title: str, description: str) -> bool:
    return bool(_TITLE_FRESHER.search(title) or _FRESHER.search(f"{title}\n{description}"))


def _fresher_text(text: str) -> str | None:
    m = _FRESHER.search(text) or _TITLE_FRESHER.search(text.split("\n", 1)[0])
    return m.group(0) if m else None


def _plausible(years: float) -> bool:
    return 0 <= years <= MAX_PLAUSIBLE_YEARS
