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
import os
import time
from itertools import product
import psycopg2

from db import get_db, return_db
from scrapers import (
    scrape_homegate,
    scrape_immoscout,
    sb_budget,
    sb_bypass_cache,
)

log = logging.getLogger('lou-app')

# Slug → display name (le scraper attend le nom avec accents, la DB
# stocke aussi le display name dans `properties.city`).
#
# DUPLIQUÉ depuis routes_stats.py volontairement : le worker tourne
# dans le cron (pas de Flask), on ne veut pas charger routes_stats
# + Blueprint pour 30 lignes de data. Maintenir synchrone manuellement
# si on ajoute des villes côté endpoint.
_CITY_SLUG_TO_DISPLAY = {
    # -----------------------------------------------------------------
    # Canton Neuchâtel — focus beta (QA_RECALL_CITIES par défaut)
    # -----------------------------------------------------------------
    'peseux':            'Peseux',
    'neuchatel':         'Neuchâtel',
    'la-chaux-de-fonds': 'La Chaux-de-Fonds',
    'le-locle':          'Le Locle',
    'boudry':            'Boudry',
    'cortaillod':        'Cortaillod',
    # Colombier : 'colombier' est le slug court ; le scraper ajoute
    # automatiquement le suffixe '-ne' via CITY_CANTONS pour produire
    # l'URL Homegate (city-colombier-ne). On garde l'alias explicite
    # 'colombier-ne' pour compat avec les anciens rapports/dashboards.
    'colombier':         'Colombier',
    'colombier-ne':      'Colombier',
    # Marin : la commune a fusionné en 2009 pour former "La Tène", mais
    # Homegate continue de lister sous "marin-epagnier" (CITY_CANTONS
    # n'a pas 'marin' mais a 'marin-epagnier'). Slug court 'marin' →
    # display historique pour que le scraper génère city-marin-epagnier.
    'marin':             'Marin-Epagnier',
    'marin-epagnier':    'Marin-Epagnier',
    # Saint-Blaise : idem colombier, scraper ajoute -ne automatiquement.
    'saint-blaise':      'Saint-Blaise',
    'saint-blaise-ne':   'Saint-Blaise',
    # Autres communes NE — disponibles si QA_RECALL_CITIES est étendu
    # via env (sinon pas scrapées par défaut).
    'hauterive':         'Hauterive',       # scraper ajoute -ne
    'hauterive-ne':      'Hauterive',
    'bevaix':            'Bevaix',
    'milvignes':         'Milvignes',
    'la-tene':           'La Tène',         # nom post-fusion 2009
    'le-landeron':       'Le Landeron',
    'val-de-ruz':        'Val-de-Ruz',
    'val-de-travers':    'Val-de-Travers',
    # -----------------------------------------------------------------
    # Grandes villes romandes — HORS scope par défaut. Incluses ici
    # uniquement pour que `QA_RECALL_CITIES` puisse les pointer sans
    # ValueError si Antony décide d'étendre la couverture plus tard.
    # -----------------------------------------------------------------
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

# Budget sb_budget par PORTAIL (depuis v6.4.3, BUG A). Avant : 300s
# partagés entre les 4 combos (Homegate vente+loc + IS24 vente+loc).
# Observation run nocturne 22/04 : Homegate (slow, 25-96s par page vide
# côté ScrapingBee) cramait à lui seul les 300s sur 7 des 8 villes →
# IS24 jamais appelé ("budget exhausted, skipping").
#
# Fix : 300s par portail, séquentiels. Total max par ville = 600s, soit
# 8 villes × 600s = 80 min max pour le cron nocturne. Acceptable à 04:00 UTC.
#
# Le nom env var `LISTINGS_QA_SCRAPE_BUDGET_S` est CONSERVÉ pour compat
# Render dashboard, mais sa sémantique est désormais "budget par portail",
# pas "par ville". Documenté en clair pour éviter confusion future.
_SB_BUDGET_PER_PORTAL_S = int(os.environ.get('LISTINGS_QA_SCRAPE_BUDGET_S', '300'))

# Ordres déterministes pour que le breakdown JSONB soit comparable d'un
# run à l'autre (clés stables = `homegate_achat`, `homegate_location`).
#
# v6.4.4 : ImmoScout24 retiré du monitoring (DataDome bloque le scraping
# stealth, premium_proxy 5× plus cher = mauvais alignement business). La
# fonction scrape_immoscout reste dans scrapers.py (deprecated mais gardée
# pour référence). On ne monitore plus que Homegate. Si on ajoute un autre
# portail (ex: scraping direct d'agences immo), ré-étendre _PORTALS suffit
# — la logique de budget nested par portail est préservée.
_PORTALS = ('homegate',)
_TRANSACTIONS = ('achat', 'location')

# Caps anti-bloat pour les JSONB.
_MAX_MISSING_PER_COMBO = 50
_MAX_MISSING_TOP_LEVEL = 200

# Clamp du recall_pct. La colonne est NUMERIC(7,2) en DB depuis v6.4.1
# (max 99999.99). Le clamp Python est un filet de sécurité — bridé
# bien en-dessous de la limite pour laisser un poil de marge aux
# arrondis psycopg2→NUMERIC.
_RECALL_PCT_MAX = 99999.99


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

    conn = None
    conn_broken = False
    try:
        conn = get_db()
        # v6.4.1 fix CRITIQUE : sb_bypass_cache() est indispensable. Sans ça,
        # _sb_get hitte le cache DB de SCRAPE_CACHE_TTL (12h) et renvoie
        # (304, '') — les scrapers retournent alors [] et source_total=0
        # pour toutes les villes après le 1er run quotidien. Le snapshot
        # est mensonger. Restauré au commit 2.1 après omission au refactor.
        #
        # v6.4.3 BUG A : sb_budget est désormais NESTED par portail (un
        # `with sb_budget(300)` séparé pour Homegate puis pour ImmoScout)
        # au lieu d'un budget global partagé entre les 4 combos. Avant,
        # Homegate (slow ScrapingBee 25-96s/page vide) cramait les 300s
        # avant qu'IS24 soit appelé sur 7/8 villes. Maintenant chaque
        # portail a son propre 300s frais — IS24 est garanti d'avoir sa
        # chance même si Homegate consomme tout son budget.
        with sb_bypass_cache():
            for portal in _PORTALS:
                if conn_broken:
                    break  # conn morte sur portail précédent — inutile de continuer
                with sb_budget(_SB_BUDGET_PER_PORTAL_S):
                    for transaction in _TRANSACTIONS:
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
                            # Intersection-based recall (borné [0, 100] par
                            # construction). Clamp défensif à _RECALL_PCT_MAX
                            # — paranoïa pure, ne devrait jamais s'activer ici.
                            recall = (
                                round(min(len(our_ids & live_ids) / len(live_ids) * 100,
                                          _RECALL_PCT_MAX), 2)
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
                            # Conn morte : inutile de continuer les combos
                            # suivants (de ce portail OU du portail suivant),
                            # ils échoueront pareil. On break inner ; le check
                            # `if conn_broken` en haut du for-portal bloque le
                            # portail suivant.
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
    for portal, transaction in product(_PORTALS, _TRANSACTIONS):
        key = f"{portal}_{transaction}"
        if key not in breakdown:
            breakdown[key] = {"error": "skipped: prior transport error"}
            errors += 1

    all_missing_capped = all_missing[:_MAX_MISSING_TOP_LEVEL]
    # Top-level = ratio des totaux (pas intersection). Peut dépasser 100%
    # quand our_total > source_total (DB plus riche que le live scraping :
    # signal diagnostique de DB stale ou scraper incomplet — on garde la
    # valeur réelle). Clamp à _RECALL_PCT_MAX (99999.99) pour que l'INSERT
    # passe même sur cas pathologiques (source_total=1, our=380 → 38000%).
    recall_pct = (
        round(min(total_our / total_source * 100, _RECALL_PCT_MAX), 2)
        if total_source else None
    )
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
        # v6.4.4 : seuil success/partial/failed dérivé du nombre réel de
        # combos (avant : hardcodé à 4 → cassé quand IS24 retiré).
        total_combos = len(_PORTALS) * len(_TRANSACTIONS)
        status = (
            'success' if errors == 0
            else ('partial' if errors < total_combos else 'failed')
        )
        _finalize_run(
            conn, run_id, status,
            listings_processed=total_source,
            errors_count=errors,
            metadata={
                "city": city_slug,
                "snapshot_id": snapshot_id,
                # Renommé en v6.4.4 — l'ancien `portals_scraped: 4` était
                # confusément nommé (c'était les combos, pas les portails).
                "combos_total": total_combos,
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
