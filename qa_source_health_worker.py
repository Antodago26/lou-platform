"""
Bon Home — QA Source Health Worker (v6.4.4 — repurpose Phase 1 cron lou-qa-recall).

Audit santé per-source : pour chaque `properties.source` distinct, calcule
total_active / scraped_7d / scraped_30d / last_scrape, dérive un statut
ok|warn|fail, écrit 1 row par source dans `qa_source_health` + 1 row
`qa_runs` (status='success'|'partial'|'failed').

Remplace l'ancien `qa_recall_worker` après le drop produit Homegate +
ImmoScout24 (décision CEO 30/04). Pas de scrape live ici : on lit
uniquement la DB. Pas de budget ScrapingBee, pas de cache à bypasser, pas
de gestion de combo portail × transaction. Durée attendue : < 5s.

Statuts (cf. brief 30/04) :
  - ok    : scraped_7d > 0
  - warn  : scraped_7d == 0 ET last_scrape ≤ 21 jours
  - fail  : last_scrape > 21 jours OU last_scrape NULL

L'intention est de flagger une source comme 'fail' dès qu'elle n'a
RIEN ramené depuis 21j (≈ règle de désactivation des listings côté
cron prod). Le 7d/30d donnent une vue "fraîcheur courante" pour
distinguer un portail sain d'un portail qui a déjà commencé à
décrocher mais n'a pas encore franchi le seuil critique.

Point d'entrée : `run_source_health_snapshot() -> dict`. Appelé par
`cron_job_qa_recall.py` Phase 1 (Render cron lou-qa-recall, 04:00 UTC).
"""
import json
import logging
import time

import psycopg2

from db import get_db, return_db

log = logging.getLogger('lou-app')

# Seuils statut. Exposés en module-level pour qu'on puisse les ajuster
# depuis l'env si besoin sans toucher la logique (pas fait dans ce
# commit — pas de demande produit). Cf. tests pour les bornes exactes.
WARN_DAYS = 21


def _create_run(conn) -> int:
    """Insère un row qa_runs en status='running' et retourne son id.

    On réutilise l'ENUM `qa_run_type` existant : la valeur 'recall' couvre
    historiquement Phase 1 du cron lou-qa-recall. Garder 'recall' évite
    une migration ENUM ALTER (Postgres ne permet pas de renommer ou
    d'ajouter une valeur dans une transaction simple) et préserve la
    continuité de la timeline qa_runs côté dashboard.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO qa_runs (run_type, status, metadata)
            VALUES ('recall', 'running', %s::jsonb)
            RETURNING id
        """, (json.dumps({"phase": "source_health", "stage": "opened"}),))
        row = cur.fetchone()
    finally:
        cur.close()
    conn.commit()
    rid = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(rid)


def _finalize_run(conn, run_id: int, status: str,
                  sources_processed: int, errors_count: int,
                  metadata: dict) -> None:
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
        """, (status, sources_processed, errors_count, json.dumps(metadata), run_id))
    finally:
        cur.close()
    conn.commit()


def _fetch_per_source_stats(conn) -> list:
    """Une seule requête agrégée — bien plus rapide que N queries.

    NOTE : on filtre sur is_active = TRUE pour total_active, mais
    scraped_7d / scraped_30d / last_scrape regardent la table entière
    (y compris les rows désactivés). Raisonnement : si Homegate a 2530
    rows désactivés le 30/04 mais aucun scraped_at récent, on veut le
    voir en 'fail' — pas le masquer derrière un total_active=0 trompeur.

    Source absente de `properties` (ex: portail ajouté demain et pas
    encore scrappé) ne ressortira pas du tout. C'est OK : pas de bruit
    dans le dashboard tant qu'aucune donnée n'a été collectée.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                source,
                COUNT(*) FILTER (WHERE is_active = TRUE) AS total_active,
                COUNT(*) FILTER (WHERE scraped_at >= NOW() - INTERVAL '7 days')  AS scraped_7d,
                COUNT(*) FILTER (WHERE scraped_at >= NOW() - INTERVAL '30 days') AS scraped_30d,
                MAX(scraped_at) AS last_scrape
            FROM properties
            WHERE source IS NOT NULL AND source <> ''
            GROUP BY source
            ORDER BY source
        """)
        rows = cur.fetchall()
    finally:
        cur.close()

    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append({
                'source':       r['source'],
                'total_active': int(r['total_active'] or 0),
                'scraped_7d':   int(r['scraped_7d']   or 0),
                'scraped_30d':  int(r['scraped_30d']  or 0),
                'last_scrape':  r['last_scrape'],
            })
        else:
            out.append({
                'source':       r[0],
                'total_active': int(r[1] or 0),
                'scraped_7d':   int(r[2] or 0),
                'scraped_30d':  int(r[3] or 0),
                'last_scrape':  r[4],
            })
    return out


def _classify(stats: dict, now) -> str:
    """ok|warn|fail à partir des compteurs.

    Ordre des tests important : 'ok' court-circuite tout (un portail
    sain n'a pas besoin de check 21j). 'fail' couvre l'absence totale
    de données (last_scrape NULL — théoriquement impossible si la
    source est dans GROUP BY, mais on tolère).
    """
    if stats['scraped_7d'] > 0:
        return 'ok'
    last = stats['last_scrape']
    if last is None:
        return 'fail'
    age_days = (now - last).total_seconds() / 86400.0
    if age_days > WARN_DAYS:
        return 'fail'
    return 'warn'


def _insert_health_rows(conn, rows: list) -> None:
    """Insert batch (1 row par source) dans qa_source_health.

    Pas d'INSERT ... VALUES multi-row volontairement : la liste des
    sources est petite (~7 max actuellement), executemany suffit et
    laisse psycopg2 gérer les types proprement (notamment last_scrape
    None vs datetime).
    """
    cur = conn.cursor()
    try:
        cur.executemany("""
            INSERT INTO qa_source_health
                (source, total_active, scraped_7d, scraped_30d, last_scrape, status)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, [
            (r['source'], r['total_active'], r['scraped_7d'],
             r['scraped_30d'], r['last_scrape'], r['status'])
            for r in rows
        ])
    finally:
        cur.close()
    conn.commit()


def run_source_health_snapshot() -> dict:
    """Calcule un snapshot santé per-source et l'écrit en DB.

    Flow :
      1. Crée un row qa_runs en status='running' (conn 1).
      2. Lit les stats agrégées sur properties (conn 2). Classifie chaque
         source en ok|warn|fail. Insère 1 row qa_source_health par source.
      3. Finalize qa_runs avec status='success' (au moins 1 source) ou
         'failed' (aucune source — DB vide ou requête KO).

    Retourne un dict résumé pour log côté cron.

    Propage psycopg2.OperationalError si conn 1 ou 3 échouent (pas de
    snapshot écrit dans ce cas — Render flaggera le cron rouge via
    exit code 1).
    """
    log.info("[qa-source-health] start")
    t_start = time.time()

    # ---------- 1) Open qa_runs row ----------
    conn = None
    conn_broken = False
    run_id = None
    try:
        conn = get_db()
        run_id = _create_run(conn)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[qa-source-health] create_run transport error: {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # ---------- 2) Aggregate + classify + write rows ----------
    rows = []
    errors = 0
    conn = None
    conn_broken = False
    try:
        conn = get_db()
        stats_list = _fetch_per_source_stats(conn)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for s in stats_list:
            # Postgres timestamps reviennent tz-aware via psycopg2 si la
            # colonne est TIMESTAMPTZ ; properties.scraped_at est TIMESTAMP
            # (pas TZ) → naive. On force la même base avant soustraction
            # pour éviter "can't subtract offset-naive and offset-aware".
            last = s['last_scrape']
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            normalized = dict(s, last_scrape=last)
            normalized['status'] = _classify(normalized, now)
            rows.append(normalized)

        if rows:
            _insert_health_rows(conn, rows)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        errors += 1
        log.error(f"[qa-source-health] DB transport error: {e}", exc_info=True)
    except Exception as e:
        errors += 1
        log.exception(f"[qa-source-health] aggregate/insert failed: {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    elapsed_s = round(time.time() - t_start, 1)

    # Log per-source en clair pour Render dashboard.
    for r in rows:
        log.info(
            f"[qa-source-health] source={r['source']} status={r['status']} "
            f"total_active={r['total_active']} scraped_7d={r['scraped_7d']} "
            f"scraped_30d={r['scraped_30d']} last_scrape={r['last_scrape']}"
        )

    # ---------- 3) Finalize qa_runs ----------
    status = 'success' if (rows and errors == 0) else (
        'partial' if rows else 'failed'
    )
    conn = None
    conn_broken = False
    try:
        conn = get_db()
        # Compteur "listings_processed" réutilisé pour "sources_processed" —
        # même colonne, sémantique adaptée. Le metadata précise.
        _finalize_run(
            conn, run_id, status,
            sources_processed=len(rows),
            errors_count=errors,
            metadata={
                "phase": "source_health",
                "sources_total": len(rows),
                "elapsed_s": elapsed_s,
                "by_status": {
                    s: sum(1 for r in rows if r['status'] == s)
                    for s in ('ok', 'warn', 'fail')
                },
            },
        )
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[qa-source-health] finalize transport error: {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    log.info(
        f"[qa-source-health] done sources={len(rows)} status={status} "
        f"errors={errors} elapsed={elapsed_s}s"
    )
    return {
        "run_id": run_id,
        "sources_total": len(rows),
        "sources": [
            {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in r.items()}
            for r in rows
        ],
        "errors": errors,
        "status": status,
        "elapsed_s": elapsed_s,
    }
