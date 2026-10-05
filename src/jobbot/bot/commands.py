"""Telegram command handlers. Pure functions over the repository, so they are easy to test.

Preference changes are stored as a new preferences version; every scan re-scores stored
jobs whose match was computed under an older version, so changes apply on the next scan
without re-scraping (`/scan_now` applies them immediately).
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jobbot.matching.explain import explain_text
from jobbot.matching.preferences import Preferences, Profile, current_preferences
from jobbot.matching.scorer import match_job
from jobbot.models import AlertStatus
from jobbot.normalize import normalize_job
from jobbot.normalize.vocab import location_vocab, skill_matcher
from jobbot.notify.base import Notifier, OutgoingMessage
from jobbot.pipeline.digest import held_back, opening_line
from jobbot.pipeline.housekeeping import FREE_TIER_BYTES
from jobbot.pipeline.scan import realert
from jobbot.storage.base import Repository, StaleVersionError
from jobbot.timeutil import IST

Dispatch = Callable[[bool], Awaitable[str]]


@dataclass
class BotContext:
    repo: Repository
    notifier: Notifier
    dispatch_scan: Dispatch  # force -> URL of the Actions page
    profile: Profile
    now: datetime


Reply = list[OutgoingMessage]
Handler = Callable[[BotContext, list[str]], Awaitable[Reply]]


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def say(text: str) -> Reply:
    return [OutgoingMessage(text=text)]


# --------------------------------------------------------------------------- dispatch


async def handle(text: str, ctx: BotContext) -> Reply:
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return say("Send /help to see what I can do.")
    name = parts[0][1:].split("@", 1)[0].lower()  # "/status@Qeiroo_Bot" -> "status"
    entry = COMMANDS.get(name)
    if entry is None:
        return say(f"Unknown command /{esc(name)}. Send /help for the list.")
    return await entry[0](ctx, parts[1:])


# --------------------------------------------------------------------------- info


async def cmd_help(ctx: BotContext, args: list[str]) -> Reply:
    lines = ["🤖 <b>JobBot commands</b>"]
    for section, names in SECTIONS:
        lines.append(f"\n<b>{section}</b>")
        lines += [f"/{name} — {esc(COMMANDS[name][1])}" for name in names]
    return say("\n".join(lines))


async def cmd_status(ctx: BotContext, args: list[str]) -> Reply:
    prefs, version = current_preferences(ctx.repo)
    runs = ctx.repo.latest_runs(1)
    if runs:
        run = runs[0]
        s = run.stats
        scan = (
            f"{_ago(run.started_at, ctx.now)} ({esc(run.trigger)}) · "
            f"{s.get('sources_ok', 0)}/{s.get('sources_due') or s.get('sources_ok', 0)} sources OK"
        )
    else:
        scan = "never"
    states = ctx.repo.list_source_states()
    failing = [st for st in states if st.consecutive_failures]
    sent_today = _sent_since(ctx, _ist_midnight(ctx.now))
    waiting = len(held_back(ctx.repo))
    used = 100 * ctx.repo.storage_bytes() / FREE_TIER_BYTES
    lines = [
        "🤖 <b>JobBot status</b>",
        f"Alerts: {'⏸ <b>paused</b> (/resume)' if prefs.paused else '▶️ on'}",
        f"Last scan: {scan}",
        f"Sources: {len(states)} tracked · "
        + (f"⚠️ {len(failing)} failing (/sources)" if failing else "all healthy"),
        f"Alerts today: {sent_today}",
        f"Waiting to send: {waiting}" if waiting else "Waiting to send: none",
        f"AI checks today: {_ai_calls_today(ctx)}",
        f"Storage: {used:.1f}% of 512 MB",
        f"Preferences v{version} · min score {prefs.min_score}",
    ]
    return say("\n".join(lines))


async def cmd_sources(ctx: BotContext, args: list[str]) -> Reply:
    states = ctx.repo.list_source_states()
    failing = sorted(
        (s for s in states if s.consecutive_failures), key=lambda s: -s.consecutive_failures
    )
    if not failing:
        return say(f"✅ All {len(states)} sources healthy.")
    lines = [f"⚠️ <b>{len(failing)} of {len(states)} sources failing</b>"]
    for state in failing[:15]:
        lines.append(
            f"• <b>{esc(state.key)}</b> ×{state.consecutive_failures}: "
            f"<i>{esc((state.last_error or '?')[:120])}</i>"
        )
    return say("\n".join(lines))


async def cmd_latest(ctx: BotContext, args: list[str]) -> Reply:
    count = _int_arg(args, default=5, low=1, high=20)
    if count is None:
        return say("Usage: /latest [1-20]")
    return _alert_list(ctx, _recent_sent(ctx, limit=count), f"🆕 <b>Latest {count} alerts</b>")


async def cmd_today(ctx: BotContext, args: list[str]) -> Reply:
    since = _ist_midnight(ctx.now)
    alerts = [a for a in _recent_sent(ctx, limit=200) if a.sent_at and a.sent_at >= since]
    return _alert_list(ctx, alerts, f"📅 <b>Today: {len(alerts)} alerts</b>")


async def cmd_history(ctx: BotContext, args: list[str]) -> Reply:
    days = _int_arg(args, default=7, low=1, high=60)
    if days is None:
        return say("Usage: /history [days 1-60]")
    since = ctx.now - timedelta(days=days)
    alerts = [a for a in _recent_sent(ctx, limit=500) if a.sent_at and a.sent_at >= since]
    return _alert_list(ctx, alerts[:25], f"🗂 <b>Last {days} days: {len(alerts)} alerts</b>")


async def cmd_job(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) != 1:
        return say("Usage: /job &lt;job id&gt; — the id is at the bottom of every alert.")
    job = ctx.repo.get_job(args[0])
    if job is None:
        return say(f"No stored job <code>{esc(args[0])}</code>.")
    if job.normalized is None:
        job = normalize_job(job)
    prefs, version = current_preferences(ctx.repo)
    match = match_job(job, prefs, ctx.profile, version)
    text = f"<pre>{esc(explain_text(job, match, prefs))}</pre>"
    return say(text + f'\n<a href="{html.escape(job.url, quote=True)}">Apply ↗</a>')


async def cmd_realert(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) != 1:
        return say("Usage: /realert &lt;job id&gt;")
    prefs, version = current_preferences(ctx.repo)
    try:
        await realert(ctx.repo, ctx.notifier, args[0], prefs, version, ctx.profile)
    except (LookupError, RuntimeError) as exc:
        return say(f"Couldn't re-send: {esc(exc)}")
    return []  # the alert itself is the reply


# --------------------------------------------------------------------------- control


async def cmd_pause(ctx: BotContext, args: list[str]) -> Reply:
    return _update(ctx, lambda p: p.model_copy(update={"paused": True}), "pause",
                   "⏸ Alerts paused. Matches are held and delivered on /resume.")  # fmt: skip


async def cmd_resume(ctx: BotContext, args: list[str]) -> Reply:
    return _update(ctx, lambda p: p.model_copy(update={"paused": False}), "resume",
                   "▶️ Alerts resumed. Anything held arrives with the next scan (or /scan_now).")  # fmt: skip


async def cmd_scan_now(ctx: BotContext, args: list[str]) -> Reply:
    url = await ctx.dispatch_scan(True)
    return say(
        "🚀 Full scan started (every source, current preferences). "
        f'Results arrive in ~2–3 minutes. <a href="{html.escape(url, quote=True)}">Progress</a>'
    )


# --------------------------------------------------------------------------- preferences


async def cmd_prefs(ctx: BotContext, args: list[str]) -> Reply:
    prefs, version = current_preferences(ctx.repo)
    lines = [
        f"⚙️ <b>Preferences v{version}</b>{' (defaults)' if version == 0 else ''}",
        f"Alerts: {'paused' if prefs.paused else 'on'} · min score {prefs.min_score}",
        f"Locations: {esc(', '.join(prefs.locations) or '—')}",
        f"Remote (India-eligible): {'yes' if prefs.allow_remote else 'no'}"
        f" · remote bonus +{prefs.remote_bonus}",
        f"Experience: up to {prefs.max_min_years:g} years required",
        f"Salary: target ₹{prefs.target_base_lpa:g} LPA base · floor ₹{prefs.floor_lpa:g} LPA",
        f"Python required: {'yes' if prefs.require_python else 'no'}",
        f"Core skills: {esc(', '.join(prefs.primary_skills))}",
        f"Other skills: {esc(', '.join(prefs.secondary_skills))}",
        f"Preferred titles: {esc(', '.join(prefs.primary_titles))}",
        f"Excluded levels: {esc(', '.join(prefs.excluded_seniority))}",
    ]
    return say("\n".join(lines))


async def cmd_set_salary(ctx: BotContext, args: list[str]) -> Reply:
    try:
        target = float(args[0])
        floor = float(args[1]) if len(args) > 1 else min(target, _prefs(ctx).floor_lpa)
    except (IndexError, ValueError):
        return say("Usage: /set_salary &lt;target LPA&gt; [floor LPA] — e.g. /set_salary 12 10")
    if not (0 < floor <= target <= 200):
        return say("Need 0 &lt; floor ≤ target ≤ 200 (LPA).")
    return _update(ctx, lambda p: p.model_copy(update={"target_base_lpa": target, "floor_lpa": floor}),
                   f"set_salary {target:g} {floor:g}",
                   f"💰 Target base ₹{target:g} LPA, floor ₹{floor:g} LPA.")  # fmt: skip


async def cmd_set_exp(ctx: BotContext, args: list[str]) -> Reply:
    years = _float_arg(args, low=0, high=15)
    if years is None:
        return say(
            "Usage: /set_exp &lt;years&gt; — exclude jobs requiring more than this, e.g. /set_exp 1.5"
        )
    return _update(ctx, lambda p: p.model_copy(update={"max_min_years": years}),
                   f"set_exp {years:g}", f"🧑‍💻 Jobs requiring more than {years:g} years are excluded.")  # fmt: skip


async def cmd_set_min_score(ctx: BotContext, args: list[str]) -> Reply:
    score = _int_arg(args, default=None, low=0, high=100)
    if score is None:
        return say("Usage: /set_min_score &lt;0-100&gt;")
    return _update(ctx, lambda p: p.model_copy(update={"min_score": score}),
                   f"set_min_score {score}", f"🎯 Minimum score is now {score}.")  # fmt: skip


async def cmd_set_remote(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) != 1 or args[0].lower() not in ("on", "off"):
        return say("Usage: /set_remote on|off")
    on = args[0].lower() == "on"
    return _update(ctx, lambda p: p.model_copy(update={"allow_remote": on}), f"set_remote {args[0]}",
                   f"🏠 India-eligible remote roles {'included' if on else 'excluded'}.")  # fmt: skip


async def cmd_location(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) < 2 or args[0] not in ("add", "remove"):
        return say("Usage: /location add|remove &lt;city&gt; — e.g. /location add pune")
    name = " ".join(args[1:]).lower()
    vocab = location_vocab()
    found = vocab.cities.find(name) or vocab.region_aliases.find(name)
    canonical = found[0] if found else (name if name in vocab.region_cities else None)
    if canonical is None:
        return say(
            f"I don't know the place “{esc(name)}”. Try a city like pune, mumbai, chennai, or ncr."
        )
    current = _prefs(ctx).locations
    if args[0] == "add":
        if canonical in current:
            return say(f"{esc(canonical)} is already in your locations.")
        updated = [*current, canonical]
    else:
        if canonical not in current:
            return say(f"{esc(canonical)} isn't in your locations.")
        updated = [c for c in current if c != canonical]
    return _update(ctx, lambda p: p.model_copy(update={"locations": updated}),
                   f"location {args[0]} {canonical}", f"📍 Locations: {esc(', '.join(updated) or '—')}")  # fmt: skip


async def cmd_skill(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) < 2 or args[0] not in ("add", "remove"):
        return say("Usage: /skill add|remove &lt;skill&gt; — e.g. /skill add redis")
    term = " ".join(args[1:])
    found = skill_matcher().find(term)
    if not found:
        return say(f"Unknown skill “{esc(term)}”. Skills come from config/taxonomy.yaml.")
    skill = found[0]
    prefs = _prefs(ctx)
    if args[0] == "add":
        if skill in prefs.primary_skills or skill in prefs.secondary_skills:
            return say(f"{esc(skill)} is already one of your skills.")
        change = {"secondary_skills": [*prefs.secondary_skills, skill]}
    else:
        if skill not in prefs.primary_skills and skill not in prefs.secondary_skills:
            return say(f"{esc(skill)} isn't one of your skills.")
        change = {
            "primary_skills": [s for s in prefs.primary_skills if s != skill],
            "secondary_skills": [s for s in prefs.secondary_skills if s != skill],
        }
    return _update(ctx, lambda p: p.model_copy(update=change), f"skill {args[0]} {skill}",
                   f"🧰 Skill {esc(skill)} {'added' if args[0] == 'add' else 'removed'}.")  # fmt: skip


async def cmd_role(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) < 2 or args[0] not in ("add", "remove"):
        return say("Usage: /role add|remove &lt;title&gt; — e.g. /role add platform engineer")
    title = " ".join(args[1:]).lower().strip()
    current = _prefs(ctx).primary_titles
    if args[0] == "add":
        if title in current:
            return say(f"“{esc(title)}” is already a preferred title.")
        updated = [*current, title]
    else:
        if title not in current:
            return say(f"“{esc(title)}” isn't a preferred title.")
        updated = [t for t in current if t != title]
    return _update(ctx, lambda p: p.model_copy(update={"primary_titles": updated}),
                   f"role {args[0]} {title}", f"🏷 Preferred titles: {esc(', '.join(updated))}")  # fmt: skip


async def cmd_set_interval(ctx: BotContext, args: list[str]) -> Reply:
    if len(args) != 2:
        return say(
            "Usage: /set_interval &lt;source&gt; &lt;minutes&gt; — e.g. /set_interval greenhouse:stripe 180"
        )
    try:
        minutes = int(args[1])
    except ValueError:
        return say("Minutes must be a whole number.")
    if not 30 <= minutes <= 1440:
        return say("Interval must be between 30 and 1440 minutes.")
    states = {s.key: s for s in ctx.repo.list_source_states()}
    key = (
        args[0]
        if args[0] in states
        else next((k for k in states if k.endswith(f":{args[0]}")), None)
    )
    if key is None:
        return say(
            f"Unknown source “{esc(args[0])}”. See /sources or use ats:company, e.g. lever:cred."
        )
    ctx.repo.save_source_state(states[key].model_copy(update={"interval_minutes": minutes}))
    return say(f"⏱ {esc(key)} will be scanned every {minutes} minutes.")


# --------------------------------------------------------------------------- helpers


def _prefs(ctx: BotContext) -> Preferences:
    return current_preferences(ctx.repo)[0]


def _update(
    ctx: BotContext, change: Callable[[Preferences], Preferences], note: str, done: str
) -> Reply:
    prefs, version = current_preferences(ctx.repo)
    updated = change(prefs)
    try:
        doc = ctx.repo.save_preferences(updated.model_dump(), expected_version=version, note=note)
    except StaleVersionError:
        return say("Preferences changed at the same moment — please send the command again.")
    return say(
        f"{done}\n<i>Saved as v{doc.version}. Applies from the next scan; /scan_now to apply now.</i>"
    )


def _recent_sent(ctx: BotContext, limit: int):
    alerts = ctx.repo.list_alerts(status=AlertStatus.SENT, limit=max(limit, 50))
    alerts.sort(key=lambda a: a.sent_at or a.claimed_at, reverse=True)
    return alerts[:limit]


def _sent_since(ctx: BotContext, since: datetime) -> int:
    return sum(1 for a in _recent_sent(ctx, limit=500) if a.sent_at and a.sent_at >= since)


def _alert_list(ctx: BotContext, alerts, title: str) -> Reply:
    if not alerts:
        return say(f"{title}\nNothing yet.")
    jobs = ctx.repo.get_jobs(a.job_id for a in alerts)
    matches = ctx.repo.get_matches(a.job_id for a in alerts)
    lines = [title]
    for alert in alerts:
        job, match = jobs.get(alert.job_id), matches.get(alert.job_id)
        if job is not None and match is not None:
            lines.append(opening_line(job, match).rstrip())
    return say("\n".join(lines)[:4000])


def _ai_calls_today(ctx: BotContext) -> int:
    usage = ctx.repo.get_meta("llm_usage") or {}
    return usage.get("calls", 0) if usage.get("date") == ctx.now.date().isoformat() else 0


def _ist_midnight(now: datetime) -> datetime:
    local = now.astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min ago"
    return f"{minutes // 1440} days ago"


def _int_arg(args: list[str], *, default: int | None, low: int, high: int) -> int | None:
    if not args:
        return default
    try:
        value = int(args[0])
    except ValueError:
        return None
    return value if low <= value <= high else None


def _float_arg(args: list[str], *, low: float, high: float) -> float | None:
    try:
        value = float(args[0])
    except (IndexError, ValueError):
        return None
    return value if low <= value <= high else None


COMMANDS: dict[str, tuple[Handler, str]] = {
    "start": (cmd_help, "show this help"),
    "help": (cmd_help, "show this help"),
    "status": (cmd_status, "health, last scan, today's alerts"),
    "sources": (cmd_sources, "failing sources and errors"),
    "latest": (cmd_latest, "last N alerts, e.g. /latest 10"),
    "today": (cmd_today, "alerts sent today"),
    "history": (cmd_history, "alerts in the last N days, e.g. /history 7"),
    "job": (cmd_job, "full score breakdown: /job <id>"),
    "realert": (cmd_realert, "send a job's alert again: /realert <id>"),
    "pause": (cmd_pause, "hold alerts (scanning continues)"),
    "resume": (cmd_resume, "deliver held alerts and continue"),
    "scan_now": (cmd_scan_now, "run a full scan right now"),
    "rematch": (cmd_scan_now, "re-score everything with current preferences now"),
    "prefs": (cmd_prefs, "show preferences"),
    "set_salary": (cmd_set_salary, "target & floor in LPA: /set_salary 12 10"),
    "set_exp": (cmd_set_exp, "max years required: /set_exp 1.5"),
    "set_min_score": (cmd_set_min_score, "alert threshold: /set_min_score 60"),
    "set_remote": (cmd_set_remote, "India-eligible remote: /set_remote on|off"),
    "location": (cmd_location, "/location add|remove <city>"),
    "skill": (cmd_skill, "/skill add|remove <skill>"),
    "role": (cmd_role, "/role add|remove <title>"),
    "set_interval": (cmd_set_interval, "/set_interval <source> <minutes>"),
}
SECTIONS = [
    ("Jobs", ["latest", "today", "history", "job", "realert"]),
    ("Control", ["status", "sources", "pause", "resume", "scan_now", "rematch"]),
    ("Preferences", ["prefs", "set_salary", "set_exp", "set_min_score", "set_remote",
                     "location", "skill", "role", "set_interval"]),
]  # fmt: skip
