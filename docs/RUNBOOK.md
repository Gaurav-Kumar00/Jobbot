# JobBot — Runbook

How to operate JobBot: daily checks, incidents, key rotation and limits. For how the code works, see [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md).

## Where everything lives

| What | Where |
|---|---|
| Code & scheduled scans | https://github.com/Gaurav-Kumar00/Jobbot (Actions tab → **Scan**) |
| Telegram bot | @Qeiroo_Bot ("JobSy") |
| Bot web service | Vercel project `jobbot`, https://jobbot-eight.vercel.app (`/api/health`) |
| Hourly timer | cron-job.org → "JobBot tick" (every 15 min, header `X-Cron-Secret`) |
| Database | MongoDB Atlas, project `jobbot`, DB `jobbot`, user `jobbot` |
| Local secrets | `.env` (git-ignored; never commit) |

## Daily health check (30 seconds)

Send **`/status`** to the bot and check:

- **Last scan** is under ~70 minutes ago, with "N/N sources OK".
- **Sources** reads "all healthy". If not, send `/sources`.
- **Storage** is under 70%.

The bot also tells you when something is wrong:
- ⚠️ a source failed 3 scans in a row;
- 🚨 no scan has succeeded for 3 hours;
- a storage warning.

GitHub emails you if a scan run fails outright.

## Incidents

### 🚨 "No successful scan for N hours"
1. Open Actions → **Scan** and look at the latest runs.
   - **No recent runs at all:** the workflow may be disabled. GitHub disables scheduled workflows after 60 days without repo activity. Click **Enable workflow**; the keepalive workflow normally prevents this.
   - **Runs failing:** open the failed run's *Scan* step. `Config error: missing …` means a GitHub secret is missing or misnamed. `MongoDB error` means Atlas is down, or the password changed (update the `MONGODB_URI` secret).
2. Start a scan yourself: send `/scan_now`, or use Actions → Scan → Run workflow (tick **force**).
3. Check the cron-job.org job's **History**.
   - **401:** the `X-Cron-Secret` header doesn't match Vercel's `CRON_SECRET`.
   - **5xx:** see "Bot not responding" below.

### ⚠️ A source keeps failing
1. Send `/sources` to see the error.
2. Interpret the error:

   | Error | Meaning | Action |
   |---|---|---|
   | `HTTP 404` | The company moved or renamed its job board | Run `uv run jobbot discover "Company Name"` to find the new slug, then update `config/companies.yaml` |
   | `HTTP 403/429` | Rate limited | Raise its interval (`/set_interval <source> 180`, or `interval_minutes` in `companies.yaml`) |
   | `not configured` | An aggregator's key secret is missing (Adzuna, SerpApi) | Add the GitHub secret |
   | `SerpApi error: … run out of searches` | Monthly quota used | Resets monthly; lower `per_run` or raise `interval_minutes` |

3. To pause a company without deleting it, set `enabled: false` in `companies.yaml` and push.

The bot sends ✅ automatically once the source recovers.

### The bot doesn't answer commands
1. Open https://jobbot-eight.vercel.app/api/health. It should return `{"ok":true}`.
2. Run `uv run jobbot webhook-info` locally.
   - **`last_error_message` mentions 401:** `TELEGRAM_WEBHOOK_SECRET` differs between `.env` and Vercel. Fix Vercel, redeploy, then run `uv run jobbot set-webhook https://jobbot-eight.vercel.app`.
   - **URL is empty or wrong:** run `set-webhook` again.
3. Check Vercel → Deployments (latest is Ready?) and Logs.

   **Every route returns 404:** check `vercel.json` has no rewrite to `/api/index`.
4. Check Vercel → Settings → Environment Variables. Every variable listed in the developer guide §10 must be present. Redeploy after any change, because env changes only apply to new deployments.

### `/scan_now` says GitHub refused (HTTP 403)
The fine-grained token lacks permission or has expired.
1. Go to GitHub → Settings → Developer settings → Fine-grained tokens → `jobbot-dispatch`.
2. Check: repository access is **only Jobbot**, and **Actions** permission is **Read and write**.
3. If the token expired, use **Regenerate token**, then update `GH_DISPATCH_TOKEN` in `.env` and Vercel (and redeploy).

### Storage warning (70% / 85% / 95%)
- Compaction runs automatically. At 85% and above it becomes aggressive, and alerts keep working.
- Check usage with `uv run jobbot db-init`.
- If usage keeps climbing:
  - remove high-volume companies with almost no engineering roles from `companies.yaml` (the comments show `india=` and `eng=` counts);
  - or lower Atlas TTLs (`RUNS_TTL_SECONDS`, `LLM_CACHE_TTL` in `storage/mongo.py`).

### Too many, too few, or wrong alerts
- **Fewer, stronger alerts:** `/set_min_score 65`.
- **Widen the search:** `/location add pune`, `/set_exp 2`, `/set_salary 10 8`.
- **Why did this job alert or not?** Send `/job <id>` (the id is at the bottom of each alert), or run `uv run jobbot explain <id>`.
- **A job is clearly misclassified:**
  1. Reproduce it with `uv run jobbot fetch <ats> <company> --dry-run -v`.
  2. Fix the rule in `normalize/role.py` (or the relevant parser) and add the title to `tests/unit/test_role.py`.
  3. Bump `NORMALIZER_VERSION` and push.
- **Pause everything:** `/pause`. Matches are held and delivered on `/resume`.

### A duplicate alert arrived
This shouldn't happen. To investigate:
1. Find both job ids from the alerts.
2. Compare their fingerprints with `uv run jobbot explain <id>`. They differ when the company, title or city text differs.
3. If they should be the same opening, extend the synonyms in `normalize/dedup.py` and add a test case in `tests/unit/test_dedup.py`.

### The AI check is unavailable
No action is needed: scans continue without it. `/status` shows "AI checks today".
- Groq's free limits are roughly 1,000 requests and 200K tokens per day per model. The bot uses at most 100 calls a day.
- If you see `HTTP 401` in logs, the `GROQ_API_KEY` secret is wrong or revoked.

### Emergency stop
- **Stop alerts:** `/pause`.
- **Stop scanning:** Actions → Scan → ⋯ → **Disable workflow**, and pause the cron-job.org job.
- **Disconnect the bot:** in Telegram's BotFather, `/revoke` regenerates the token, which immediately invalidates the old one everywhere.

## Key rotation checklist

Rotate when a value may have leaked (it was pasted in chat or shown in a screenshot), when it expires, or yearly.

| Secret | Expires | Where it's used (update all of them) | How to rotate |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | never | `.env`, GitHub secret, Vercel env | BotFather → `/revoke`. Then run `uv run jobbot set-webhook https://jobbot-eight.vercel.app`. |
| `MONGODB_URI` (password) | never | `.env`, GitHub secret, Vercel env | Atlas → Database Access → `jobbot` → Edit Password → Autogenerate |
| `GH_DISPATCH_TOKEN` | **3 Nov 2026** | `.env`, Vercel env | GitHub → fine-grained tokens → `jobbot-dispatch` → Regenerate |
| `CRON_SECRET` | never (internal) | `.env`, Vercel env, cron-job.org header | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TELEGRAM_WEBHOOK_SECRET` | never (internal) | `.env`, Vercel env | Generate as above, redeploy Vercel, then run `set-webhook` |
| `GROQ_API_KEY` | never | `.env`, GitHub secret | console.groq.com → API Keys |
| `ADZUNA_APP_ID/KEY` | never | `.env`, GitHub secrets | developer.adzuna.com → Dashboard → Create new key |
| `SERPAPI_KEY` | never | `.env`, GitHub secret | serpapi.com → API Key → Regenerate |

After changing any Vercel variable, **redeploy**: Deployments → ⋯ → Redeploy.

**Currently recommended:**
- The `CRON_SECRET` appeared in a screenshot during setup. Rotate it at your convenience; it can only trigger scans.
- The Atlas password was pasted during setup before it was rotated, and has since been rotated.

## Free-tier limits (and the bot's use of them)

| Service | Free limit | JobBot usage |
|---|---|---|
| GitHub Actions (public repo) | Unlimited minutes; 6 h per job | About 1–3 min per scan, up to 24 scans/day |
| MongoDB Atlas M0 | 512 MB; pauses after 30 days with no connections | About 40 MB (8%) and growing slowly; compaction keeps it bounded |
| Vercel Hobby | 1M function invocations/month | About 100–200 per day (ticks + commands) |
| cron-job.org | Free | 96 calls/day |
| Telegram Bot API | About 30 msgs/s; 1 msg/s per chat | Paced at 1.1 s between alerts |
| Groq | About 1K req/day and 200K tokens/day per model | ≤ 100 calls/day (about 110K tokens) |
| Adzuna | 250/day, 2,500/month | ≤ 48/day |
| SerpApi | 250 searches/month | About 240/month (2 searches every 6 h) |

## Routine changes

| Task | How |
|---|---|
| Add a company | Add a line to `config/companies.yaml` (find the slug with `uv run jobbot discover "Name"`), check with `uv run jobbot fetch <ats> <key> --dry-run`, then push |
| Add a city | `/location add <city>`. If the city is unknown, add it to `config/locations.yaml` first. |
| Add a skill | `/skill add <skill>`. If unknown, add it with its aliases to `config/taxonomy.yaml`. |
| Change preferences | Telegram `/prefs` and the `/set_…` commands. `/scan_now` applies them immediately. |
| Send held-back matches now | `uv run jobbot backlog --send` |
| Re-send one alert | `/realert <id>` |
| Reset preferences to defaults | Delete the `preferences` document in Atlas. The bot then falls back to `config/defaults.yaml` (version 0). |

## Security review (Phase 11, Oct 2026)

**Verified:**
- No token, password or API key appears anywhere in the git history. Every current `.env` value was checked against all commits.
- No `.env` or PDF file is tracked.
- Workflow permissions are minimal: `ci` and `scan` have `contents: read`; `keepalive` has `actions: write`.
- Scans log at WARNING, and every log line passes through the redactor.
- Endpoints:
  - the webhook checks Telegram's secret header with a constant-time compare and answers only the owner;
  - `/api/tick` requires `CRON_SECRET`;
  - `/api/health` exposes nothing.
- The bot fetches only fixed source URLs, never user-supplied ones, so there is no SSRF.
- Telegram output is HTML-escaped.

**Known and accepted:**
- **Your Telegram chat id is in the history of one test commit.** It was removed in `dde6f3a`. It can't be used without the bot token.
- **Your Adzuna app id was in a recorded fixture**, via Adzuna's `utm_source` link parameter. It was scrubbed in Phase 11 but remains in history. It is useless without the app key, which was never committed.
- **Prompt injection is possible.** A posting's text is sent to the LLM, so it could try to steer the AI's answer. The impact is limited: the output must fit a strict schema, can only fill gaps or relabel vague titles, and can't override stated facts.

**Recommended:**
- Rotate `CRON_SECRET`; it was visible in a setup screenshot.
- Set a calendar reminder for the `GH_DISPATCH_TOKEN` expiry on **3 Nov 2026**.
