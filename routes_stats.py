"""
Bon Home — Stats QA Blueprint (v6.4.0 — snapshot read-only).

Endpoint pour cron externe (Cowork) qui sert le recall précalculé par
`cron_job_qa_recall.py` (Render cron, 04:00 UTC quotidien). Protégé par
header X-QA-Token (constant-time compare). Pas d'auth JWT : le token
sert justement à appeler hors session utilisateur.

GET /api/stats/listings-qa?city=<slug>
  → SELECT DISTINCT ON (city) le row le plus récent de qa_recall_snapshots
  → retourne 503 snapshot_not_ready si aucun row (le cron n'a pas encore
    tourné pour cette ville) ; 200 avec le snapshot + snapshot_age_hours
    sinon.

IMPORTANT (v6.4.0) :
  - Plus de scrape live dans l'endpoint. Cible : < 100 ms en lecture.
  - Plus de fallback ?live=true : si le snapshot est absent ou stale, c'est
    signalé via le code HTTP (503) ou le champ snapshot_age_hours (200).
    L'opérateur est responsable de surveiller age_hours et d'ack un cron
    qui a échoué.
  - Breaking change de shape vs v6.3.x : `bonhome_indexed.*` et
    `sources.*_live.*` ont disparu. Les consommateurs (Cowork SKILL.md
    `monitor-villes-vs-bonhome`) doivent être mis à jour pour parser la
    nouvelle forme (source_total_listings, our_total_listings, breakdown).
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
    """Constant-time compare du header X-QA-Token avec QA_TOKEN env.
    Retourne True si OK, False sinon. Bloque aussi si QA_TOKEN vide
    (protection anti-accès accidentel si env var pas set en prod)."""
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
    """Lecture pure du snapshot le plus récent pour une ville.

    Deux chemins DB-transient :
      - OperationalError / InterfaceError (SSL bad record mac sur Neon
        cold-start) : 1 retry avec conn détruite via return_db(close=True),
        ensuite 503 si ça repète (rare mais possible).
      - Autre exception : 500 avec detail tronqué.

    Note : on ne valide PLUS le city_slug côté endpoint (pas de
    _CITY_SLUG_TO_DISPLAY ici — cf. qa_recall_worker.py). Un slug inconnu
    produira 503 snapshot_not_ready, ce qui est le comportement attendu :
    soit tu demandes une ville qui n'existe pas, soit le cron n'a pas
    tourné dessus — dans les deux cas la réponse "pas de snapshot" est
    appropriée.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401

    city_slug = (request.args.get('city') or '').strip().lower()
    if not city_slug:
        return jsonify({"error": "city required"}), 400

    row = None
    last_err = None
    for attempt in (1, 2):
        conn = None
        conn_broken = False
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, captured_at, source_total_listings, our_total_listings,
                       recall_pct, missing_listing_ids, raw_snapshot
                FROM qa_recall_snapshots
                WHERE city = %s
                ORDER BY captured_at DESC
                LIMIT 1
            """, (city_slug,))
            row = cur.fetchone()
            cur.close()
            break  # succès → sort du retry loop
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

    if not row:
        return jsonify({
            "error": "snapshot_not_ready",
            "message": (
                "Snapshot pas encore généré pour cette ville. "
                "Prochaine exécution du cron à 04:00 UTC."
            ),
            "city": city_slug,
        }), 503

    captured_at   = _row_get(row, 'captured_at',           1)
    source_total  = _row_get(row, 'source_total_listings', 2)
    our_total     = _row_get(row, 'our_total_listings',    3)
    recall_pct    = _row_get(row, 'recall_pct',            4)
    missing       = _row_get(row, 'missing_listing_ids',   5)
    breakdown     = _row_get(row, 'raw_snapshot',          6)

    # captured_at = TIMESTAMPTZ → datetime tz-aware
    now = datetime.now(timezone.utc)
    try:
        age_hours = round((now - captured_at).total_seconds() / 3600, 1)
    except Exception:
        age_hours = None

    return jsonify({
        "city": city_slug,
        "captured_at": captured_at.isoformat() if captured_at else None,
        "snapshot_age_hours": age_hours,
        "source_total_listings": int(source_total) if source_total is not None else 0,
        "our_total_listings": int(our_total) if our_total is not None else 0,
        # Decimal → float pour JSON-sérialisable
        "recall_pct": float(recall_pct) if recall_pct is not None else None,
        # missing et breakdown sont des JSONB, psycopg2 les renvoie déjà
        # parsés en list/dict Python.
        "missing_listing_ids": missing or [],
        "breakdown": breakdown or {},
    }), 200
