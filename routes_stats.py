"""
Bon Home — Stats QA Blueprint (v6.3.3 — pré-beta audit point 4).

Endpoint pour cron externe (Cowork) qui compare Bonhome vs portails en live.
Protégé par header X-QA-Token (constant-time compare). Pas d'auth JWT :
le token sert justement à appeler hors session utilisateur.

GET /api/stats/listings-qa?city=<slug>
  → bonhome_indexed (COUNT DB) + homegate_live + immoscout24_live via
    ScrapingBee. Pour chaque portail : recall rate = |IDs live ∩ IDs DB|
    / |IDs live|. Vente + location agrégés ET détaillés.

⚠ Latence : chaque appel scraper ≈ 30-60s (ScrapingBee stealth proxy).
   2 portails × 2 transactions = 4 calls ≈ 2 min. max_pages=1 pour borner.
   gunicorn timeout = 300s (render.yaml) donc marge OK.

⚠ Coût : ~4 crédits ScrapingBee par hit. Cron externe = 1× /jour suffit.
"""
import os
import time
import hmac
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from db import get_db, return_db
from scrapers import scrape_homegate, scrape_immoscout

log = logging.getLogger('lou-app')
stats_bp = Blueprint('stats', __name__)

QA_TOKEN = os.environ.get('QA_TOKEN', '').strip()

# Slug → display name pour les scrapers (qui attendent le nom avec accents).
# Limité aux villes du beta + quelques grandes villes. Étendre au besoin.
_CITY_SLUG_TO_DISPLAY = {
    'neuchatel': 'Neuchâtel',
    'la-chaux-de-fonds': 'La Chaux-de-Fonds',
    'le-locle': 'Le Locle',
    'cortaillod': 'Cortaillod',
    'colombier': 'Colombier',
    'peseux': 'Peseux',
    'boudry': 'Boudry',
    'marin-epagnier': 'Marin-Epagnier',
    'hauterive': 'Hauterive',
    'saint-blaise': 'Saint-Blaise',
    'milvignes': 'Milvignes',
    'la-tene': 'La Tène',
    'le-landeron': 'Le Landeron',
    'bevaix': 'Bevaix',
    'val-de-ruz': 'Val-de-Ruz',
    'val-de-travers': 'Val-de-Travers',
    'fleurier': 'Fleurier',
    'corcelles-cormondreche': 'Corcelles-Cormondrèche',
    'lausanne': 'Lausanne',
    'geneve': 'Genève',
    'fribourg': 'Fribourg',
    'sion': 'Sion',
    'bienne': 'Bienne',
    'montreux': 'Montreux',
    'nyon': 'Nyon',
    'morges': 'Morges',
    'yverdon': 'Yverdon',
    'yverdon-les-bains': 'Yverdon-les-Bains',
    'vevey': 'Vevey',
    'renens': 'Renens',
    'zurich': 'Zurich',
    'basel': 'Basel',
    'berne': 'Berne',
    'lugano': 'Lugano',
}


def _check_token():
    """Constant-time compare du header X-QA-Token avec QA_TOKEN env.
    Retourne True si OK, False sinon. Bloque aussi si QA_TOKEN vide
    (protection anti-accès accidentel si env var pas set en prod)."""
    if not QA_TOKEN:
        return False
    provided = request.headers.get('X-QA-Token', '') or ''
    return hmac.compare_digest(provided, QA_TOKEN)


def _safe_scrape(fn, portal_name, city_display, transaction):
    """Wrapper qui attrape tout, log, et retourne (listings, elapsed_ms, error).
    Un portail en échec ne doit PAS faire sauter l'endpoint entier (l'autre
    portail peut toujours renvoyer son recall)."""
    t0 = time.time()
    try:
        listings = fn(city=city_display, transaction=transaction, max_pages=1) or []
    except Exception as e:
        log.exception(f"listings-qa {portal_name}/{transaction}/{city_display} failed: {e}")
        return [], round((time.time() - t0) * 1000, 1), str(e)[:200]
    return listings, round((time.time() - t0) * 1000, 1), None


def _recall(live_ids, db_ids, sample_n=5):
    """|live ∩ db| / |live|, plus un échantillon d'IDs manquants pour debug."""
    live_set = set(live_ids)
    db_set = set(db_ids)
    matched = live_set & db_set
    missing = list(live_set - db_set)[:sample_n]
    recall = round(len(matched) / len(live_set), 3) if live_set else None
    return {
        "live_count": len(live_set),
        "matched_in_db": len(matched),
        "recall": recall,
        "missing_sample": missing,
    }


@stats_bp.route('/api/stats/listings-qa', methods=['GET'])
def listings_qa():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401

    city_slug = (request.args.get('city') or '').strip().lower()
    if not city_slug:
        return jsonify({"error": "city required"}), 400

    city_display = _CITY_SLUG_TO_DISPLAY.get(city_slug)
    if not city_display:
        return jsonify({"error": f"unknown city slug '{city_slug}'"}), 404

    t_start = time.time()

    # --- 1) Bonhome indexed (DB COUNT + breakdown) ------------------------
    bonhome = {
        "total": 0,
        "by_transaction": {"location": 0, "achat": 0},
        "by_source": {},
        "ids_by_portal": {"homegate": set(), "immoscout24": set()},
    }
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Match sur city display OU slug (la colonne contient le nom d'affichage)
        # pour tolérer les 2 orthographes "Neuchâtel" / "neuchatel".
        cur.execute("""
            SELECT COALESCE(transaction,'') AS transaction,
                   COUNT(*) AS n
            FROM properties
            WHERE is_active = TRUE
              AND LOWER(COALESCE(city,'')) IN (%s, %s)
            GROUP BY transaction
        """, (city_display.lower(), city_slug))
        for r in cur.fetchall():
            tx = r['transaction'] if isinstance(r, dict) else r[0]
            n = r['n'] if isinstance(r, dict) else r[1]
            if tx in ('location', 'achat'):
                bonhome["by_transaction"][tx] = int(n)
            bonhome["total"] += int(n)

        # Breakdown par source (via property_sources)
        cur.execute("""
            SELECT ps.source, COUNT(DISTINCT p.id) AS n
            FROM properties p
            LEFT JOIN property_sources ps ON ps.property_id = p.id
            WHERE p.is_active = TRUE
              AND LOWER(COALESCE(p.city,'')) IN (%s, %s)
            GROUP BY ps.source
        """, (city_display.lower(), city_slug))
        for r in cur.fetchall():
            src = r['source'] if isinstance(r, dict) else r[0]
            n = r['n'] if isinstance(r, dict) else r[1]
            if src:
                bonhome["by_source"][src] = int(n)

        # IDs externes connus par portail (pour recall)
        cur.execute("""
            SELECT p.source, p.external_id
            FROM properties p
            WHERE p.is_active = TRUE
              AND LOWER(COALESCE(p.city,'')) IN (%s, %s)
              AND p.source IN ('homegate', 'immoscout24')
              AND p.external_id IS NOT NULL
        """, (city_display.lower(), city_slug))
        for r in cur.fetchall():
            src = r['source'] if isinstance(r, dict) else r[0]
            eid = r['external_id'] if isinstance(r, dict) else r[1]
            if src in bonhome["ids_by_portal"]:
                bonhome["ids_by_portal"][src].add(str(eid))

        # Aussi via property_sources (cross-portal dedup)
        cur.execute("""
            SELECT ps.source, ps.external_id
            FROM property_sources ps
            JOIN properties p ON p.id = ps.property_id
            WHERE p.is_active = TRUE
              AND LOWER(COALESCE(p.city,'')) IN (%s, %s)
              AND ps.source IN ('homegate', 'immoscout24')
              AND ps.external_id IS NOT NULL
        """, (city_display.lower(), city_slug))
        for r in cur.fetchall():
            src = r['source'] if isinstance(r, dict) else r[0]
            eid = r['external_id'] if isinstance(r, dict) else r[1]
            if src in bonhome["ids_by_portal"]:
                bonhome["ids_by_portal"][src].add(str(eid))

        cur.close()
    except Exception as e:
        log.exception(f"listings-qa DB query failed: {e}")
        return jsonify({"error": "db_query_failed", "detail": str(e)[:200]}), 500
    finally:
        if conn is not None:
            try:
                return_db(conn)
            except Exception:
                pass

    # --- 2) Scrape live Homegate + ImmoScout24 (vente + location) ---------
    sources = {}
    for portal_name, scraper_fn in (
        ('homegate', scrape_homegate),
        ('immoscout24', scrape_immoscout),
    ):
        db_ids = bonhome["ids_by_portal"].get(portal_name, set())
        portal_block = {}
        for transaction in ('location', 'achat'):
            listings, elapsed_ms, err = _safe_scrape(
                scraper_fn, portal_name, city_display, transaction
            )
            live_ids = [str(l.get('external_id')) for l in listings if l.get('external_id')]
            stats = _recall(live_ids, db_ids)
            stats["elapsed_ms"] = elapsed_ms
            if err:
                stats["error"] = err
            portal_block[transaction] = stats
        sources[portal_name + '_live'] = portal_block

    total_elapsed_ms = round((time.time() - t_start) * 1000, 1)

    # Ne pas leak les sets dans la réponse
    bonhome_out = {
        "total": bonhome["total"],
        "by_transaction": bonhome["by_transaction"],
        "by_source": bonhome["by_source"],
    }

    return jsonify({
        "env": os.environ.get('FLASK_ENV', 'development'),
        "city": city_slug,
        "city_display": city_display,
        "bonhome_indexed": bonhome_out,
        "sources": sources,
        "elapsed_ms": total_elapsed_ms,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }), 200
