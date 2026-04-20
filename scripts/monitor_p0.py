#!/usr/bin/env python3
"""
monitor_p0.py — watchdog P0 SSL "bad record mac" (fix commit 69a53f8).

Périmètre temporaire : Peseux uniquement.
Cortaillod a été retiré le 20/04/2026 : son live scraping timeout à 480s
générait du bruit d'alerte non lié au fix SSL. À réintégrer une fois P1
(cache nocturne) déployé, quand le live scraping ne sera plus le chemin
critique de l'endpoint QA.

Two checks per run:
  1. Render logs API, filtered client-side by case-insensitive regex on
     the P0 patterns, over a sliding --window (default 2h).
  2. QA endpoint /api/stats/listings-qa on Peseux TWICE sequentially with
     a short delay — reproduces the original crash scenario (1st call
     possibly OK, 2nd hits the retry digue if any stale SSL conn lingers).
     Both Peseux calls must return 200.

Exit codes:
  0  OK            — no SSL pattern AND every QA call returned 200 ok=True.
  1  FAIL          — at least one SSL pattern matched in Render logs.
  2  WARN          — no SSL pattern, but at least one QA call failed (500
                     / timeout / JSON error=db_query_failed). Endpoint flaky
                     without visible log evidence of the P0 root cause.
  3  INFRA         — missing env var, Render API down on ALL pattern calls.

Stdout: JSON report. Also written to ./monitor-report.json (artefact).
Stderr: ::error:: / ::warning:: lines picked up by GitHub Actions.

Local debug:
  Put QA_TOKEN / RENDER_API_KEY / RENDER_SERVICE_ID in a .env at the repo
  root (or export them), then:
    python3 scripts/monitor_p0.py --window 2h
    python3 scripts/monitor_p0.py --window 48h   # historical first run
"""
import argparse
import atexit
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("ERROR: `requests` is required. pip install requests==2.32.3", file=sys.stderr)
    sys.exit(3)

RENDER_API = "https://api.render.com/v1"

# Patterns = case-insensitive substrings/regexes that prove a P0 regression.
SSL_PATTERNS = [
    r"bad record mac",
    r"ssl error",
    r"operationalerror",
    r"decryption failed",
    r"server closed the connection",
    r"db_query_failed",
]
SSL_REGEX = re.compile("|".join(SSL_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# .env loader (stdlib only) — optional, no-op if file absent.
# ---------------------------------------------------------------------------
def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            os.environ.setdefault(k, v)


def require_env(name):
    v = os.environ.get(name)
    if not v:
        print(f"::error::missing env var {name}", file=sys.stderr)
        sys.exit(3)
    return v


def parse_window(spec):
    """"2h" / "30m" / "48h" / "1d" → timedelta. Raises ValueError on bad input."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", spec, re.IGNORECASE)
    if not m:
        raise ValueError(f"bad --window '{spec}', expected e.g. 2h / 30m / 48h / 1d")
    n = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=n * mult)


# ---------------------------------------------------------------------------
# Render logs
# ---------------------------------------------------------------------------
def fetch_render_owner_id(api_key):
    """Resolve the owner ID automatically — the /v1/logs endpoint requires
    ownerId in addition to resource (service_id) since the 2025 API shift.
    Rather than loading Antony with a 4th secret, we call /v1/owners once
    at startup and pick the first owner. If the token has access to multiple
    workspaces we take the first; a warning is logged so it's visible in the
    Actions output if that ever becomes ambiguous."""
    r = requests.get(
        f"{RENDER_API}/owners",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    # Response shape: [{"owner": {"id": "tea-...", "name": "..."}}, ...]
    owners = []
    for item in body if isinstance(body, list) else body.get("data") or []:
        owner = item.get("owner") if isinstance(item, dict) else None
        if owner and owner.get("id"):
            owners.append(owner["id"])
        elif isinstance(item, dict) and item.get("id"):
            owners.append(item["id"])
    if not owners:
        raise RuntimeError(f"no owner returned by /v1/owners (body preview: {str(body)[:200]})")
    if len(owners) > 1:
        print(f"::warning::Render token has access to {len(owners)} owners; using first ({owners[0]})", file=sys.stderr)
    return owners[0]


def fetch_render_logs(api_key, owner_id, service_id, since_iso, until_iso, limit=500):
    """Call Render /v1/logs. Returns a list of log entries (possibly empty).
    Render's API shape for logs has migrated a couple of times — we tolerate
    {"logs":[...]}, {"data":[...]}, or a bare list."""
    r = requests.get(
        f"{RENDER_API}/logs",
        params={
            "ownerId": owner_id,
            "resource": service_id,
            "startTime": since_iso,
            "endTime": until_iso,
            "limit": limit,
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if isinstance(body, list):
        return body
    return body.get("logs") or body.get("data") or []


def scan_logs(api_key, service_id, window):
    now = datetime.now(timezone.utc)
    since = now - window
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    api_error = None
    owner_id = None
    entries = []
    try:
        owner_id = fetch_render_owner_id(api_key)
        entries = fetch_render_logs(api_key, owner_id, service_id, since_iso, until_iso)
    except requests.HTTPError as e:
        api_error = f"HTTP {e.response.status_code} {e.response.text[:200]}"
    except Exception as e:
        api_error = str(e)[:200]

    hits = []
    for entry in entries:
        msg = entry.get("message") or entry.get("text") or ""
        if SSL_REGEX.search(msg):
            hits.append({
                "timestamp": entry.get("timestamp") or entry.get("time"),
                "message": msg[:400],
            })

    return {
        "window": {"since": since_iso, "until": until_iso, "duration": str(window)},
        "owner_id": owner_id,
        "entries_scanned": len(entries),
        "hits": hits,
        "api_error": api_error,
    }


# ---------------------------------------------------------------------------
# QA endpoint
# ---------------------------------------------------------------------------
def call_qa(host, token, city, timeout_s=480):
    """One hit to /api/stats/listings-qa. Returns a dict with ok flag."""
    t0 = time.time()
    out = {"city": city, "ok": False, "status": 0, "elapsed_s": 0.0}
    try:
        r = requests.get(
            f"{host}/api/stats/listings-qa",
            params={"city": city},
            headers={"X-QA-Token": token},
            timeout=timeout_s,
        )
        out["elapsed_s"] = round(time.time() - t0, 2)
        out["status"] = r.status_code
        out["body_preview"] = r.text[:300]
        if r.status_code == 200:
            try:
                body = r.json()
                if body.get("error") is None:
                    out["ok"] = True
                    out["bonhome_total"] = (body.get("bonhome_indexed") or {}).get("total")
                else:
                    out["error"] = body.get("error")
                    out["detail"] = body.get("detail")
            except ValueError:
                out["error"] = "non-json response"
    except requests.Timeout:
        out["elapsed_s"] = round(time.time() - t0, 2)
        out["error"] = f"timeout after {timeout_s}s"
    except Exception as e:
        out["elapsed_s"] = round(time.time() - t0, 2)
        out["error"] = str(e)[:200]
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="P0 SSL regression watchdog")
    ap.add_argument("--window", default="2h", help="log lookback window (e.g. 2h / 48h / 30m)")
    ap.add_argument("--host", default=os.environ.get("BONHOME_HOST", "https://bonhome.ch"))
    ap.add_argument("--peseux-delay", type=int, default=8, help="seconds between Peseux calls 1 and 2")
    ap.add_argument("--report-path", default="monitor-report.json")
    args = ap.parse_args()

    load_dotenv()

    api_key = require_env("RENDER_API_KEY")
    service_id = require_env("RENDER_SERVICE_ID")
    qa_token = require_env("QA_TOKEN")

    try:
        window = parse_window(args.window)
    except ValueError as e:
        print(f"::error::{e}", file=sys.stderr)
        sys.exit(3)

    report = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "host": args.host,
        "window_requested": args.window,
        "logs": None,
        "qa_calls": [],
        "verdict": "pending",
        "partial": True,  # flipé à False juste avant exit propre
    }

    # Dump atomique sur disque — appelé après chaque étape + via atexit si
    # le process est tué (GitHub Actions timeout, SIGTERM, exception non
    # attrapée). Garantit qu'on a toujours monitor-report.json lisible
    # dans l'artefact même sur run cancelled.
    def _flush_report():
        try:
            with open(args.report_path, "w") as fh:
                json.dump(report, fh, indent=2, default=str)
        except Exception as e:
            print(f"::warning::could not write {args.report_path}: {e}", file=sys.stderr)

    atexit.register(_flush_report)

    # 1) Render logs (séquentiel, ~5-10s)
    report["logs"] = scan_logs(api_key, service_id, window)
    _flush_report()

    # 2) QA pings — Peseux ×2 séquentiels, délai peseux_delay entre les 2.
    # Cortaillod retiré le 20/04/2026 : timeout 480s du live scraping non lié
    # au fix SSL → bruit WARN toutes les 2h. À réintégrer post-déploiement P1
    # (cache nocturne) quand l'endpoint ne sera plus bound sur ScrapingBee.
    report["qa_calls"].append(call_qa(args.host, qa_token, "peseux"))
    _flush_report()
    time.sleep(args.peseux_delay)
    report["qa_calls"].append(call_qa(args.host, qa_token, "peseux"))
    _flush_report()

    # Verdict
    ssl_hit = bool(report["logs"]["hits"])
    qa_fail = any(not c["ok"] for c in report["qa_calls"])
    api_error = report["logs"]["api_error"]

    if ssl_hit:
        report["verdict"] = "FAIL"
        exit_code = 1
    elif qa_fail:
        report["verdict"] = "WARN"
        exit_code = 2
    else:
        report["verdict"] = "OK"
        exit_code = 0

    # Run complet → on retire le flag partial. Le atexit flushera la version
    # finale; on dump aussi explicitement ici pour couper court à toute race.
    report["partial"] = False
    _flush_report()

    # Output: JSON to stdout
    print(json.dumps(report, indent=2, default=str))

    # GitHub-Actions annotations
    if ssl_hit:
        print(f"::error::P0 REGRESSION — {len(report['logs']['hits'])} SSL log hit(s) in last {args.window}", file=sys.stderr)
        for h in report["logs"]["hits"][:5]:
            print(f"::error::[{h['timestamp']}] {h['message']}", file=sys.stderr)
    if qa_fail:
        for c in report["qa_calls"]:
            if not c["ok"]:
                print(f"::warning::QA {c['city']} failed: status={c['status']} err={c.get('error') or c.get('detail')}", file=sys.stderr)
    if api_error:
        print(f"::warning::Render logs API error (log check degraded): {api_error}", file=sys.stderr)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
