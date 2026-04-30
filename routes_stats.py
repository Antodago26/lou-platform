"""
Bon Home — Stats QA Blueprint (v6.4.4 — repurpose source health).

Endpoint pour cron externe (Cowork) qui sert les snapshots santé per-source
précalculés par la Phase 1 du cron `lou-qa-recall` (Render cron, 04:00 UTC
quotidien). Protégé par header X-QA-Token (constant-time compare).

GET /api/stats/listings-qa
  → SELECT DISTINCT ON (source) le row le plus récent de qa_source_health
    (un par source, captured_at DESC).
  → 200 avec `sources: [...]` + `captured_at` (le plus récent global) +
    `snapshot_age_hours` si au moins une row existe.
  → 503 snapshot_not_ready si la table est vide (cron pas encore tourné
    après la migration v6.4.4).

BREAKING CHANGE vs v6.4.0 :
  - Le param `?city=<slug>` est retiré (la donnée est globale, pas
    per-city). Cowork SKILL.md `monitor-villes-vs-bonhome` doit être
    adapté en aval (drop produit Homegate/IS24 30/04 — la liste des
    villes n'a plus de sens dans ce contexte).
  - Le shape change : `{city, source_total_listings, our_total_listings,
    recall_pct, missing_listing_ids, breakdown}` → `{captured_at,
    snapshot_age_hours, sources: [{source, status, total_active,
    scraped_7d, scraped_30d, last_scrape}, ...]}`.

Sample 200 :
  {
    "captured_at": "2026-04-30T04:00:12.345+00:00",
    "snapshot_age_hours": 1.5,
    "sources": [
      {"source": "Flatfox",       "status": "ok",
       "total_active": 412, "scraped_7d": 380, "scraped_30d": 410,
       "last_scrape": "2026-04-30T03:42:01+00:00"},
      {"source": "Immobilier.ch", "status": "warn",
       "total_active": 89,  "scraped_7d": 0,   "scraped_30d": 89,
       "last_scrape": "2026-04-22T15:18:33+00:00"},
      ...
    ]
  }
"""
import os
import hmac
import logging
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, request, jsonify

from db import get_db, return_db

log = logging.getLogger('lou-app')
stats_bp = Blueprint('stats', __name__)

QA_TOKEN = os.environ.get('QA_TOKEN', '').strip()


def _check_token():
    """Constant-time compare du header X-QA-Token avec QA_TOKEN env."""
    if not QA_TOKEN:
        return False
    provided = request.headers.get('X-QA-Token', '') or ''
    return hmac.compare_digest(provided, QA_TOKEN)


def _row_get(row, key, tuple_index):
    """Tolère RealDictCursor (dict) ET cursor brut (tuple)."""
    if isinstance(row, dict):
        return row[key]
    return row[tuple_index]


@stats_bp.route('/api/stats/listings-qa', methods=['GET'])
def listings_qa():
    """Lecture pure du dernier snapshot par source.

    DISTINCT ON (source) ORDER BY source, captured_at DESC = pour chaque
    source, garder le row le plus récent. Pattern Postgres-natif, plus
    rapide qu'une window function ROW_NUMBER() pour ce cas.

    Deux chemins DB-transient :
      - OperationalError / InterfaceError (SSL bad record mac sur Neon
        cold-start) : 1 retry avec conn détruite via return_db(close=True),
        ensuite 503 si ça repète.
      - Autre exception : 500 avec detail tronqué.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401

    rows = None
    last_err = None
    for attempt in (1, 2):
        conn = None
        conn_broken = False
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT ON (source)
                       source, captured_at, total_active,
                       scraped_7d, scraped_30d, last_scrape, status
                FROM qa_source_health
                ORDER BY source, captured_at DESC
            """)
            rows = cur.fetchall()
            cur.close()
            break  # succès
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            conn_broken = True
            last_err = e
            log.warning(f"listings-qa DB transient (attempt {attempt}/2): {e}")
            if attempt == 2:
                log.exception(f"listings-qa DB transient after retry: {e}")
                return jsonify({
                    "error": "db_transient",
                    "detail": str(e)[:200],
                }), 503
        except Exception as e:
            log.exception(f"listings-qa DB query failed: {e}")
            return jsonify({
                "error": "db_query_failed",
                "detail": str(e)[:200],
            }), 500
        finally:
            if conn is not None:
                try:
                    return_db(conn, close=conn_broken)
                except Exception:
                    pass

    if not rows:
        return jsonify({
            "error": "snapshot_not_ready",
            "message": (
                "Aucun snapshot santé per-source en DB. "
                "Prochaine exécution du cron à 04:00 UTC."
            ),
        }), 503

    sources = []
    latest_captured_at = None
    for row in rows:
        source       = _row_get(row, 'source',       0)
        captured_at  = _row_get(row, 'captured_at',  1)
        total_active = _row_get(row, 'total_active', 2)
        scraped_7d   = _row_get(row, 'scraped_7d',   3)
        scraped_30d  = _row_get(row, 'scraped_30d',  4)
        last_scrape  = _row_get(row, 'last_scrape',  5)
        status       = _row_get(row, 'status',       6)

        if latest_captured_at is None or (
            captured_at is not None and captured_at > latest_captured_at
        ):
            latest_captured_at = captured_at

        sources.append({
            "source":       source,
            "status":       status,
            "total_active": int(total_active) if total_active is not None else 0,
            "scraped_7d":   int(scraped_7d)   if scraped_7d   is not None else 0,
            "scraped_30d":  int(scraped_30d)  if scraped_30d  is not None else 0,
            "last_scrape":  last_scrape.isoformat() if last_scrape else None,
        })

    age_hours = None
    if latest_captured_at is not None:
        try:
            now = datetime.now(timezone.utc)
            age_hours = round((now - latest_captured_at).total_seconds() / 3600, 1)
        except Exception:
            age_hours = None

    return jsonify({
        "captured_at": latest_captured_at.isoformat() if latest_captured_at else None,
        "snapshot_age_hours": age_hours,
        "sources": sources,
    }), 200
