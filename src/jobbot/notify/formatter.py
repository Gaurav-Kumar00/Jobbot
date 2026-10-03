"""Telegram (HTML) rendering of job alerts."""

from __future__ import annotations

import html
from datetime import datetime

from jobbot.matching.scorer import tier
from jobbot.models import Job, MatchResult, Normalized, RemoteScope, WorkMode
from jobbot.normalize.summary import summarize
from jobbot.notify.base import Button, OutgoingMessage
from jobbot.notify.telegram import MAX_MESSAGE_LEN
from jobbot.timeutil import IST

SOURCE_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters",
    "workday": "Workday",
    "amazon": "Amazon Jobs",
    "microsoft": "Microsoft Careers",
    "atlassian": "Atlassian Careers",
    "adzuna": "Adzuna",
    "hn": "HN Who's Hiring",
}
MODE_LABELS = {
    WorkMode.ONSITE: "On-site",
    WorkMode.HYBRID: "Hybrid",
    WorkMode.REMOTE: "Remote",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_alert(job: Job, match: MatchResult, *, now: datetime) -> OutgoingMessage:
    n = job.normalized or Normalized(version=0)
    header = [
        f"{tier(match.score)} <b>{match.score}/100 · {esc(job.title)}</b>",
        f"🏢 <b>{esc(job.company_name or job.company)}</b>",
    ]
    facts = [
        _field("📍", "Location", _where(job, n), "Not stated"),
        _field("🏠", "Work mode", _mode(n), "Not stated"),
        _field("🧑‍💻", "Experience", _experience(n), "Not stated"),
        _field("💰", "Salary", _salary(n), "Not disclosed"),
        _when(job, now),
    ]
    sections = ["\n".join(header), "\n".join(facts)]
    if match.reasons:
        bullets = "\n".join(f"• {esc(reason)}" for reason in match.reasons[:5])
        sections.append(f"✅ <b>Why it matches</b>\n{bullets}")
    summary = summarize(job.description)
    if summary:
        sections.append(f"📝 <b>About the role</b>\n{esc(summary)}")
    source = SOURCE_LABELS.get(job.source, job.source.title())
    sections.append(f"🔗 {esc(source)} · <code>{esc(job.id)}</code>")

    text = "\n\n".join(sections)
    if len(text) > MAX_MESSAGE_LEN:  # only possible with absurd titles; drop the summary
        sections = [s for s in sections if not s.startswith("📝")]
        text = "\n\n".join(sections)[:MAX_MESSAGE_LEN]
    return OutgoingMessage(text=text, buttons=(Button("Apply ↗", job.url),))


def _field(emoji: str, label: str, value: tuple[str, str] | None, missing: str) -> str:
    """Known values in bold (with an optional italic note); unknown ones in italics."""
    if value is None:
        return f"{emoji} <b>{label}:</b> <i>{missing}</i>"
    main, note = value
    suffix = f" <i>({esc(note)})</i>" if note else ""
    return f"{emoji} <b>{label}:</b> <b>{esc(main)}</b>{suffix}"


def _where(job: Job, n: Normalized) -> tuple[str, str] | None:
    loc = n.location
    if loc.cities:
        place = ", ".join(c.replace("_", " ").title() for c in loc.cities[:3])
    elif job.location_raw.strip():
        place = job.location_raw.strip()
    elif loc.remote_scope is RemoteScope.NOT_REMOTE:
        return None
    else:
        place = "Remote"
    note = {
        RemoteScope.INDIA: "remote, open to India",
        RemoteScope.GLOBAL: "remote, worldwide",
        RemoteScope.APAC: "remote, APAC",
    }.get(loc.remote_scope, "")
    return place, note


def _mode(n: Normalized) -> tuple[str, str] | None:
    mode = n.location.work_mode
    return None if mode is WorkMode.UNKNOWN else (MODE_LABELS[mode], "")


def _experience(n: Normalized) -> tuple[str, str] | None:
    exp = n.experience
    if exp.min_years is None:
        return ("Fresher-friendly", "") if exp.fresher else None
    years = (
        f"{exp.min_years:g}–{exp.max_years:g} yrs"
        if exp.max_years is not None
        else f"{exp.min_years:g}+ yrs"
    )
    return years, "fresher-friendly" if exp.fresher else ""


def _salary(n: Normalized) -> tuple[str, str] | None:
    s = n.salary
    if s is None or s.min is None:
        return None
    if s.currency == "INR":
        amount = f"₹{s.min:g}{f'–{s.max:g}' if s.max else ''} LPA"
    else:
        amount = f"{s.currency} {s.min:,.0f}{f'–{s.max:,.0f}' if s.max else ''}/yr"
    if s.kind == "base":
        return f"Base {amount}", ""
    if s.kind == "ctc":
        return f"CTC {amount}", "base not stated"
    return amount, "base vs CTC not stated"


def _when(job: Job, now: datetime) -> str:
    found = job.first_seen_at.astimezone(IST).strftime("%d %b, %H:%M IST")
    if job.posted_at is None:
        return f"🕒 <b>Posted:</b> <i>Not stated</i> · found {found}"
    return f"🕒 <b>Posted:</b> <b>{_ago(job.posted_at, now)}</b> · found {found}"


def _ago(then: datetime, now: datetime) -> str:
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"
