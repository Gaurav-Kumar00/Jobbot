"""Short extractive JD summary (no LLM): the first lines describing the actual role."""

from __future__ import annotations

import re

from jobbot.normalize.text import truncate

_ROLE_HEADING = re.compile(
    r"^\s*(?:what you(?:'|’)?ll (?:do|be doing)|what you will (?:do|be doing)|what will you do"
    r"|(?:key |your |job |primary )?responsibilities|the role|about the role|about this role"
    r"|role overview|in this role|your role|the opportunity|what the role involves"
    r"|job description|role summary|position summary|day to day|a day in the life)\b[^\n]{0,40}$",
    re.I | re.M,
)
_ROLE_HINT = re.compile(
    r"\b(you will|you(?:'|’)ll|we are looking for|we(?:'|’)re looking for|we are seeking"
    r"|seeking a|as an? [\w-]+ (?:engineer|developer)|in this role|responsible for)\b",
    re.I,
)
# Lines that start a new section (so the summary stops) or are bare headings.
_SECTION_HEADING = re.compile(
    r"^(?:about\b|requirements|qualifications|basic qualifications|preferred|nice to have"
    r"|perks|benefits|what we offer|who you are|you have|what you bring|skills|must have"
    r"|why join|our values|compensation|equal opportunity|eeo|the team|who we are)"
    r"[^.!?]{0,40}$"
    r"|^[^.!?]{0,60}:$",
    re.I,
)


def summarize(description: str, limit: int = 240) -> str:
    if not description:
        return ""
    heading = _ROLE_HEADING.search(description)
    body = description[heading.end() :] if heading else description
    lines = [line.strip(" •-*\t") for line in body.split("\n") if line.strip(" •-*\t")]
    if not heading:
        # Skip the company boilerplate that usually opens a JD.
        for index, line in enumerate(lines):
            if _ROLE_HINT.search(line):
                lines = lines[index:]
                break
    pieces: list[str] = []
    for line in lines:
        if _SECTION_HEADING.match(line):
            if pieces:
                break  # reached the next section
            continue
        pieces.append(line.rstrip(";"))
        if sum(len(p) for p in pieces) >= limit or len(pieces) >= 2:
            break
    return truncate(" ".join(pieces), limit)
