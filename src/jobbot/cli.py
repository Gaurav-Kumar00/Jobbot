"""Command-line entry point: `uv run jobbot <command>` or `python -m jobbot <command>`."""

from __future__ import annotations

import argparse
import asyncio
import html
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pymongo.errors import PyMongoError

from jobbot import __version__
from jobbot.config import find_company, load_companies
from jobbot.http import FetchError, HttpClient
from jobbot.llm.chain import build_chain
from jobbot.log import redact, setup_logging
from jobbot.matching.explain import explain_text, one_line
from jobbot.matching.preferences import Preferences, current_preferences, load_profile
from jobbot.matching.scorer import match_job
from jobbot.models import CompanyTarget, Job, MatchResult, UpsertOutcome
from jobbot.normalize import NORMALIZER_VERSION, needs_renormalize, normalize_job
from jobbot.notify.base import OutgoingMessage
from jobbot.notify.telegram import TelegramClient, TelegramError, TelegramNotifier
from jobbot.pipeline.collect import collect
from jobbot.pipeline.digest import held_back, send_held_back
from jobbot.pipeline.discover import discover, yaml_line
from jobbot.pipeline.scan import ScanReport, realert, run_scan
from jobbot.settings import ConfigError, Settings
from jobbot.sources import SOURCES, get_source
from jobbot.storage import open_repository
from jobbot.timeutil import IST, fmt_ist, utcnow

ATLAS_FREE_TIER_BYTES = 512 * 1024 * 1024

Handler = Callable[[Settings, argparse.Namespace], Awaitable[int]]


@dataclass(frozen=True)
class Command:
    handler: Handler
    help: str
    configure: Callable[[argparse.ArgumentParser], None] | None = None


async def cmd_ping(settings: Settings, args: argparse.Namespace) -> int:
    """Send a test message to the owner chat."""
    settings.require("telegram_bot_token", "telegram_chat_id")
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        me = await client.get_me()
        text = (
            "✅ <b>JobBot is alive</b>\n"
            f"Bot: @{html.escape(me.get('username', '?'))} · v{__version__}\n"
            f"Env: {html.escape(settings.jobbot_env)} · {fmt_ist(utcnow())}"
        )
        message_id = await TelegramNotifier(client, settings.telegram_chat_id).send(
            OutgoingMessage(text=text)
        )
    print(f"Sent test message (message_id={message_id}). Check Telegram.")
    return 0


async def cmd_whoami(settings: Settings, args: argparse.Namespace) -> int:
    """Validate the bot token and show the bot's identity."""
    settings.require("telegram_bot_token")
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        me = await client.get_me()
    print(f"Bot OK: @{me.get('username')} (id={me.get('id')}, name={me.get('first_name')})")
    return 0


async def cmd_chat_id(settings: Settings, args: argparse.Namespace) -> int:
    """List chats that recently messaged the bot, to find TELEGRAM_CHAT_ID. Local use only."""
    settings.require("telegram_bot_token")
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        updates = await client.get_updates()
    chats: dict[int, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
            chats[chat["id"]] = f"{chat.get('type', '?')} · {name}"
    if not chats:
        print("No messages found. Send any message (e.g. /start) to your bot, then re-run.")
        return 1
    for chat_id, label in chats.items():
        print(f"{chat_id}\t{label}")
    print("Put your private chat's id in .env as TELEGRAM_CHAT_ID.")
    return 0


async def cmd_db_init(settings: Settings, args: argparse.Namespace) -> int:
    """Connect to MongoDB, create indexes (idempotent) and report storage usage."""
    settings.require("mongodb_uri")
    repo = open_repository(settings)
    repo.ping()
    repo.ensure_indexes()
    used = repo.storage_bytes()
    pct = 100 * used / ATLAS_FREE_TIER_BYTES
    print(f"MongoDB OK: db={settings.mongodb_db}, indexes ensured.")
    print(f"Storage used: {used / 1024 / 1024:.2f} MB ({pct:.1f}% of the 512 MB free tier)")
    return 0


def _configure_fetch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", choices=sorted(SOURCES), help="source adapter, e.g. greenhouse")
    parser.add_argument("company", help="company key or slug from config/companies.yaml")
    parser.add_argument(
        "--slug", action="store_true", help="treat COMPANY as an ad-hoc ATS slug not in config"
    )
    parser.add_argument("--dry-run", action="store_true", help="print jobs; don't write to DB")
    parser.add_argument("--limit", type=int, default=50, help="max jobs to print (default 50)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="also print the normalised fields per job"
    )


async def cmd_fetch(settings: Settings, args: argparse.Namespace) -> int:
    """Fetch one company's jobs from one source; print them and optionally store them."""
    if args.slug:
        target = CompanyTarget(
            key=args.company, name=args.company, ats=args.source, slug=args.company
        )
    else:
        companies = load_companies(known_sources=SOURCES)
        target = find_company(companies, args.source, args.company)
        if target is None:
            raise ConfigError(
                f"{args.company!r} is not in config/companies.yaml for {args.source} "
                "(use --slug to try an ad-hoc board)"
            )
    if not args.dry_run:
        settings.require("mongodb_uri")

    async with HttpClient() as http:
        result = await get_source(args.source).fetch(target, http)

    jobs = sorted(
        (normalize_job(job) for job in result.jobs),
        key=lambda j: j.posted_at or j.first_seen_at,
        reverse=True,
    )
    print(
        f"{target.name} ({args.source}:{target.slug}): {len(jobs)} jobs, {result.skipped} skipped"
    )
    for job in jobs[: args.limit]:
        posted = (
            job.posted_at.astimezone(IST).strftime("%Y-%m-%d") if job.posted_at else "????-??-??"
        )
        print(f"  {posted}  {job.location_raw[:28]:<28}  {job.title[:70]}")
        if args.verbose:
            print(describe_normalized(job))
    if len(jobs) > args.limit:
        print(f"  … {len(jobs) - args.limit} more")
    for error in result.errors[:10]:
        print(f"  ! {error}", file=sys.stderr)

    if args.dry_run:
        return 0
    repo = open_repository(settings)
    outcomes: Counter[str] = Counter()
    for job in jobs:
        outcome = repo.upsert_job(job)
        if outcome is UpsertOutcome.UNCHANGED:
            stored = repo.get_job(job.id)
            if stored is None or needs_renormalize(stored):
                repo.save_derived(job.id, job.normalized, job.fingerprint)
        outcomes[outcome.value] += 1
    closed = repo.mark_missing_inactive(args.source, target.key, (j.id for j in jobs))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())) or "nothing"
    print(f"Stored: {summary}; marked inactive: {closed}")
    return 0


def describe_normalized(job: Job) -> str:
    n = job.normalized
    if n is None:
        return "      (not normalised)"
    loc = n.location
    exp = n.experience
    years = (
        "?"
        if exp.min_years is None
        else f"{exp.min_years:g}-{exp.max_years:g}"
        if exp.max_years is not None
        else f"{exp.min_years:g}+"
    )
    salary = (
        f"{n.salary.currency} {n.salary.min:g}{f'-{n.salary.max:g}' if n.salary.max else ''}"
        f" ({n.salary.kind})"
        if n.salary
        else "-"
    )
    return (
        f"      role={n.role_category}/{n.seniority} type={n.employment_type} "
        f"exp={years}{' fresher' if exp.fresher else ''} salary={salary}\n"
        f"      where={','.join(loc.cities) or '-'} countries={','.join(loc.countries) or '-'} "
        f"mode={loc.work_mode.value} remote={loc.remote_scope.value}\n"
        f"      skills={','.join(n.skills[:12]) or '-'}\n"
        f"      fingerprint={job.fingerprint}"
    )


async def cmd_renormalize(settings: Settings, args: argparse.Namespace) -> int:
    """Re-apply the current normalisers to stored jobs (no scraping)."""
    settings.require("mongodb_uri")
    repo = open_repository(settings)
    jobs = repo.list_jobs(active_only=False)
    updated = 0
    for job in jobs:
        if args.all or needs_renormalize(job):
            fresh = normalize_job(job)
            repo.save_derived(job.id, fresh.normalized, fresh.fingerprint)
            updated += 1
    print(f"Re-normalised {updated} of {len(jobs)} stored jobs (normalizer v{NORMALIZER_VERSION}).")
    return 0


def _configure_renormalize(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true", help="redo every job, not just outdated ones")


def _maybe_repo(settings: Settings):
    """Repository if MongoDB is configured, else None (commands fall back to defaults)."""
    return open_repository(settings) if settings.mongodb_uri else None


def _load_prefs(settings: Settings) -> tuple[Preferences, int]:
    return current_preferences(_maybe_repo(settings))


def _configure_rank(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top", type=int, default=15, help="best matches to show")
    parser.add_argument("--bottom", type=int, default=15, help="weakest matches to show")
    parser.add_argument(
        "--near", type=int, default=10, help="best jobs rejected only for a low score"
    )
    parser.add_argument("--company", action="append", help="limit to company key (repeatable)")
    parser.add_argument("--details", action="store_true", help="full breakdown per job")


async def cmd_rank(settings: Settings, args: argparse.Namespace) -> int:
    """Live-fetch the registry, score every job, and print the ranking (no DB writes)."""
    prefs, version = _load_prefs(settings)
    profile = load_profile()
    targets = [c for c in load_companies(known_sources=SOURCES) if c.enabled]
    if args.company:
        targets = [t for t in targets if t.key in args.company]
    async with HttpClient() as http:
        outcomes = await collect(targets, http)

    scored: list[tuple[Job, MatchResult]] = []
    for outcome in outcomes:
        for job in outcome.jobs:
            scored.append((job, match_job(job, prefs, profile, version)))
    copies = Counter(job.fingerprint for job, _ in scored)
    matches = _unique(
        sorted((p for p in scored if p[1].decision == "match"), key=lambda p: -p[1].score)
    )
    near = _unique(
        sorted(
            (p for p in scored if (p[1].reject_reason or "").startswith("score ")),
            key=lambda p: -p[1].score,
        )
    )
    reasons = Counter(_reason_bucket(m.reject_reason) for _, m in scored if m.decision == "reject")

    failed = [o for o in outcomes if not o.ok]
    print(
        f"Fetched {len(scored)} jobs from {len(outcomes) - len(failed)}/{len(outcomes)} companies"
        f" · prefs v{version} · min score {prefs.min_score}"
    )
    for o in failed:
        print(f"  ! {o.target.key}: {o.error}")
    print(
        f"Matches: {len(matches)} unique openings · "
        f"Rejected: {sum(1 for _, m in scored if m.decision == 'reject')} postings"
    )
    for reason, count in reasons.most_common(12):
        print(f"  {count:>5}  {reason}")

    def show(title: str, rows: list[tuple[Job, MatchResult]]) -> None:
        print(f"\n== {title} ==   [role/tech/exp/loc/salary/profile]")
        for job, match in rows:
            dupes = f"  ×{copies[job.fingerprint]}" if copies[job.fingerprint] > 1 else ""
            print(
                (explain_text(job, match, prefs) if args.details else one_line(job, match)) + dupes
            )
            if args.details:
                print()

    show(f"Top {args.top} matches", matches[: args.top])
    if args.bottom:
        show(f"Bottom {args.bottom} matches (closest to the cut-off)", matches[-args.bottom :])
    if args.near:
        show(f"Top {args.near} near-misses (rejected only for score)", near[: args.near])
    return 0


def _unique(rows: list[tuple[Job, MatchResult]]) -> list[tuple[Job, MatchResult]]:
    """One row per fingerprint (same company + title + city = same opening)."""
    seen: set[str] = set()
    out = []
    for job, match in rows:
        if job.fingerprint not in seen:
            seen.add(job.fingerprint)
            out.append((job, match))
    return out


def _reason_bucket(reason: str | None) -> str:
    if not reason:
        return "?"
    if reason.startswith("location "):
        return "location not preferred"
    if reason.startswith("outside India"):
        return "outside India"
    if reason.startswith("needs "):
        return "too much experience required"
    if reason.startswith("score "):
        return "score below minimum"
    if "below your" in reason:
        return "salary below floor"
    return reason


def _configure_scan(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--company", action="append", help="limit to company key (repeatable)")
    parser.add_argument(
        "--no-send", action="store_true", help="store and score only; alerts stay pending"
    )
    parser.add_argument("--trigger", default="manual", help="label recorded with the run")
    parser.add_argument(
        "--force", action="store_true", help="scan every company now, ignoring intervals"
    )
    parser.add_argument("--no-llm", action="store_true", help="skip the optional AI check")


async def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    """Run one full scan: fetch, store, score and alert."""
    settings.require("mongodb_uri")
    if not args.no_send:
        settings.require("telegram_bot_token", "telegram_chat_id")
    repo = open_repository(settings)
    repo.ensure_indexes()
    prefs, version = current_preferences(repo)
    targets = [c for c in load_companies(known_sources=SOURCES) if c.enabled]
    if args.company:
        targets = [t for t in targets if t.key in args.company]

    async with HttpClient() as http:
        common = dict(
            repo=repo,
            http=http,
            targets=targets,
            prefs=prefs,
            prefs_version=version,
            profile=load_profile(),
            trigger=args.trigger,
            force=args.force,
            llm=None if args.no_llm else build_chain(settings),
            llm_run_budget=settings.llm_run_budget,
            llm_daily_budget=settings.llm_daily_budget,
        )
        if args.no_send:
            report = await run_scan(notifier=None, **common)
        else:
            async with TelegramClient(settings.secret("telegram_bot_token")) as tg:
                notifier = TelegramNotifier(tg, settings.telegram_chat_id)
                report = await run_scan(notifier=notifier, **common)
    print(describe_report(report, version))
    if report.sources_due and not report.sources_ok:
        print("Every source failed this run.", file=sys.stderr)
        return 1  # makes the GitHub Actions run fail -> GitHub emails the owner
    return 0


def describe_report(report: ScanReport, prefs_version: int) -> str:
    seconds = (report.finished_at - report.started_at).total_seconds() if report.finished_at else 0
    lines = [
        f"Scan finished in {seconds:.1f}s (prefs v{prefs_version})",
        f"  sources: {report.sources_due} due, {report.sources_ok} ok,"
        f" {len(report.sources_failed)} failed",
        f"  postings: {report.fetched} fetched, {report.skipped_foreign} outside India skipped,"
        f" {report.new} new, {report.changed} changed, {report.closed} closed",
        f"  scoring: {report.scored} scored, {report.new_matches} matched,"
        f" {report.duplicates} duplicate openings collapsed",
        f"  alerts: {report.alerts_sent} sent, {report.alerts_failed} failed (will retry),"
        f" {report.alerts_uncertain} uncertain (not resent), {report.held_paused} held,"
        f" {report.baseline} older than 48h on first scan, {report.digested} sent as digest",
        f"  AI check: {report.llm_calls} calls, {report.llm_applied} applied,"
        f" {report.llm_failed} failed, {report.llm_flipped} decisions changed",
        f"  storage: {report.storage_pct:g}% used, {report.compacted} compacted,"
        f" {report.deleted} deleted",
    ]
    lines += [f"  ! {error}" for error in report.sources_failed]
    return "\n".join(lines)


def _configure_realert(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id", help="e.g. greenhouse:purestorage:8131743")


async def cmd_realert(settings: Settings, args: argparse.Namespace) -> int:
    """Re-send the alert for one stored job (e.g. after a format change)."""
    settings.require("mongodb_uri", "telegram_bot_token", "telegram_chat_id")
    repo = open_repository(settings)
    prefs, version = current_preferences(repo)
    async with TelegramClient(settings.secret("telegram_bot_token")) as tg:
        notifier = TelegramNotifier(tg, settings.telegram_chat_id)
        try:
            message_id = await realert(repo, notifier, args.job_id, prefs, version, load_profile())
        except (LookupError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    print(f"Re-sent {args.job_id} (message_id={message_id}).")
    return 0


def _configure_discover(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("names", nargs="+", help='company names, e.g. "Sarvam AI" Zepto')
    parser.add_argument("--slug", action="append", default=[], help="extra slug to try")


async def cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    """Probe public ATS boards for each company and print registry lines for hits."""
    found_any = False
    async with HttpClient(max_retries=1) as http:
        for name in args.names:
            hits = await discover(name, http, tuple(args.slug))
            if not hits:
                print(f"✗ {name}: no public board found (try --slug, or check its careers page)")
                continue
            found_any = True
            print(f"✓ {name}:")
            for hit in hits:
                print(
                    f"    {hit.ats:<15} {hit.slug:<28} total={hit.total:<4} india={hit.india:<4}"
                    f" india_eng={hit.india_engineering}"
                )
            print(yaml_line(name, hits[0]))
    return 0 if found_any else 1


def _configure_backlog(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--send", action="store_true", help="send the digest to Telegram now")


async def cmd_backlog(settings: Settings, args: argparse.Namespace) -> int:
    """List (or --send) held-back matches: open roles posted before a company was watched."""
    settings.require("mongodb_uri")
    repo = open_repository(settings)
    if not args.send:
        entries = held_back(repo)
        print(f"{len(entries)} held-back openings (unique):")
        for job, match in entries:
            print(f"  {one_line(job, match)}")
        return 0
    settings.require("telegram_bot_token", "telegram_chat_id")
    async with TelegramClient(settings.secret("telegram_bot_token")) as tg:
        sent = await send_held_back(repo, TelegramNotifier(tg, settings.telegram_chat_id))
    print(f"Sent digest covering {sent} openings.")
    return 0


BOT_MENU = [
    ("status", "Health, last scan, today's alerts"),
    ("latest", "Last N alerts"),
    ("today", "Alerts sent today"),
    ("prefs", "Show preferences"),
    ("scan_now", "Run a full scan now"),
    ("pause", "Hold alerts"),
    ("resume", "Resume alerts"),
    ("help", "All commands"),
]


def _configure_set_webhook(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("base_url", help="your Vercel URL, e.g. https://jobbot-xyz.vercel.app")


async def cmd_set_webhook(settings: Settings, args: argparse.Namespace) -> int:
    """Point Telegram at the Vercel webhook and install the bot's command menu."""
    settings.require("telegram_bot_token", "telegram_webhook_secret")
    url = args.base_url.rstrip("/") + "/api/telegram"
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        await client.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": settings.secret("telegram_webhook_secret"),
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            },
        )
        await client.call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in BOT_MENU]},
        )
        info = await client.call("getWebhookInfo")
    print(f"Webhook set to {info.get('url')} (pending updates: {info.get('pending_update_count')})")
    return 0


async def cmd_webhook_info(settings: Settings, args: argparse.Namespace) -> int:
    """Show Telegram's view of the webhook (URL, last error)."""
    settings.require("telegram_bot_token")
    async with TelegramClient(settings.secret("telegram_bot_token")) as client:
        info = await client.call("getWebhookInfo")
    for key in ("url", "pending_update_count", "last_error_date", "last_error_message"):
        print(f"{key}: {info.get(key)}")
    return 0


def _configure_explain(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id", help="e.g. greenhouse:mongodb:1234567")


async def cmd_explain(settings: Settings, args: argparse.Namespace) -> int:
    """Show the full score breakdown for one job (from the DB, else fetched live)."""
    prefs, version = _load_prefs(settings)
    repo = _maybe_repo(settings)
    job = repo.get_job(args.job_id) if repo else None
    if job is None:
        source, _, rest = args.job_id.partition(":")
        company_key = rest.partition(":")[0]
        target = find_company(load_companies(known_sources=SOURCES), source, company_key)
        if target is None:
            raise ConfigError(f"unknown company in job id {args.job_id!r}")
        async with HttpClient() as http:
            (outcome,) = await collect([target], http)
        if outcome.error:
            raise FetchError(target.slug, outcome.error)
        job = next((j for j in outcome.jobs if j.id == args.job_id), None)
        if job is None:
            print(f"Job {args.job_id} not found (it may have been closed).", file=sys.stderr)
            return 1
    elif job.normalized is None:
        job = normalize_job(job)
    match = match_job(job, prefs, load_profile(), version)
    print(explain_text(job, match, prefs))
    print(describe_normalized(job))
    return 0


def _configure_prefs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed", action="store_true", help="store config/defaults.yaml as version 1 if unset"
    )


async def cmd_prefs(settings: Settings, args: argparse.Namespace) -> int:
    """Show the active preferences (or seed them into the DB)."""
    repo = _maybe_repo(settings)
    if args.seed:
        settings.require("mongodb_uri")
        if repo.get_preferences() is not None:
            print("Preferences already stored; not overwriting.")
        else:
            prefs, _ = current_preferences(None)
            doc = repo.save_preferences(prefs.model_dump(), expected_version=0, note="seed")
            print(f"Seeded preferences from config/defaults.yaml (version {doc.version}).")
    prefs, version = current_preferences(repo)
    origin = "database" if version else "config/defaults.yaml (not seeded)"
    print(f"Preferences v{version} from {origin}:")
    for key, value in prefs.model_dump().items():
        print(f"  {key}: {value}")
    return 0


COMMANDS: dict[str, Command] = {
    "ping": Command(cmd_ping, "Send a test message to your Telegram chat"),
    "whoami": Command(cmd_whoami, "Validate the bot token (getMe)"),
    "chat-id": Command(cmd_chat_id, "Find your TELEGRAM_CHAT_ID (message the bot first)"),
    "db-init": Command(cmd_db_init, "Check MongoDB connectivity and create indexes"),
    "fetch": Command(cmd_fetch, "Fetch one company's jobs from one source", _configure_fetch),
    "scan": Command(cmd_scan, "Run one scan: fetch, store, score and alert", _configure_scan),
    "realert": Command(cmd_realert, "Re-send the alert for one job", _configure_realert),
    "discover": Command(cmd_discover, "Find a company's public ATS board(s)", _configure_discover),
    "backlog": Command(cmd_backlog, "List or --send held-back matches", _configure_backlog),
    "set-webhook": Command(
        cmd_set_webhook, "Point Telegram at the Vercel webhook", _configure_set_webhook
    ),
    "webhook-info": Command(cmd_webhook_info, "Show Telegram webhook status"),
    "rank": Command(cmd_rank, "Score live jobs from all companies and rank them", _configure_rank),
    "explain": Command(cmd_explain, "Full score breakdown for one job", _configure_explain),
    "prefs": Command(cmd_prefs, "Show (or --seed) job preferences", _configure_prefs),
    "renormalize": Command(
        cmd_renormalize, "Re-apply normalisers to stored jobs", _configure_renormalize
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobbot", description="Personal job discovery bot")
    parser.add_argument("--version", action="version", version=f"jobbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, command in COMMANDS.items():
        command_parser = sub.add_parser(name, help=command.help)
        if command.configure:
            command.configure(command_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level)
    command = COMMANDS[args.command]
    try:
        return asyncio.run(command.handler(settings, args))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1
    except FetchError as exc:
        print(f"Fetch error: {exc}", file=sys.stderr)
        return 1
    except PyMongoError as exc:
        print(f"MongoDB error: {redact(str(exc))[:300]}", file=sys.stderr)
        return 1
