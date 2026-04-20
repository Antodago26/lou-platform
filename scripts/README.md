# scripts/

Operational helper scripts, not shipped with the app.

## `monitor_p0.py` — P0 SSL regression watchdog

Watches for the return of the `SSL error: decryption failed or bad record mac`
class of bugs fixed in commit `69a53f8` (thread-local `_CACHE_CONN` in
`scrapers.py`, 1-shot retry digue in `routes_stats.py`). Designed to run
unattended in `.github/workflows/monitor-p0-ssl.yml` every 2h for 48h, but
also runnable locally for debug.

### What it does

1. Scans Render logs on a sliding window (default `2h`, override with
   `--window 48h` etc.) via the Render `/v1/logs` API, filters
   client-side with a case-insensitive regex on the P0 patterns
   (`bad record mac`, `ssl error`, `operationalerror`, `decryption failed`,
   `server closed the connection`, `db_query_failed`).
2. Hits `GET /api/stats/listings-qa` with `X-QA-Token` on:
   - `cortaillod` — sanity check, must pass.
   - `peseux` twice sequentially with ~8s delay — reproduces the original
     crash scenario; the 2nd call tests the retry digue if any stale conn
     was left behind.
3. Writes a structured report to stdout **and** to `./monitor-report.json`
   (picked up by the workflow as an artifact).

### Exit codes

| Code | Verdict | Meaning                                                                      |
|------|---------|------------------------------------------------------------------------------|
| 0    | OK      | Zero SSL log hits, all 3 QA calls returned 200 with `error=null`.            |
| 1    | FAIL    | At least one SSL pattern matched — workflow turns red, email notif fires.   |
| 2    | WARN    | No log evidence but a QA call failed (500/timeout/db_query_failed).         |
| 3    | INFRA   | Missing env var or bad `--window` — distinct from a real regression.        |

### Run it locally

Requires Python 3.9+ and `requests`.

```bash
pip install requests
```

Put the 3 secrets in a `.env` at the repo root (git-ignored), e.g.:

```
QA_TOKEN=<value already set in Render env>
RENDER_API_KEY=<generate on Render dashboard → Account Settings → API Keys>
RENDER_SERVICE_ID=srv-xxxxxxxx
```

Then:

```bash
# Periodic check (same as scheduled workflow):
python3 scripts/monitor_p0.py --window 2h

# Historical backfill covering the 19/04 crash + overnight crons:
python3 scripts/monitor_p0.py --window 48h

# Skip the delay between Peseux calls (local debug):
python3 scripts/monitor_p0.py --peseux-delay 0
```

Report goes to stdout as pretty-printed JSON. A copy lands at
`./monitor-report.json` which you can jq-grep:

```bash
jq '.verdict, .logs.hits | length, .qa_calls[] | {city, ok, elapsed_s}' monitor-report.json
```

### GitHub Actions setup (one-time, Antony)

Repo → **Settings** → **Secrets and variables** → **Actions** → **New
repository secret**, add:

| Secret             | Where to find it                                                          |
|--------------------|---------------------------------------------------------------------------|
| `QA_TOKEN`         | Same value you already put in Render env vars.                           |
| `RENDER_API_KEY`   | Render dashboard → Account Settings → API Keys → Create. Copy once.      |
| `RENDER_SERVICE_ID`| Render dashboard → bonhome service → URL shows `srv-xxxxxxxx`.            |

> **No `RENDER_OWNER_ID` needed.** The Render `/v1/logs` endpoint requires
> `ownerId` in addition to `resource` since the 2025 API shift, but the
> script resolves it automatically by calling `/v1/owners` at startup with
> the same API key. If the token has access to multiple workspaces, the
> first owner is used and a `::warning::` annotation is emitted.

First run: **Actions** tab → `monitor-p0-ssl` → **Run workflow** → set
`window=48h` → Run. After green, the `cron` schedule takes over every 2h.

### Life cycle

Disable the workflow after 48h clean (Actions → `monitor-p0-ssl` → `···` →
Disable), OR lower the cron to `'0 8 * * *'` for a daily canary.
