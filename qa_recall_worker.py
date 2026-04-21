"""
Bon Home — QA Recall Worker (v6.4.0).

Calcule le snapshot de recall pour UNE ville : scrape live les 4 combos
(Homegate + ImmoScout24, vente + location), compare avec ce qu'on a
indexé côté DB, insère 1 row dans `qa_recall_snapshots` + gère le
registre `qa_runs`.

Point d'entrée : `run_recall_snapshot_for_city(city_slug) -> dict`.

Invoqué par `cron_job_qa_recall.py` (Render cron, 04:00 UTC).
Pas de thread, pas de lock : Render garantit qu'un seul cron tourne.

Réutilise :
  - `scrape_homegate`, `scrape_immoscout` (inchangés, importés depuis scrapers.py)
  - `sb_budget(120)` par ville pour cap le coût ScrapingBee. Si un combo
    épuise le budget, `_sb_get` renvoie (0, '') → le scraper retourne
    [] → on consigne source_total=0 dans le breakdown pour ce combo.
    Pas de crash, pas de retry (on réessayera demain).
  - `get_db` / `return_db` avec pattern `conn_broken` pour éviter le
    SSL-poisoning du pool Neon (cf. fix P0 v6.3.4).

Un run produit TOUJOURS 1 row `qa_recall_snapshots` (même si tous les
combos échouent) et 1 row `qa_runs`, pour qu'on puisse diagnostiquer via
DB même quand ScrapingBee est complètement down.
"""
import json
import logging
import time
import psycopg2

from db import get_db, return_db
from scrapers import scrape_homegate, scrape_immoscout, sb_budget

log = logging.getLogger('lou-app')

# Slug → display name (le scraper attend le nom avec accents, la DB
# stocke aussi le display name dans `properties.city`).
#
# DUPLIQUÉ depuis routes_stats.py volontairement : le worker tourne
# dans le cron (pas de Flask), on ne veut pas charger routes_stats
# + Blueprint pour 30 lignes de data. Maintenir synchrone manuellement
# si on ajoute des villes côté endpoint.
_CITY_SLUG_TO_DISPLAY = {
    # Villes du beta Neuchâtel
    'peseux':            'Peseux',
    'neuchatel':         'Neuchâtel',
    'la-chaux-de-fonds': 'La Chaux-de-Fonds',
    'le-locle':          'Le Locle',
    'cortaillod':        'Cortaillod',
    'colombier-ne':      'Colombier',
    'boudry':            'Boudry',
    # Grandes villes romandes
    'lausanne':          'Lausanne',
    'geneve':            'Genève',
    'fribourg':          'Fribourg',
    'sion':              'Sion',
}

# Pagination max par combo. Suffisant pour Genève/Lausanne (les grosses)
# ; les petites communes exit early via `consecutive_errors >= 2` dans
# les scrapers. 20 pages × 20-30 listings ≈ 400-600 listings max par
# combo, largement au-dessus de la réalité romande.
_MAX_PAGES_PER_COMBO = 20

# Budget sb_budget par ville (les 4 combos partagent). 120s est tight
# pour Geneva sur ImmoScout (render_js=True = 5 crédits/page, ~4s/page) :
# prévoir que certains combos passent en "budget exhausted" → source_total=0
# consigné dans le breakdown. Acceptable — le run suivant réessayera.
_SB_BUDGET_PER_CITY_S = 120

# Caps anti-bloat pour les JSONB.
_MAX_MISSING_PER_COMBO = 50
_MAX_MISSING_TOP_LEVEL = 200


# --- DB helpers ------------------------------------------------------------

def _fetch_our_ids(conn, city_slug: str, city_display: str,
                   portal: str, transaction: str) -> set:
    """IDs externes qu'on a indexés pour ce (city × portal × transaction).

    Union de `properties.external_id` ET `property_sources.external_id`
    (cross-portal dedup : une même annonce apparaît parfois dans les
    deux tables quand elle est cross-postée).

    Match sur `city` en tolérant display name ('Neuchâtel') OU slug
    ('neuchatel'), comme l'ancien endpoint.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT eid FROM (
                SELECT p.external_id AS eid
                FROM properties p
                WHERE p.is_active = TRUE
                  AND LOWER(COALESCE(p.city, '')) IN (%s, %s)
                  AND p.transaction = %s
                  AND LOWER(COALESCE(p.source, '')) = %s
                  AND p.external_id IS NOT NULL
                UNION ALL
                SELECT ps.external_id AS eid
                FROM property_sources ps
                JOIN properties p ON p.id = ps.property_id
                WHERE p.is_active = TRUE
                  AND LOWER(COALESCE(p.city, '')) IN (%s, %s)
                  AND p.transaction = %s
                  AND LOWER(COALESCE(ps.source, '')) = %s
                  AND ps.external_id IS NOT NULL
            ) x
        """, (
            city_display.lower(), city_slug, transaction, portal,
            city_display.lower(), city_slug, transaction, portal,
        ))
        rows = cur.fetchall()
    finally:
        cur.close()
    out = set()
    for r in rows:
        v = r[0] if not isinstance(r, dict) else list(r.values())[0]
        if v is not None:
            out.add(str(v))
    return out


def _create_run(conn, city_slug: str) -> int:
    """Insère un row qa_runs en status='running' et retourne son id."""
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO qa_runs (run_type, status, metadata)
            VALUES ('recall', 'running', %s::jsonb)
            RETURNING id
        """, (json.dumps({"city": city_slug, "stage": "opened"}),))
        row = cur.fetchone()
    finally:
        cur.close()
    conn.commit()
    rid = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(rid)


def _finalize_run(conn, run_id: int, status: str,
                  listings_processed: int, errors_count: int,
                  metadata: dict) -> None:
    """Marque un run comme terminé."""
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE qa_runs
            SET status = %s,
                completed_at = NOW(),
                listings_processed = %s,
                errors_count = %s,
                metadata = %s::jsonb
            WHERE id = %s
        """, (status, listings_processed, errors_count, json.dumps(metadata), run_id))
    finally:
        cur.close()
    conn.commit()


def _insert_snapshot(conn, city_slug: str, source_total: int, our_total: int,
                     recall_pct, missing_all: list, breakdown: dict) -> int:
    """Insère un row qa_recall_snapshots et retourne son id."""
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO qa_recall_snapshots
                (city, source_total_listings, our_total_listings, recall_pct,
                 missing_listing_ids, raw_snapshot)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id
        """, (
            city_slug, source_total, our_total, recall_pct,
            json.dumps(missing_all), json.dumps(breakdown),
        ))
        row = cur.fetchone()
    finally:
        cur.close()
    conn.commit()
    sid = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(sid)


# --- Scrape helper ---------------------------------------------------------

def _run_scraper(portal: str, city_display: str, transaction: str) -> list:
    """Résout le scraper AU MOMENT DE L'APPEL (pas à l'import) pour que
    les tests puissent patcher `qa_recall_worker.scrape_homegate` /
    `qa_recall_worker.scrape_immoscout`. Retourne [] si le scraper lève."""
    fn = scrape_homegate if portal == 'homegate' else scrape_immoscout
    try:
        return fn(
            city=city_display,
            transaction=transaction,
            max_pages=_MAX_PAGES_PER_COMBO,
        ) or []
    except Exception as e:
        log.exception(f"[qa-recall] scrape failed {portal}/{transaction}/{city_display}: {e}")
        return []


# --- Entry point -----------------------------------------------------------

def run_recall_snapshot_for_city(city_slug: str) -> dict:
    """Scrape live + compare DB + insère un snapshot pour UNE ville.

    Flow :
      1. Crée 1 row qa_runs en status='running' (conn 1).
      2. Ouvre 1 conn (conn 2) pour tous les combos : 4 scrape live +
         4 SELECTs sur properties, sous `sb_budget(120)`. Les erreurs par
         combo sont consignées dans le breakdown sans faire sauter le run.
      3. Insère 1 row qa_recall_snapshots + finalize qa_runs (conn 3).

    Utilise 3 conns séparées volontairement : si la conn 2 se corrompt
    (SSL bad record mac pendant un SELECT lourd), la finalisation peut
    quand même écrire le snapshot avec une conn fraîche — l'important
    est qu'on ait TOUJOURS une trace DB de la tentative.

    Statuts possibles dans qa_runs :
      - 'success' si errors == 0
      - 'partial' si 1 ≤ errors < 4
      - 'failed'  si errors == 4 (tous les combos ont échoué)

    Raises ValueError si city_slug n'est pas dans _CITY_SLUG_TO_DISPLAY.
    Propage les psycopg2.OperationalError si les conns 1 ou 3 échouent
    (dans ce cas on n'a littéralement pas pu écrire en DB — le cron log
    la ville comme erreur et passe à la suivante).
    """
    city_display = _CITY_SLUG_TO_DISPLAY.get(city_slug)
    if not city_display:
        raise ValueError(f"unknown city slug: {city_slug}")

    log.info(f"[qa-recall] start city={city_slug} display={city_display!r}")
    t_start = time.time()

    # ---------- 1) Open qa_runs row (conn 1) ----------
    conn = None
    conn_broken = False
    run_id = None
    try:
        conn = get_db()
        run_id = _create_run(conn, city_slug)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[qa-recall] create_run transport error for {city_slug}: {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # ---------- 2) Scrape + compare (conn 2) ----------
    breakdown = {}
    all_missing = []
    total_source = 0
    total_our = 0
    errors = 0

    combos = [
        ('homegate',    'achat'),
        ('homegate',    'location'),
        ('immoscout24', 'achat'),
        ('immoscout24', 'location'),
    ]

    conn = None
    conn_broken = False
    try:
        conn = get_db()
        with sb_budget(_SB_BUDGET_PER_CITY_S):
            for portal, transaction in combos:
                key = f"{portal}_{transaction}"
                try:
                    listings = _run_scraper(portal, city_display, transaction)
                    live_ids = {
                        str(l.get('external_id'))
                        for l in listings
                        if l.get('external_id')
                    }
                    our_ids = _fetch_our_ids(conn, city_slug, city_display, portal, transaction)
                    missing = sorted(live_ids - our_ids)
                    recall = (
                        round(len(our_ids & live_ids) / len(live_ids) * 100, 2)
                        if live_ids else None
                    )
                    breakdown[key] = {
                        "source_total": len(live_ids),
                        "our_total": len(our_ids),
                        "recall_pct": recall,
                        "missing_ids": missing[:_MAX_MISSING_PER_COMBO],
                    }
                    total_source += len(live_ids)
                    total_our += len(our_ids)
                    for mid in missing:
                        all_missing.append({
                            "portal": portal,
                            "transaction": transaction,
                            "id": mid,
                        })
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    # La conn 2 est morte : inutile de continuer les combos
                    # suivants, ils échoueront pareil. On break et finalise.
                    conn_broken = True
                    breakdown[key] = {"error": f"db_transient: {str(e)[:150]}"}
                    errors += 1
                    log.error(f"[qa-recall] DB transport error {key}/{city_slug}: {e}", exc_info=True)
                    break
                except Exception as e:
                    breakdown[key] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
                    errors += 1
                    log.exception(f"[qa-recall] combo {key}/{city_slug} failed")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # Si la conn 2 est tombée en plein milieu, combler les combos restants
    # avec un marker d'erreur — le snapshot raw reflète alors clairement
    # qu'on n'a pas pu tester tous les combos.
    for portal, transaction in combos:
        key = f"{portal}_{transaction}"
        if key not in breakdown:
            breakdown[key] = {"error": "skipped: prior transport error"}
            errors += 1

    all_missing_capped = all_missing[:_MAX_MISSING_TOP_LEVEL]
    recall_pct = round(total_our / total_source * 100, 2) if total_source else None
    elapsed_s = round(time.time() - t_start, 1)

    # ---------- 3) Insert snapshot + finalize run (conn 3) ----------
    conn = None
    conn_broken = False
    snapshot_id = None
    try:
        conn = get_db()
        snapshot_id = _insert_snapshot(
            conn, city_slug, total_source, total_our,
            recall_pct, all_missing_capped, breakdown,
        )
        status = 'success' if errors == 0 else ('partial' if errors < 4 else 'failed')
        _finalize_run(
            conn, run_id, status,
            listings_processed=total_source,
            errors_count=errors,
            metadata={
                "city": city_slug,
                "snapshot_id": snapshot_id,
                "portals_scraped": 4,
                "combos_with_error": errors,
                "elapsed_s": elapsed_s,
            },
        )
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[qa-recall] finalize transport error for {city_slug}: {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    log.info(
        f"[qa-recall] done city={city_slug} source={total_source} "
        f"our={total_our} recall={recall_pct}% errors={errors} "
        f"elapsed={elapsed_s}s snapshot_id={snapshot_id}"
    )
    return {
        "city": city_slug,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "source_total": total_source,
        "our_total": total_our,
        "recall_pct": recall_pct,
        "errors": errors,
        "elapsed_s": elapsed_s,
    }
