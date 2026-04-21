"""
Bon Home — QA Recall Cron Entry Point (v6.4.0).

Lancé par Render cron à 04:00 UTC quotidien (cf. render.yaml → lou-qa-recall).

  Command : python3 cron_job_qa_recall.py
  Schedule: 0 4 * * *

Itère sur les villes de QA_RECALL_CITIES (env, défaut = 5 villes beta),
appelle `qa_recall_worker.run_recall_snapshot_for_city` pour chacune, log
le résultat en JSON structuré sur stdout.

Exit code :
  0 si TOUTES les villes ont produit un snapshot (même status='partial')
  1 si au moins une ville a complètement échoué (exception non catchée
    dans le worker → pas de row qa_recall_snapshots écrit)

Un status='failed' (tous les combos d'une ville en échec) compte quand
même comme "la ville a un snapshot" — le row existe, recall_pct=NULL,
l'endpoint servira snapshot_age_hours pour que l'opérateur voie que
le cron a tourné mais n'a rien pu mesurer.
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

    total_elapsed = round(time.time() - t0, 1)
    _log_event(
        "cron_end",
        elapsed_s=total_elapsed,
        cities_processed=len(results),
        cities_failed=hard_failures,
        cities_total=len(cities),
    )

    return 0 if hard_failures == 0 else 1


if __name__ == '__main__':
    sys.exit(run())
