"""
Bon Home — QA Recall + Link Health Cron Entry Point (v6.4.5).

Lancé par Render cron à 04:00 UTC quotidien (cf. render.yaml → lou-qa-recall).

  Command : python3 cron_job_qa_recall.py
  Schedule: 0 4 * * *

Deux phases en séquence (Option B, archi validée 26/04 — pas de cron séparé) :

  Phase 1 — RECALL : itère sur QA_RECALL_CITIES, appelle
  `qa_recall_worker.run_recall_snapshot_for_city` pour chacune, écrit
  qa_recall_snapshots. Durée actuelle ~32 min (8 villes × Homegate × 2 tx).

  Phase 2 — LINK HEALTH : appelle
  `qa_link_health_worker.run_link_health_check()` (cap 1000 URLs/run,
  oldest first). HEAD direct gratuit pour non-Homegate (~85% du stock),
  ScrapingBee Premium uniquement pour Homegate sans scrape récent
  (optimisation e). Durée cible 5-15 min.

Exit code :
  0 si Phase 1 OK (toutes les villes ont un snapshot) ET Phase 2 OK
    (run_link_health_check n'a pas raised une exception fatale).
  1 si Phase 1 a au moins 1 hard failure OU Phase 2 a raised.

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

# v6.4.1 : focus canton Neuchâtel uniquement (décision produit, Antony
# 21/04). Lausanne/Genève dropped — trop gros volume pour le budget
# ScrapingBee et hors scope beta. 8 villes NE représentatives.
#
# Slugs reconnus par qa_recall_worker._CITY_SLUG_TO_DISPLAY :
# - peseux, neuchatel, la-chaux-de-fonds, boudry, cortaillod : slugs directs
# - colombier → display "Colombier", scraper ajoute -ne via CITY_CANTONS
# - marin     → display "Marin-Epagnier" (URL Homegate post-fusion 2009)
# - saint-blaise → display "Saint-Blaise", scraper ajoute -ne
_DEFAULT_CITIES = (
    'peseux,neuchatel,la-chaux-de-fonds,boudry,cortaillod,'
    'colombier,marin,saint-blaise'
)


def _parse_cities() -> list:
    """Parse QA_RECALL_CITIES (CSV) avec trim + lowercase. Slugs, pas
    de display names."""
    raw = (os.environ.get('QA_RECALL_CITIES') or _DEFAULT_CITIES).strip()
    slugs = [s.strip().lower() for s in raw.split(',') if s.strip()]
    return slugs


def _log_event(event: str, **fields) -> None:
    """Log JSON structuré sur stdout (facile à grep côté Render)."""
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    log.info(json.dumps(payload, default=str))


def run() -> int:
    if not os.environ.get('DATABASE_URL'):
        log.error("DATABASE_URL not set — aborting")
        return 1

    cities = _parse_cities()
    if not cities:
        log.error("QA_RECALL_CITIES is empty — aborting")
        return 1

    _log_event("cron_start", cities=cities, count=len(cities))
    t0 = time.time()

    # Import local pour que le log.error ci-dessus puisse parler même
    # si qa_recall_worker explose à l'import (ex: psycopg2 missing).
    from qa_recall_worker import run_recall_snapshot_for_city

    results = []
    hard_failures = 0
    for slug in cities:
        try:
            res = run_recall_snapshot_for_city(slug)
            _log_event(
                "city_done",
                city=slug,
                run_id=res.get('run_id'),
                snapshot_id=res.get('snapshot_id'),
                source_total=res.get('source_total'),
                our_total=res.get('our_total'),
                recall_pct=res.get('recall_pct'),
                errors=res.get('errors'),
                elapsed_s=res.get('elapsed_s'),
            )
            results.append(res)
        except ValueError as e:
            # Slug inconnu : pas de snapshot écrit. Hard failure.
            hard_failures += 1
            _log_event("city_failed", city=slug, reason="unknown_slug", error=str(e))
        except Exception as e:
            # Transport error / crash inattendu : pas de snapshot écrit.
            # C'est une vraie panne — on compte en hard_failures pour
            # que l'exit code soit 1 et que Render flagge le cron rouge.
            hard_failures += 1
            _log_event(
                "city_failed",
                city=slug,
                reason=type(e).__name__,
                error=str(e)[:300],
            )
            log.exception(f"Unexpected error on city={slug}")

    phase1_elapsed = round(time.time() - t0, 1)
    _log_event(
        "phase1_end",
        elapsed_s=phase1_elapsed,
        cities_processed=len(results),
        cities_failed=hard_failures,
        cities_total=len(cities),
    )

    # ====================================================================
    # Phase 2 — Link health check
    # ====================================================================
    # On lance Phase 2 même si Phase 1 a eu des hard_failures : les deux
    # phases sont indépendantes (Phase 2 lit `properties` directement, pas
    # les snapshots de Phase 1). Si Phase 1 a un problème, on veut quand
    # même la donnée Phase 2 du jour.
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
        cities_processed=len(results),
        cities_failed=hard_failures,
        cities_total=len(cities),
        phase2_failed=phase2_failed,
    )

    return 0 if (hard_failures == 0 and not phase2_failed) else 1


if __name__ == '__main__':
    sys.exit(run())
