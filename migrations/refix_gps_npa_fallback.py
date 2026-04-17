"""
v6.3.2 — Refix GPS des biens en NPA-fallback après correction de NPA_COORDS.

Contexte : le commit 83953f3 a corrigé 11 entrées >5km fausses de NPA_COORDS
(et 8+ autres plus modérées). Mais les ~1282 biens (sur 4937) dont les lat/lng
ont été remplies via NPA_COORDS lors du backfill v6.3 conservent en DB les
ANCIENNES coords fausses. Ce script les re-résout via le NOUVEAU _lookup_city_coords.

Stratégie de détection (en l'absence de colonne gps_source historique) :
  "coords rondes à 3 décimales" = signature du dict Python vs GPS scrapé
  qui a 5-6 décimales. Faux positifs rares ; coût d'un faux positif : un
  GPS précis remplacé par le centre de commune (décalage ~100-500m max).

Modes :
  --dry-run   : échantillonne + compte sans UPDATE (par défaut)
  --apply     : exécute les UPDATE
  --limit N   : limite le scan (debug)

Env var `V632_REFIX_MODE` : dry-run | apply | skip (pour hook boot).

Enregistrement dans migrations_applied (name='v632_refix_gps_npa_fallback')
pour éviter la ré-exécution.
"""
import argparse
import logging
import os
import re
import sys
from typing import Optional, Tuple

logger = logging.getLogger('lou-app')

MIGRATION_NAME = 'v632_refix_gps_npa_fallback'


# ------------------------------------------------------------------
# Detection SQL : "coords rondes à 3 décimales"
# ------------------------------------------------------------------
# ABS(lat * 1000 - ROUND(lat * 1000)) < 1e-6
# + idem longitude
# + gps_source IS NULL (ignorer les biens déjà tagués après v6.3.2)
# + latitude/longitude IS NOT NULL
#
# Exclut les biens scrapés (qui ont habituellement 5-6 décimales).
_DETECT_SQL = """
SELECT id, title, address, city, latitude, longitude
FROM properties
WHERE latitude IS NOT NULL
  AND longitude IS NOT NULL
  AND gps_source IS NULL
  AND ABS(latitude * 1000 - ROUND(latitude * 1000)) < 1e-6
  AND ABS(longitude * 1000 - ROUND(longitude * 1000)) < 1e-6
"""


def _recompute_coords(row, lookup_city_coords) -> Optional[Tuple[float, float]]:
    """Re-résout les coords via _lookup_city_coords : NPA extrait address, puis city."""
    addr = row.get('address')
    city = row.get('city')
    coords = None
    if addr:
        m = re.search(r'\b(\d{4})\b', addr)
        if m:
            coords = lookup_city_coords(m.group(1))
    if not coords and city:
        coords = lookup_city_coords(city)
    return coords


def run(conn, lookup_city_coords, dry_run: bool = True, sample_size: int = 10, limit: int = None) -> dict:
    """
    Exécute le refix.

    Returns stats dict :
      detected, updated, unchanged_after_recompute, unresolvable, samples
    """
    cur = conn.cursor()
    sql = _DETECT_SQL
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    cur.execute(sql)
    candidates = cur.fetchall()

    detected = len(candidates)
    updated = 0
    unchanged = 0
    unresolvable = 0
    samples_updated = []
    samples_unchanged = []
    samples_unresolvable = []

    for row in candidates:
        old_lat = float(row['latitude'])
        old_lng = float(row['longitude'])
        coords = _recompute_coords(row, lookup_city_coords)

        if coords is None:
            unresolvable += 1
            if len(samples_unresolvable) < 5:
                samples_unresolvable.append({
                    'id': row['id'],
                    'address': row.get('address'),
                    'city': row.get('city'),
                    'old': (old_lat, old_lng),
                })
            continue

        new_lat, new_lng = coords
        # Compare à ~1m (5e-6 deg ~ 0.5m en latitude)
        if abs(new_lat - old_lat) < 5e-6 and abs(new_lng - old_lng) < 5e-6:
            unchanged += 1
            if len(samples_unchanged) < 5:
                samples_unchanged.append({
                    'id': row['id'],
                    'city': row.get('city'),
                    'coords': (old_lat, old_lng),
                })
            continue

        # Change détecté
        if len(samples_updated) < sample_size:
            samples_updated.append({
                'id': row['id'],
                'address': row.get('address'),
                'city': row.get('city'),
                'before': (old_lat, old_lng),
                'after': (new_lat, new_lng),
            })

        if not dry_run:
            cur.execute(
                "UPDATE properties SET latitude=%s, longitude=%s, gps_source='npa_fallback' WHERE id=%s",
                (new_lat, new_lng, row['id']),
            )
        updated += 1

    if not dry_run:
        conn.commit()
    cur.close()

    stats = {
        'mode': 'dry-run' if dry_run else 'apply',
        'detected': detected,
        'updated': updated,
        'unchanged_after_recompute': unchanged,
        'unresolvable': unresolvable,
        'samples_updated': samples_updated,
        'samples_unchanged': samples_unchanged,
        'samples_unresolvable': samples_unresolvable,
    }
    return stats


def run_at_boot(conn, lookup_city_coords) -> dict:
    """
    Hook boot — lit V632_REFIX_MODE :
      - 'skip'    : ne fait rien
      - 'dry-run' : log les stats et samples (pour validation humaine)
      - 'apply'   : exécute + marque dans migrations_applied
    Si la migration est déjà marquée appliquée, skip automatique.
    """
    from .schema_v632 import is_applied, mark_applied

    mode = os.environ.get('V632_REFIX_MODE', 'skip').lower().strip()
    if mode not in ('skip', 'dry-run', 'apply'):
        logger.warning(f"V632_REFIX_MODE invalide '{mode}', skip")
        return {'mode': mode, 'status': 'invalid_env_var'}

    if mode == 'skip':
        return {'mode': 'skip', 'status': 'skipped'}

    if is_applied(conn, MIGRATION_NAME):
        logger.info(f"{MIGRATION_NAME} déjà appliqué, skip")
        return {'mode': mode, 'status': 'already_applied'}

    dry = (mode == 'dry-run')
    logger.info(f"=== {MIGRATION_NAME} START mode={mode} ===")
    stats = run(conn, lookup_city_coords, dry_run=dry)
    logger.info(f"=== {MIGRATION_NAME} stats: detected={stats['detected']} "
                f"updated={stats['updated']} unchanged={stats['unchanged_after_recompute']} "
                f"unresolvable={stats['unresolvable']} mode={stats['mode']} ===")
    # Log samples pour validation visuelle
    for s in stats['samples_updated'][:10]:
        logger.info(f"  UPDATE sample id={s['id']} city={s['city']!r} "
                    f"before={s['before']} after={s['after']}")
    for s in stats['samples_unchanged'][:5]:
        logger.info(f"  UNCHANGED sample id={s['id']} city={s['city']!r} coords={s['coords']}")
    for s in stats['samples_unresolvable'][:5]:
        logger.info(f"  UNRESOLVABLE sample id={s['id']} address={s['address']!r} "
                    f"city={s['city']!r} old={s['old']}")

    if not dry:
        mark_applied(conn, MIGRATION_NAME, notes=f"updated={stats['updated']}")

    stats['status'] = 'applied' if not dry else 'dry_run_complete'
    return stats


# ------------------------------------------------------------------
# CLI standalone
# ------------------------------------------------------------------

def _main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='v6.3.2 refix GPS NPA-fallback')
    parser.add_argument('--apply', action='store_true', help='exécute les UPDATE (par défaut: dry-run)')
    parser.add_argument('--limit', type=int, default=None, help='limite le scan')
    parser.add_argument('--sample-size', type=int, default=10, help='nombre de samples à afficher')
    args = parser.parse_args()

    # Charge app root dans le path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db import get_db, return_db
    from scoring_engine import _lookup_city_coords

    dry = not args.apply
    conn = get_db()
    try:
        stats = run(conn, _lookup_city_coords, dry_run=dry,
                    sample_size=args.sample_size, limit=args.limit)
    finally:
        return_db(conn)

    import json
    print(json.dumps(stats, indent=2, default=str))

    if args.apply and stats['updated'] > 0:
        # Marque appliqué
        from migrations.schema_v632 import ensure_migrations_table, mark_applied
        conn = get_db()
        try:
            ensure_migrations_table(conn)
            mark_applied(conn, MIGRATION_NAME, notes=f"cli updated={stats['updated']}")
        finally:
            return_db(conn)


if __name__ == '__main__':
    _main()
