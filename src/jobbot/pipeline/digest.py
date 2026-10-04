"""Digest of held-back matches: still-open roles that were already posted when their
company was first scanned. Instead of a flood of full alerts (or silently dropping them),
they arrive as compact messages — one line per unique opening, best first."""

from __future__ import annotations

import html
import logging
from collections import defaultdict

from jobbot.matching.scorer import tier
from jobbot.models import AlertStatus, Job, MatchResult
from jobbot.normalize.dedup import choose_canonical
from jobbot.notify.base import Notifier, OutgoingMessage
from jobbot.storage.base import Repository

log = logging.getLogger(__name__)

MAX_CHARS = 3800  # Telegram's limit is 4096; leave room for the header


def held_back(repo: Repository) -> list[tuple[Job, MatchResult]]:
    """Unique, still-open, still-matching openings that were recorded but never sent."""
    alerts = repo.list_alerts(status=AlertStatus.BASELINE)
    ids = [a.job_id for a in alerts]
    jobs, matches = repo.get_jobs(ids), repo.get_matches(ids)
    candidates = [
        jobs[i]
        for i in ids
        if i in jobs
        and jobs[i].is_active
        and not jobs[i].duplicate_of
        and getattr(matches.get(i), "decision", "") == "match"
    ]
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in candidates:
        groups[job.fingerprint or job.id].append(job)
    chosen: list[tuple[Job, MatchResult]] = []
    for group in groups.values():
        canonical = choose_canonical(group)
        for other in group:
            if other.id != canonical.id:
                repo.set_duplicate_of(other.id, canonical.id)
        chosen.append((canonical, matches[canonical.id]))
    chosen.sort(key=lambda pair: (-pair[1].score, pair[0].title))
    return chosen


def digest_messages(entries: list[tuple[Job, MatchResult]]) -> list[tuple[str, list[str]]]:
    """Render entries into (html_text, job_ids) chunks that each fit one Telegram message."""
    header = (
        f"📬 <b>{len(entries)} more matching roles, still open</b>\n"
        "<i>Posted before JobBot started watching these companies. New ones keep "
        "arriving as full alerts.</i>\n"
    )
    chunks: list[tuple[str, list[str]]] = []
    text, ids = header, []
    for job, match in entries:
        line = opening_line(job, match)
        if len(text) + len(line) > MAX_CHARS and ids:
            chunks.append((text, ids))
            text, ids = "", []
        text += line
        ids.append(job.id)
    if ids:
        chunks.append((text, ids))
    return chunks


async def send_held_back(repo: Repository, notifier: Notifier) -> int:
    """Send the digest and mark each included job as alerted. Returns openings sent."""
    entries = held_back(repo)
    sent = 0
    for text, ids in digest_messages(entries):
        message_id = await notifier.send(OutgoingMessage(text=text.rstrip()))
        for job_id in ids:
            repo.mark_alert_sent(job_id, message_id)
        sent += len(ids)
    if sent:
        log.info("digest sent", extra={"openings": sent})
    return sent


def opening_line(job: Job, match: MatchResult) -> str:
    n = job.normalized
    place = ", ".join(c.replace("_", " ").title() for c in n.location.cities[:2]) if n else ""
    if n and n.location.remote_scope.value in ("india", "global"):
        place = f"{place} · remote".strip(" ·")
    exp = n.experience if n else None
    years = ""
    if exp and exp.min_years is not None:
        years = (
            f"{exp.min_years:g}–{exp.max_years:g} yrs"
            if exp.max_years is not None
            else f"{exp.min_years:g}+ yrs"
        )
    elif exp and exp.fresher:
        years = "fresher"
    details = " · ".join(x for x in (job.company_name or job.company, place, years) if x)
    url = html.escape(job.url, quote=True)
    return (
        f"\n{tier(match.score)} <b>{match.score}</b> · "
        f'<a href="{url}">{html.escape(job.title, quote=False)}</a>\n'
        f"     {html.escape(details, quote=False)}\n"
    )
