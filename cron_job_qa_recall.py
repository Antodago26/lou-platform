"""
Bon Home — QA Source Health + Link Health Cron Entry Point (v6.4.4).

Lancé par Render cron à 04:00 UTC quotidien (cf. render.yaml → lou-qa-recall ;
nom du service inchangé pour éviter une migration Render — repurpose interne).

  Command : python3 cron_job_qa_recall.py
  Schedule: 0 4 * * *

Deux phases en séquence :

  Phase 1 — SOURCE HEALTH (v6.4.4) : appelle
  `qa_source_health_worker.run_source_health_snapshot()`. Lit `properties`
  agrégé par `source`, calcule total_active / scraped_7d / scraped_30d /
  last_scrape, dérive un statut ok|warn|fail par source, écrit 1 row par
  source dans `qa_source_health` + 1 row `qa_runs`. Durée < 5s.

  Avant le drop produit Homegate + ImmoScout24 (CEO 30/04), Phase 1
  scrappait Homegate live pour 8 villes pour calculer un recall. Ce
  calcul n'a plus de sens sans Homegate — la phase a été repurpose en
  audit santé per-source. `qa_recall_snapshots` est conservée mais plus
  écrite (lecture rétro possible).

  Phase 2 — LINK HEALTH : appelle
  `qa_link_health_worker.run_link_health_check()` (cap 1000 URLs/run,
  oldest first). Inchangé par le repurpose Phase 1.

Exit code :
  0 si Phase 1 OK ET Phase 2 OK.
  1 si Phase 1 raised (hard failure transport DB) OU Phase 2 raised.

Phase 2 NE PEUT PAS faire échouer Phase 1 : si elle crash, on log
l'erreur, on continue (exit code reflète quand même la failure pour
Render dashboard rouge).
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
)
log = logging.getLogger('lou-qa-cron')


def _log_event(event: str, **fields) -> None:
    """Log JSON structuré sur stdout (facile à grep côté Render)."""
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    log.info(json.dumps(payload, default=str))


def run() -> int:
    if not os.environ.get('DATABASE_URL'):
        log.error("DATABASE_URL not set — aborting")
        return 1

    # Audit H8 (2026-05) : Phase 2 (link health) calls ScrapingBee Premium
    # for Homegate URLs. Without the key it silently falls back to direct
    # requests which DataDome blocks → entire batch lands as 'unreachable'
    # without any signal that the key was the root cause.
    if not os.environ.get('SCRAPINGBEE_API_KEY'):
        log.error("SCRAPINGBEE_API_KEY not set — Phase 2 would silently mark all Homegate URLs unreachable")
        return 1

    _log_event("cron_start", phase1="source_health", phase2="link_health")
    t0 = time.time()

    # Import local pour que le log.error ci-dessus puisse parler même
    # si le worker explose à l'import (ex: psycopg2 missing).
    from qa_source_health_worker import run_source_health_snapshot

    phase1_failed = False
    phase1_result = None
    try:
        phase1_result = run_source_health_snapshot()
        _log_event(
            "phase1_done",
            sources_total=phase1_result.get('sources_total'),
            run_id=phase1_result.get('run_id'),
            errors=phase1_result.get('errors'),
            status=phase1_result.get('status'),
            elapsed_s=phase1_result.get('elapsed_s'),
            sources=phase1_result.get('sources'),
        )
    except Exception as e:
        # Transport error / crash inattendu : pas de snapshot écrit.
        # Hard failure → exit code 1, Render dashboard rouge.
        phase1_failed = True
        _log_event(
            "phase1_failed",
            reason=type(e).__name__,
            error=str(e)[:300],
        )
        log.exception("Unexpected error in source health worker")

    phase1_elapsed = round(time.time() - t0, 1)
    _log_event("phase1_end", elapsed_s=phase1_elapsed, failed=phase1_failed)

    # ====================================================================
    # Phase 2 — Link health check
    # ====================================================================
    # On lance Phase 2 même si Phase 1 a échoué : les deux phases sont
    # indépendantes (Phase 2 lit `properties` directement, pas les snapshots
    # de Phase 1). Si Phase 1 a un problème, on veut quand même la donnée
    # Phase 2 du jour.
    phase2_failed = False
    phase2_t0 = time.time()
    try:
        from qa_link_health_worker import run_link_health_check
        link_result = run_link_health_check()
        _log_event(
            "phase2_end",
            urls_checked=link_result.get('urls_checked'),
            counts=link_result.get('counts'),
            cache_hits_homegate=link_result.get('cache_hits_homegate'),
            sb_credits_estimated=link_result.get('sb_credits_estimated'),
            unreachable_pct=link_result.get('unreachable_pct'),
            elapsed_s=link_result.get('elapsed_s'),
            run_id=link_result.get('run_id'),
        )
    except Exception as e:
        phase2_failed = True
        _log_event(
            "phase2_failed",
            elapsed_s=round(time.time() - phase2_t0, 1),
            reason=type(e).__name__,
            error=str(e)[:300],
        )
        log.exception("Phase 2 (link health) crashed")

    total_elapsed = round(time.time() - t0, 1)
    _log_event(
        "cron_end",
        elapsed_s=total_elapsed,
        phase1_elapsed_s=phase1_elapsed,
        phase1_failed=phase1_failed,
        phase1_sources_total=(phase1_result or {}).get('sources_total'),
        phase2_failed=phase2_failed,
    )

    return 0 if (not phase1_failed and not phase2_failed) else 1


if __name__ == '__main__':
    sys.exit(run())
