# JobBot

A personal job-discovery bot. Every hour it scans about 180 companies' public job boards plus several job aggregators. It scores each new posting against my resume and preferences, removes duplicates, and sends every relevant opening to Telegram exactly once. It runs entirely on free tiers: GitHub Actions, MongoDB Atlas, Vercel and cron-job.org.

**Status:** live. 183 sources, scanned hourly, with Telegram commands, a watchdog, and an optional AI check (Groq).

- **[Developer guide](docs/DEVELOPER_GUIDE.md):** architecture, tech stack, project structure, data model, how a scan works, recipes for extending, and build history.
- **[Runbook](docs/RUNBOOK.md):** daily checks, incidents, key rotation, free-tier limits.

## What an alert looks like

```
🔥 92/100 · Python Developer
🏢 Acme Labs

📍 Location: Bengaluru
🏠 Work mode: Hybrid
🧑‍💻 Experience: 0–1 yrs (fresher-friendly)
💰 Salary: Base ₹16–20 LPA
🕒 Posted: 3h ago · found 05 Oct, 10:17 IST

✅ Why it matches
• Backend role (preferred title)
• Fresher-friendly (0–1 yrs)
• Python + Django, FastAPI
• Also matches: Kafka, Celery, PostgreSQL

📝 About the role (AI summary)
Builds and owns payment APIs …

🔗 Lever · lever:acme:1234            [Apply ↗]
```

## Sources

| Kind | Sources |
|---|---|
| Company job boards (ATS) | Greenhouse (78), Ashby (44), Lever (31), SmartRecruiters (21), Workable (3), Keka |
| Big-tech careers APIs | Amazon, Microsoft, Atlassian |
| Job boards & aggregators | Unstop, Adzuna India, Google Jobs via SerpApi |

Google Jobs also covers listings that appear on LinkedIn, Naukri, Instahyre and similar sites. Those sites forbid scraping, so they are never scraped directly.

## Quick start (development)

```bash
make install          # uv sync — Python 3.12 via uv
cp .env.example .env  # fill in what you need (tests never read it)
make test             # full offline test suite
make lint             # ruff
uv run jobbot --help  # every operator command
```

Common commands:

| Command | Purpose |
|---|---|
| `jobbot scan [--no-send] [--force] [--no-llm]` | One full scan: fetch, store, score, AI check, alert |
| `jobbot rank [--top N]` | Live-score all sources and print the ranking (no DB writes) |
| `jobbot fetch <ats> <company> --dry-run -v` | One source, with normalised fields |
| `jobbot explain <job_id>` | Full score breakdown |
| `jobbot discover "<Company>"` | Find which public ATS board a company uses |
| `jobbot backlog [--send]` | Held-back matches as a digest |
| `jobbot db-init` | Database connectivity, indexes, storage used |
| `jobbot set-webhook <url>`, `jobbot webhook-info` | Telegram webhook setup and status |
| `jobbot ping`, `whoami`, `chat-id`, `prefs`, `renormalize`, `realert` | Setup and maintenance |

Telegram commands include `/status`, `/latest`, `/today`, `/history`, `/job <id>`, `/pause`, `/resume`, `/scan_now`, `/prefs`, `/set_salary`, `/set_exp`, `/set_min_score`, `/location`, `/skill` and `/role`. Send `/help` in the bot for the full list.

## Security

- **Secrets** live only in `.env` (git-ignored), GitHub Secrets and Vercel env vars. Settings hold them as `SecretStr`.
- **Logs:** every log line passes through a redactor. Actions logs are public, so production logs at WARNING.
- **Webhook:** it only answers the owner's chat, and requires Telegram's secret header. The tick endpoint requires its own secret.
- **GitHub token:** fine-grained, this repo only, Actions read/write.
- **Personal files:** `.gitignore` excludes `.env` and all PDFs, so the resume is never committed.
