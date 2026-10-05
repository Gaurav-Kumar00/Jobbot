# JobBot — Developer Guide

JobBot is a personal job-discovery system. Every hour it reads the public job boards of about 180 companies plus a handful of aggregators, normalises each posting into comparable fields, scores it against a stored set of preferences, removes duplicates, and sends each genuinely relevant opening to Telegram exactly once. It runs entirely on free tiers, so nothing depends on a laptop being switched on.

This guide is for anyone changing the code. For day-to-day operations (what to do when something breaks, rotating keys, quotas) see [`RUNBOOK.md`](RUNBOOK.md).

---

## 1. System overview

```
                     ┌──────────────────────── GitHub Actions (public repo) ───────────────────────┐
cron "17 * * * *" ──►│  scan.yml → `jobbot scan`                                                    │
workflow_dispatch ──►│   collect → normalise → store → score → AI check → dedupe → alert → health  │
                     └───────────────┬─────────────────────────────────────────────▲───────────────┘
                                     │ reads/writes                                 │ workflow_dispatch
                                     ▼                                              │ (GitHub API)
                          ┌────────────────────┐                     ┌──────────────┴───────────────┐
                          │  MongoDB Atlas M0  │◄─── reads/writes ───│  Vercel (FastAPI, serverless)│
                          │  jobs, matches,    │                     │  /api/telegram  (commands)   │
                          │  alerts, prefs, …  │                     │  /api/tick      (cron, 15m)  │
                          └────────────────────┘                     │  /api/health                 │
                                     ▲                               └──────────────▲───────────────┘
                                     │                                              │
  Job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Keka,          cron-job.org ──┤
  Amazon, Microsoft, Atlassian, Unstop, Adzuna, Google Jobs via SerpApi)         Telegram ──────┘
                                     │
                                     └──► Groq LLM (optional AI check)      alerts ──► Telegram (you)
```

There are two runtimes, one database, and one Python codebase:

- **The scanner** runs on GitHub Actions in a public repo, which gives it unlimited free minutes. It's a batch job: fetch everything that's due, process it, send alerts, exit.
- **The bot** runs on Vercel's free Hobby plan as a FastAPI app. It is stateless and answers Telegram commands. It also serves `/api/tick`, which an external cron (cron-job.org) calls every 15 minutes. That gives scanning a reliable hourly cadence even when GitHub's scheduler skips runs, and doubles as a watchdog.
- **The database** is MongoDB Atlas M0 (512 MB, free). All state lives there: jobs, matches, alert records, preferences, source health, run history and the AI cache.

Why this shape? The research behind it is summarised in [§12](#12-design-decisions-and-trade-offs). In short, it was the only free combination that offers hourly batch compute, an always-on webhook and a persistent store, without any machine of yours being online.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Matches the owner's stack; good async HTTP and data tooling |
| Packaging | **uv** (`pyproject.toml`, `uv.lock`), **hatchling** build | Fast, reproducible installs locally, in CI and on Vercel |
| HTTP | **httpx** (async) | One client for all sources; timeouts, retries and connection pooling |
| Models & config | **Pydantic v2**, **pydantic-settings** | Typed domain models; settings from env/`.env`; `SecretStr` keeps secrets out of logs |
| Database | **MongoDB Atlas M0** via **pymongo** | Flexible schema for heterogeneous job payloads; free 512 MB; works from Actions and Vercel |
| Web | **FastAPI** on **Vercel** Python runtime | Tiny serverless webhook; same codebase as the scanner |
| Telegram | Bot API called directly with httpx | A framework is unnecessary for one webhook and `sendMessage` |
| AI (optional) | **Groq** `openai/gpt-oss-20b` → `gpt-oss-120b`, Gemini fallback, OpenAI-compatible API | Free, fast, strict JSON schema output; swapping providers is just configuration |
| Config data | **YAML** (`config/*.yaml`) | Companies, cities and skills can be changed without code |
| Tests | **pytest**, pytest-asyncio, **respx** (HTTP mocks), FastAPI `TestClient` | Fast offline tests; recorded real API responses as fixtures |
| Lint/format | **ruff** | One tool for both; enforced in CI |
| CI/CD | GitHub Actions (`ci.yml`), Vercel Git integration | Push to `main` runs the tests and redeploys the bot |

The runtime dependencies are deliberately few: `fastapi`, `httpx`, `pydantic`, `pydantic-settings`, `pymongo` and `pyyaml`.

---

## 3. Repository layout

```
Job-hunt/
├── api/index.py                 Vercel entry point: exposes the FastAPI app
├── config/                      Data that changes without code
│   ├── companies.yaml           Registry: 183 sources (company + ATS + slug + interval/options)
│   ├── defaults.yaml            Seed preferences (copied into MongoDB on first edit)
│   ├── profile.yaml             Resume-backed skills (no personal info)
│   ├── locations.yaml           Cities, NCR region, countries, US states, remote regions
│   └── taxonomy.yaml            ~70 skills and their aliases
├── src/jobbot/
│   ├── cli.py                   `jobbot <command>`: every operator entry point
│   ├── settings.py              Env/.env config (SecretStr for secrets)
│   ├── models.py                Domain models: Job, Normalized, MatchResult, AlertRecord, …
│   ├── config.py                Loads companies.yaml (validated)
│   ├── http.py                  Shared HTTP client: timeouts, retries, backoff, per-host limits
│   ├── log.py                   JSON logs with secret redaction
│   ├── timeutil.py              UTC/IST helpers
│   ├── sources/                 One module per job source (adapters)
│   │   ├── base.py              Source protocol, build_job(), helpers
│   │   ├── greenhouse.py lever.py ashby.py smartrecruiters.py workable.py keka.py
│   │   ├── amazon.py microsoft.py atlassian.py           Big-tech careers APIs
│   │   └── unstop.py adzuna.py serpapi.py                Job boards / aggregators
│   ├── normalize/               Raw posting → comparable fields (pure functions)
│   │   ├── location.py experience.py salary.py role.py skills.py
│   │   ├── dedup.py             Fingerprints and canonical-copy choice
│   │   ├── text.py summary.py vocab.py
│   │   └── __init__.py          normalize_job(), NORMALIZER_VERSION
│   ├── matching/                Preferences, hard filters, 100-point scorer, explanations
│   ├── llm/                     Optional AI check: client, provider chain, enrichment
│   ├── pipeline/                Orchestration
│   │   ├── collect.py           Concurrent fetch with per-source isolation
│   │   ├── scan.py              The scan (see §5) and realert()
│   │   ├── digest.py            Held-back matches as compact digests
│   │   ├── housekeeping.py      Storage safety net (compaction, retention, guard)
│   │   └── discover.py          Probe which ATS a company uses
│   ├── notify/                  Notifier protocol, Telegram client, alert formatter
│   ├── storage/                 Repository protocol, MongoDB and in-memory implementations
│   └── bot/                     Vercel app: webhook, commands, hourly tick, GitHub dispatch
├── tests/
│   ├── unit/                    Parsers, scorer, adapters, formatter, CLI, …
│   ├── contract/                Repository contract tests (memory + real MongoDB)
│   ├── pipeline/                End-to-end scans with fake sources and notifiers
│   ├── llm/ bot/                AI layer; webhook, commands, watchdog
│   └── fixtures/                Recorded real API responses per source
├── .github/workflows/           ci.yml, scan.yml (hourly), keepalive.yml
├── vercel.json requirements.txt .vercelignore
├── docs/                        This guide and the runbook
└── pyproject.toml uv.lock Makefile .env.example .gitignore
```

### Layering rules

Dependencies point downwards only:

```
cli / bot ─► pipeline ─► matching, llm, notify ─► normalize ─► models
                 │                                    ▲
                 └─────────► storage (Repository) ────┘   sources ─► http, normalize.text
```

- Everything above `storage/` talks only to the **`Repository` protocol**, never to pymongo directly. Tests use `MemoryRepository`.
- `normalize/` and `matching/` are **pure**: no I/O. That keeps them trivially testable and lets stored jobs be re-scored without re-scraping.
- `sources/` only know how to turn one API response into `Job`s. They know nothing about storage or matching.

---

## 4. Data model

The models are in `models.py` and the collections are in MongoDB database `jobbot`.

| Collection | `_id` | Contents |
|---|---|---|
| `jobs` | `{source}:{company}:{native_id}` | Title, url, `location_raw`, description (≤8 KB), `posted_at`, `first_seen_at`/`last_seen_at`, `is_active`, `content_hash`, `fingerprint`, `duplicate_of`, `hints` (structured fields from the source), `normalized` (see below), `raw` (trimmed payload), `compacted` |
| `matches` | job id | `score`, `decision` (`match`/`reject`), `reject_reason`, per-component `breakdown`, `details`, `reasons`, `prefs_version` |
| `alerts` | job id | `status` (`sending`/`sent`/`failed`/`baseline`), `trigger`, `fingerprint`, `message_id`, `sent_at`, `attempts` |
| `preferences` | `"owner"` | Versioned preference document; `preference_history` keeps every version |
| `source_state` | `{ats}:{company}` | `bootstrapped`, `interval_minutes`, last run/success, `consecutive_failures`, `last_error`, `failure_notified` |
| `runs` | auto | Per-scan stats and errors (TTL 30 days) |
| `llm_cache` | `{task_version}:{content_hash}` | Cached `AIInsight` (TTL 90 days) |
| `meta` | key | Small state: storage warnings, watchdog state, LLM daily usage |

**`Normalized`** is everything derived from a posting:
- `location`: cities, regions (e.g. `ncr`), countries, `work_mode`, `remote_scope`;
- `experience`: min/max years, fresher flag and evidence;
- `salary`: currency, min/max in LPA, base/ctc/unspecified, period;
- `role_category`, `seniority`, `employment_type`, `skills`, `title_skills`;
- optional `ai` (an `AIInsight`).

It carries a `version`; bump `NORMALIZER_VERSION` when extraction logic changes.

---

## 5. How a scan works

The code is in `pipeline/scan.py::run_scan`. The steps:

1. **Due sources.** Each source has an interval: 60 minutes by default, overridable per company in `companies.yaml` or from Telegram. A source is due if its last run is older than that, with 10 minutes of grace because GitHub cron runs late.
2. **Collect** (`collect.py`). Fetch all due sources concurrently, 8 at a time and at most 4 at a time per host.
   - Each source is isolated. An HTTP failure, a crash in an adapter, or a single malformed posting affects only that source or that posting.
   - Adapters must label jobs with their own ATS name, because closing stale postings depends on it.
3. **Normalise** each posting and **re-apply cached AI insights** (one cache lookup per run).
4. **Store** (`upsert_jobs`, one batch per source). This yields NEW, CHANGED (content hash differs) or UNCHANGED for each posting.
   - Postings clearly outside India are not stored.
   - Postings missing from a *complete, successful* fetch are marked inactive.
5. **First-scan backfill.** For a company scanned for the first time, matching postings older than 48 hours are recorded as `baseline` rather than alerted. They are later sent as one digest (step 8).
6. **Score** (`matching/scorer.py`). New or changed jobs, plus any job whose match was computed under an older preferences version, get a fresh `MatchResult`.
7. **AI check** (optional; `llm/enrich.py`). Would-be alerts and near-misses that haven't been checked or alerted go to the LLM (within budget). Insights fill gaps, and the affected jobs are re-scored.
8. **Deliver** (`deliver_pending`):
   - Take every matched, active, not-yet-alerted job.
   - Collapse postings that share a fingerprint into one opening; the official ATS copy wins.
   - Skip openings already alerted under another id.
   - For each remaining job, **claim the alert in the database first, then send**. Afterwards, send held-back matches as a digest.
9. **Health.** Update `source_state`. After 3 consecutive failures send one ⚠️ warning, and a ✅ message on recovery.
10. **Housekeeping.** Compaction, retention and the storage guard (§7), then record the run.

### The no-duplicates guarantee

`Repository.claim_alert` atomically inserts an alert record with status `sending` before the Telegram call:

- **Sent:** the record becomes `sent`.
- **Definitely not sent** (an API error, or a connection that never happened): the record becomes `failed` and is retried on the next scan.
- **Possibly sent** (timeout after the request left, or a crash): the record stays `sending` and is never retried. Missing one alert is preferred over sending it twice.
- **Openings:** `fingerprint = company|title|city` collapses reposts and cross-source copies.

`tests/pipeline/test_scan.py` covers each of these cases.

---

## 6. Normalisation and matching

### Normalisers (`normalize/`)

All are pure functions with table-driven tests (`tests/unit/test_*.py`).

- **Location:** aliases (Bangalore = Bengaluru, Gurgaon = Gurugram, NCR = Delhi/Gurugram/Noida/…), Indian states, countries, US states, and remote eligibility (`india`/`global`/`apac`/`restricted`/`unknown`). If the location field is empty, Indian cities named in the description are used. Country codes in YAML are quoted, because an unquoted `NO:` parses as `false`.
- **Experience:** ranges, `N+`, "at least N", months, and fresher signals ("2026 batch", "new grad", GET). The first range wins; otherwise the strictest minimum.
- **Salary:** LPA, lakhs, `₹12,00,000`, monthly to annual, and USD. Base, CTC and variable are told apart. Insurance cover, funding amounts and bonuses are ignored.
- **Role:** ordered regex families (non-software → management → … → generic), plus seniority and employment type. An explicit contract or intern title beats a generic "full-time" field.
- **Skills:** a phrase matcher over `taxonomy.yaml` that respects word boundaries (`c++`, `node.js`, no "java" inside "javascript"). Django, DRF, FastAPI, Flask and Celery imply Python.
- **Dedup:** normalised company (legal suffixes stripped), title (synonyms such as sde → software engineer, sr → senior) and primary city.

### Matching (`matching/`)

- **Preferences** (`preferences.py`) are a Pydantic model. They're seeded from `config/defaults.yaml`, then live in MongoDB with a version, and Telegram commands edit them.
- **Hard filters** always record a reason:
  - duplicate;
  - employment type;
  - excluded role family or seniority;
  - Python required but the stack lists only other languages;
  - more than `max_min_years` of experience required;
  - location not preferred, outside India, restricted remote, or unknown;
  - stated pay below the floor.
- **Score out of 100** from six components:

  | Component | Weight |
  |---|---|
  | Role | 25 |
  | Tech | 25 |
  | Experience | 20 |
  | Location | 10 |
  | Salary | 10 |
  | Profile overlap | 10 |

  Each component gives a 0–1 fraction plus a human-readable detail, and the fractions are scaled by the weights in `Preferences.weights`. Missing information gives a neutral fraction and is never shown as a reason.
- **Tiers:** 🔥 ≥80, ✅ ≥65, 🟡 ≥`min_score` (55).
- `jobbot explain <id>` and `/job <id>` show the full breakdown.

### AI check (`llm/`)

- `client.py`: one OpenAI-compatible `Provider` with a strict JSON schema. Responses are validated by Pydantic (`AIInsight`).
- `chain.py`: an ordered fallback. A provider that runs out of quota is skipped for the rest of the run.
- `enrich.py`:
  - **Candidate selection:** would-be alerts and near-misses within 12 points of `min_score`.
  - **Budgets:** per scan and per day (stored in `meta`).
  - **Cache** keyed by content hash.
  - **`apply_insight` rules:** fill only unknowns, refine only vague role labels, never override numbers stated in the posting.

  Bump `TASK_VERSION` when the prompt changes.

---

## 7. Storage safety net

The code is in `pipeline/housekeeping.py`; it runs after every scan.

- **Compaction:** inactive jobs lose description and raw data (identity and fingerprint are kept) after 7 days if they were rejected and never alerted, otherwise after 30.
- **Retention:** jobs inactive for 180 days that never matched or alerted are deleted.
- **Guard:**
  - at 70% full, send a Telegram warning (at most once a day);
  - at 85%, compact every inactive job;
  - at 95%, also compact active rejected jobs.

  Alerting is never blocked.

In steady state a full job is about 7 KB and a compacted one about 0.5 KB.

---

## 8. The bot (Vercel)

`bot/app.py` is a FastAPI app with three routes:

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/telegram` | `X-Telegram-Bot-Api-Secret-Token` must equal `TELEGRAM_WEBHOOK_SECRET` | Telegram webhook. Only `TELEGRAM_CHAT_ID` gets replies. It always returns 200 so Telegram never retry-storms, and handler crashes produce an apology message instead. |
| `GET /api/tick` | `X-Cron-Secret` header (or `?key=`) = `CRON_SECRET` | Called by cron-job.org. Dispatches a scan if none has started in 50 minutes and none is queued or running, and warns once if nothing has succeeded for 3 hours. |
| `GET /api/health` | none | Liveness check |

- **Commands** (`bot/commands.py`) are plain async functions `(BotContext, args) -> list[OutgoingMessage]`. They're registered in `COMMANDS` and grouped for `/help` in `SECTIONS`.
- **Preference edits** go through `_update()`, which saves a new version with an optimistic-concurrency check. The next scan re-scores everything automatically.
- **Vercel notes:**
  - `vercel.json` must **not** rewrite `/api/(.*)` to `/api/index`. That rewrite replaces the request path, and every route then returns 404.
  - `includeFiles` ships `src/` and `config/` with the function.

---

## 9. Development workflow

```bash
make install          # uv sync (dev deps included)
cp .env.example .env  # fill in what you need; tests never read it
make test             # full offline suite (~700 tests, a few seconds)
make test-mongo       # + repository contract tests against MONGODB_URI (Atlas)
make lint             # ruff check + format check (CI enforces)
make fmt              # auto-fix

uv run jobbot fetch lever cred --dry-run -v   # one source, normalised fields
uv run jobbot rank --top 20                   # live ranking, no DB writes
uv run jobbot scan --no-send                  # store + score, keep alerts pending
uv run jobbot explain lever:cred:<id>         # why a job scored what it did
```

The tests isolate themselves:
- `tests/conftest.py` clears env vars and switches to a temp directory, so a real `.env` is never read;
- HTTP is mocked with respx;
- storage uses `MemoryRepository`. Contract tests also run against a real MongoDB when `MONGODB_TEST_URI` is set (CI uses a service container) or when `JOBBOT_TEST_MONGO=1` is set (Atlas, via `.env`).

### Recipes

**Add a job source** (for example a new ATS):
1. Create `src/jobbot/sources/<name>.py` with a class whose `name = "<name>"` and whose `async fetch(target, http) -> FetchResult` builds jobs via `build_job(source=self.name, …)`.
2. Put structured fields (workplace, employment, salary text) into `job.hints`.
3. Register the class in `sources/__init__.py`. If it's an aggregator, add its precedence in `normalize/dedup.py` and a label in `notify/formatter.py`.
4. Record a trimmed real response under `tests/fixtures/<name>/` and add tests to `tests/unit/test_ats_sources.py`. Cover a recorded fixture, field mapping, malformed items and a bad response shape.
5. Add companies to `config/companies.yaml`. `uv run jobbot discover "Company"` finds slugs on supported ATSes.

**Add a company:** add one line to `config/companies.yaml`, verify it with `jobbot fetch <ats> <key> --dry-run`, then push. Its first scan backfills 48 hours, and older matches arrive as a digest.

**Add a city or skill:** edit `config/locations.yaml` or `config/taxonomy.yaml`. Bump `NORMALIZER_VERSION` if needed, and add a row to the relevant test table.

**Add a Telegram command:** write `async def cmd_x(ctx, args) -> Reply` in `bot/commands.py`, register it in `COMMANDS` and `SECTIONS`, add a test in `tests/bot/test_commands.py`, and optionally add it to `BOT_MENU` in `cli.py`. Then run `jobbot set-webhook` again to refresh the menu.

**Change scoring:** edit `matching/scorer.py`, add or adjust cases in `tests/unit/test_scorer.py`, and check the effect on real data with `jobbot rank`.

### Conventions

- **Secrets** are only ever read through `Settings` (`SecretStr`) and never logged; `log.py` redacts tokens, connection-string passwords and API keys. The repo and its Actions logs are public, so log at WARNING in production and never log the chat id or preferences.
- **Fake credentials in tests** are built at runtime (`tests/conftest.py::fake_mongo_uri`), so secret scanners don't flag fixtures.
- **Retries:** external calls go through `HttpClient`. It retries 408/425/429/5xx and network errors with exponential backoff, honours `Retry-After`, and treats `Retry-After: 0` as "use backoff".
- **Versions:** behaviour changes that affect stored derived data bump a version: `NORMALIZER_VERSION`, `TASK_VERSION`, or the preferences version.

---

## 10. CI/CD and deployment

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push / PR | `uv sync --locked`, ruff, pytest (with a MongoDB service container) |
| `scan.yml` | cron `17 * * * *`, `workflow_dispatch` (input `force`) | `jobbot scan`. Secrets come from repo secrets; `concurrency: scan` prevents overlap. |
| `keepalive.yml` | 1st and 15th of each month | Re-enables scheduled workflows (GitHub disables them after 60 idle days) |

**Vercel** redeploys on every push to `main`. **cron-job.org** calls `/api/tick` every 15 minutes.

**GitHub secrets:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MONGODB_URI`, `MONGODB_DB`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `SERPAPI_KEY`, `GROQ_API_KEY`.

**Vercel env:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MONGODB_URI`, `MONGODB_DB`, `TELEGRAM_WEBHOOK_SECRET`, `CRON_SECRET`, `GH_DISPATCH_TOKEN`, `GITHUB_REPOSITORY`, `LOG_LEVEL`.

---

## 11. Build history (what was done, and what each phase taught us)

The system was built in gated phases. Each one was implemented, tested and verified live before the next began.

| Phase | Built | Lessons from real data |
|---|---|---|
| 0 Research | Free-tier audit of hosting, databases, schedulers and LLMs; legal source inventory | Vercel cron runs only daily, Cloudflare Workers allow only 50 subrequests, Render/Fly/Railway have no usable free tier, PythonAnywhere blocks outbound requests, and Oracle quietly halved its free tier. That led to Actions + Vercel + Atlas. |
| 1 Foundation | Settings, redacting logs, Telegram client, CLI, CI | Never let the bot token reach logs; httpx logs full URLs at INFO |
| 2 Storage | `Repository` protocol, MongoDB + memory, contract tests, atomic alert claims | Atlas IP allowlisting breaks on mobile networks, so allow `0.0.0.0/0` and rely on a strong password |
| 3 First source | HTTP layer, Greenhouse adapter, company registry | Greenhouse returns HTML that has itself been entity-escaped |
| 4 Normalisation | Location, experience, salary, role and skills parsers; fingerprints | Real titles ("SDE in Test II", "Intermediate", "MTS") needed extra rules; legacy documents must load safely |
| 5 Matching | Preferences, hard filters, 100-point scorer, `rank`/`explain` | Unknown foreign locations slipped through until "no recognisable India location" became a reject; kernel/C++ roles needed their own category; reposted openings needed collapsing |
| 6 Alerts | Scan pipeline, formatter, claim-before-send, batch DB operations | One-at-a-time database calls would have taken about 10 minutes per scan; batching cut it to about 35 seconds |
| 7 Go live | Hourly Actions, per-source intervals, 48-hour backfill, health warnings, storage safety net, keepalive | GitHub flagged a *fake* test credential as a leaked secret, so fixtures are now built at runtime |
| 9 Sources | Lever, Ashby, SmartRecruiters, Workable, Keka, Amazon, Microsoft, Atlassian, Unstop, Adzuna, Google Jobs; `discover`; 183 registry entries; held-back digests | One Norwegian posting crashed a whole scan (YAML `NO` → `false`) and exposed that normalisation ran outside error isolation. Workable sends `Retry-After: 0`. Lever slugs are case-sensitive. LinkedIn, Naukri and Indeed forbid scraping, so their listings come via Google Jobs instead. |
| 8 Bot | Vercel FastAPI webhook, 22 commands, hourly tick + watchdog, GitHub dispatch | A Vercel rewrite replaced every path (404s); the fine-grained token had zero permissions; GitHub's scheduler went 3–7 hours between runs, which is why the tick exists |
| 10 AI | Groq-backed gap filling and summaries, cache, budgets, fallback | The first prompt labelled Snowflake and quant roles "backend"; tighter definitions fixed it |
| 11 Wrap-up | This guide, the runbook, a security review | — |

Phase 9 was done before Phase 8 on purpose: coverage mattered more than commands.

---

## 12. Design decisions and trade-offs

- **Batch scanner + serverless bot, rather than one always-on server.** No free tier offers an always-on VM that won't be reclaimed or put to sleep. Batch jobs plus a stateless webhook fit the free tiers exactly.
- **MongoDB, not Postgres or SQLite.** The owner already had Atlas. Job payloads vary a lot between sources, and both runtimes need the same store. SQLite in a public repo would publish the data.
- **Deterministic matching first, AI second.** The rules are explainable, testable and free. The AI only fills gaps, is budgeted and cached, and has no power to override stated facts.
- **Prefer missing over duplicating.** Alerts are claimed before sending, and uncertain sends are never retried.
- **Never scrape around protections.** Every source is a public API, feed or an aggregator API. Sites that forbid automation (LinkedIn, Naukri, Indeed, Instahyre, Wellfound) are reached only through Google Jobs or the user's own alert emails.
- **Public repo:** unlimited Actions minutes in exchange for public code and logs, so secrets live only in GitHub and Vercel secrets and logs stay minimal.

**Known limits:**
- GitHub cron drifts; the tick covers for it.
- Many Indian product companies run custom career sites with no public feed (Flipkart, PhonePe, Zepto, …).
- Salary is rarely disclosed.
- Free-tier terms can change. The `Repository`, `Notifier` and `Provider` abstractions keep migrations small.
