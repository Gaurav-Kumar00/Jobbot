# JobBot

A personal job-discovery bot. It looks for newly posted backend/SWE jobs, scores them against my resume and preferences, removes duplicates, and sends each relevant job to Telegram once. It runs on free infrastructure: GitHub Actions for scanning, MongoDB Atlas M0 for storage, and Vercel for bot commands.

> Status: **Phase 10 — optional AI check** (181 sources, hourly scans, Vercel bot, watchdog, Groq-assisted matching).

## Quick start

```bash
make install          # uv sync (Python 3.12 via uv)
cp .env.example .env  # then fill in values (see below)
make test             # full test suite, prints pass/fail
make test-mongo       # same, plus storage contract tests against your Atlas DB
uv run jobbot ping    # sends a test message to your Telegram
```

## Telegram setup (one time)

1. In Telegram, open **@BotFather** and send `/newbot`. Pick a name and a username ending in `bot`.
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
3. Open your new bot and send it any message, e.g. `/start`.
4. Run `uv run jobbot chat-id`. Copy the id of your **private** chat into `.env` as `TELEGRAM_CHAT_ID`.
5. Run `uv run jobbot ping`. You should get "✅ JobBot is alive".

## MongoDB Atlas setup (one time)

1. Security → Database Access: create a user with `readWrite` on database `jobbot`.
2. Security → Network Access: add `0.0.0.0/0`. GitHub Actions and Vercel connect from changing IPs, so the strong password is what protects the database.
3. Put the `mongodb+srv://...` connection string in `.env` as `MONGODB_URI`, then run `uv run jobbot db-init`.

## Commands

| Command | Purpose |
|---|---|
| `jobbot ping` | Send a test message to your chat |
| `jobbot whoami` | Check the bot token (Telegram `getMe`) |
| `jobbot chat-id` | List chats that messaged the bot (local use only) |
| `jobbot db-init` | Check MongoDB connectivity, create indexes, show storage used |
| `jobbot fetch greenhouse razorpay --dry-run` | Fetch one company's live jobs and print them (drop `--dry-run` to store them) |
| `jobbot fetch greenhouse <slug> --slug --dry-run` | Try a board that isn't in `config/companies.yaml` yet |
| `jobbot fetch ... -v` | Also show normalised fields (role, experience, salary, location, skills) |
| `jobbot renormalize [--all]` | Re-apply normalisers to stored jobs without scraping |
| `jobbot scan [--no-send] [--company K]` | One full scan: fetch, store, score, alert (`--no-send` keeps alerts pending) |
| `jobbot rank [--top N --bottom N --near N --details]` | Live-score every registered company and show the ranking |
| `jobbot explain <job_id>` | Full score breakdown for one job |
| `jobbot backlog [--send]` | List or send held-back matches as a digest |
| `jobbot discover <name>` | Find a company's public ATS board |
| `jobbot set-webhook <url>`, `jobbot webhook-info` | Telegram webhook setup and status |
| `jobbot prefs [--seed]` | Show active preferences (or store `config/defaults.yaml` in the DB) |

## Telegram commands (Vercel)

The bot answers only `TELEGRAM_CHAT_ID`. Every webhook request must carry Telegram's secret header (`TELEGRAM_WEBHOOK_SECRET`).

| Command | What it does |
|---|---|
| `/status`, `/sources` | Health, last scan, failing sources |
| `/latest [n]`, `/today`, `/history [days]` | Recent alerts |
| `/job <id>`, `/realert <id>` | Score breakdown; send an alert again |
| `/pause`, `/resume` | Hold or release alerts (scanning continues) |
| `/scan_now` (`/rematch`) | Full scan now with current preferences |
| `/prefs` | Show preferences |
| `/set_salary`, `/set_exp`, `/set_min_score`, `/set_remote` | Edit preferences |
| `/location`, `/skill`, `/role` | Add or remove cities, skills, preferred titles |
| `/set_interval` | Change one source's scan cadence |

Preference edits are versioned, and the next scan re-scores stored jobs without re-scraping.

### Deploy

1. Import the repo into Vercel and set these env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MONGODB_URI`, `MONGODB_DB`, `TELEGRAM_WEBHOOK_SECRET`, `CRON_SECRET`, `GH_DISPATCH_TOKEN`, `GITHUB_REPOSITORY`.
2. Run `uv run jobbot set-webhook https://<your-app>.vercel.app`.
3. On cron-job.org, call `GET https://<your-app>.vercel.app/api/tick` every 15–60 minutes with header `X-Cron-Secret: <CRON_SECRET>`. It starts a scan when GitHub's scheduler skipped one (and none is running). It also warns on Telegram once if no scan has succeeded for 3 hours.
4. `GH_DISPATCH_TOKEN` is a fine-grained token for this repo only, with **Actions: Read and write**.

## Matching

Every job first goes through hard filters, which always record a reason:
- role family, seniority and employment type;
- more than `max_min_years` of experience required;
- location not in your cities, outside India, or remote but restricted to other countries;
- stated pay below the floor.

The remaining jobs get a score out of 100:

| Component | Max points |
|---|---|
| Role | 25 |
| Technology | 25 |
| Experience | 20 |
| Location | 10 |
| Salary | 10 |
| Overlap with resume skills (`config/profile.yaml`) | 10 |

Jobs scoring at least `min_score` match. Weights and thresholds are preferences, so they can be changed without code.

## Alerts

- **One alert per opening.** Postings that share company, title and city are collapsed into one, including the same job reposted later under a new id.
- **Reserved before sending.** Each alert is recorded in the database before it goes out. If the bot crashes mid-send, or the network times out after Telegram may have received the message, it is never resent.
- **Retries.** Failures where Telegram definitely didn't get the message are retried on the next scan.
- **Pausing.** While alerts are paused, matches are held and delivered on resume.

## Optional AI check

With `GROQ_API_KEY` (or `GROQ_TOKEN`) set, each scan sends would-be alerts and near-misses to a free LLM: Groq `gpt-oss-20b`, then `gpt-oss-120b`, then Gemini if `GEMINI_API_KEY` is set.

**What it does:**
- It only **fills gaps**: unstated experience, whether the role is backend or something else (only for vague titles like "Software Engineer" or "Full Stack"), India-eligibility for remote roles, and base vs CTC.
- It writes the alert's 2-line summary.
- Numbers stated in the posting always win over the AI's reading.

**Limits and failure handling:**
- Each posting is checked once; the answer is cached by content for 90 days.
- Budget is `LLM_RUN_BUDGET` (15) calls per scan and `LLM_DAILY_BUDGET` (100) per day.
- If the AI is down or out of quota, scans behave exactly as without it.
- `jobbot scan --no-llm` skips it.

## Vocabulary

Cities, regions, countries and skills live in `config/locations.yaml` and `config/taxonomy.yaml`, so new ones can be added without code changes. After changing normaliser logic, bump `NORMALIZER_VERSION` and run `jobbot renormalize`.

## Adding a company

Add an entry to `config/companies.yaml` (`key`, `name`, `ats`, `slug`), then check it with `jobbot fetch <ats> <key> --dry-run`.

## Security

- Secrets come only from environment variables (`.env` locally, GitHub Secrets / Vercel env in production).
- Every log line goes through a redactor that strips bot tokens, connection-string passwords and API keys. Actions logs in a public repo are public.
- `.gitignore` excludes `.env` and all PDFs, so the resume is never committed.
