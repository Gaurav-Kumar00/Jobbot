"""Human-readable rendering of a match decision (CLI and, later, Telegram /job)."""

from __future__ import annotations

from jobbot.matching.preferences import Preferences
from jobbot.matching.scorer import tier
from jobbot.models import Job, MatchResult

COMPONENT_ORDER = ("role", "tech", "experience", "location", "salary", "profile")


def explain_text(job: Job, match: MatchResult, prefs: Preferences) -> str:
    weights = prefs.weights.model_dump()
    verdict = "MATCH" if match.decision == "match" else f"REJECT — {match.reject_reason}"
    lines = [
        f"{tier(match.score)} {match.score}/100  {job.title} @ {job.company_name or job.company}",
        f"   {verdict}",
        f"   {job.location_raw or 'location n/a'} · {job.url}",
    ]
    for name in COMPONENT_ORDER:
        points = match.breakdown.get(name, 0)
        lines.append(f"   {name:<10} {points:>2}/{weights[name]:<2} {match.details.get(name, '')}")
    return "\n".join(lines)


def one_line(job: Job, match: MatchResult) -> str:
    name = (job.company_name or job.company)[:12]
    b = match.breakdown
    parts = "/".join(str(b.get(k, 0)) for k in COMPONENT_ORDER)
    return f"{tier(match.score)} {match.score:>3}  {name:<12} {job.title[:52]:<52} [{parts}]"
