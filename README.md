# JobBot

A personal job-discovery bot. It looks for newly posted backend/SWE jobs, scores them against my resume and preferences, removes duplicates, and sends each relevant job to Telegram once. It runs on free infrastructure: GitHub Actions for scanning, MongoDB Atlas M0 for storage, and Vercel for bot commands.

> Status: **Phase 6 — Telegram alerts** (scan pipeline, one alert per opening, restart-safe).

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
| `jobbot prefs [--seed]` | Show active preferences (or store `config/defaults.yaml` in the DB) |

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

## Vocabulary

Cities, regions, countries and skills live in `config/locations.yaml` and `config/taxonomy.yaml`, so new ones can be added without code changes. After changing normaliser logic, bump `NORMALIZER_VERSION` and run `jobbot renormalize`.

## Adding a company

Add an entry to `config/companies.yaml` (`key`, `name`, `ats`, `slug`), then check it with `jobbot fetch <ats> <key> --dry-run`.

## Security

- Secrets come only from environment variables (`.env` locally, GitHub Secrets / Vercel env in production).
- Every log line goes through a redactor that strips bot tokens, connection-string passwords and API keys. Actions logs in a public repo are public.
- `.gitignore` excludes `.env` and all PDFs, so the resume is never committed.
