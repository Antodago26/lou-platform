"""
Migrations de schéma v6.4.2 — extensions DB pour la Phase 2 du cron
`lou-qa-recall` (link health check, à venir au commit suivant).

NB : "v6.4.2" = numéro de schéma (séquence schema_v632 → v640 → v641 → v642),
sans rapport avec le commit code v6.4.2 qui était purement code (pagination
empty-page detection).

Trois ajouts, tous via ALTER TABLE ... ADD COLUMN IF NOT EXISTS (pattern
identique à first_seen_at dans app.py:_run_migrations) :

1. `properties.last_checked_at TIMESTAMPTZ` (NULL par défaut)
   - Renseigné par le link_health_worker après chaque HEAD
   - Permet de prioriser les annonces à re-checker via :
       WHERE is_active = TRUE
         AND (last_checked_at IS NULL OR last_checked_at < NOW() - INTERVAL '7 days')
       ORDER BY last_checked_at NULLS FIRST
   - Index partiel sur is_active=TRUE (les annonces désactivées n'ont pas
     besoin d'être re-checkées).

2. `qa_link_checks.status TEXT` — sémantique fixée par CEO 2026-04-26 :
     - `'ok'`          : HTTP 200/2xx — rien à faire
     - `'redirect'`    : HTTP 3xx vers une URL différente du même portail —
                         logger dans `final_url`, pas d'alerte
     - `'broken'`      : HTTP 404, 410, ou 5xx persistant après retry —
                         l'annonce sera masquée côté front
     - `'unreachable'` : 403 anti-bot, timeout, DNS, network error —
                         log silencieux, **NE PAS masquer l'annonce**.
                         Distingo critique : un DataDome/Cloudflare qui
                         bloque notre HEAD ≠ une annonce morte.

3. `qa_link_checks.final_url TEXT` — destination des redirects 3xx
   (pour pouvoir détecter si Homegate a restructuré ses URLs : tous les
   liens redirigent vers la home page = layout change, pas annonces mortes).

4. `qa_link_checks.error_msg TEXT` — message d'exception tronqué pour
   debug ('Connection timed out', 'Name resolution failure', etc.).

`ok BOOLEAN` original (créé par v640) est CONSERVÉ pour compat. Le worker
écrira les deux : `ok = (status in ('ok', 'redirect'))`. Permet de basculer
les requêtes existantes vers `status` à terme sans casser le présent.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def ensure_properties_last_checked_at(conn) -> None:
    """Ajoute `properties.last_checked_at TIMESTAMPTZ` + index partiel.

    L'index ne couvre que `is_active = TRUE` : les annonces désactivées
    (purgées par le cron de scrape, > 21j sans update) n'ont pas besoin
    d'être re-checkées. `NULLS FIRST` donne la priorité aux annonces jamais
    checkées dans le scan de sélection.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE properties
            ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_properties_last_checked_at
            ON properties (last_checked_at NULLS FIRST)
            WHERE is_active = TRUE
        """)
    finally:
        cur.close()
    conn.commit()


def ensure_qa_link_checks_extensions(conn) -> None:
    """Étend `qa_link_checks` (créée par v640) avec status TEXT, final_url
    TEXT, error_msg TEXT. Le `ok BOOLEAN` original reste pour compat.

    Index supplémentaire sur (status, checked_at DESC) avec WHERE clause
    partielle (status IS NOT NULL) — utile pour les requêtes du futur
    dashboard "tous les broken récents" : le predicate partiel évite
    d'indexer les anciens rows v6.4.1 où `status` est NULL.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE qa_link_checks
            ADD COLUMN IF NOT EXISTS status TEXT
        """)
        cur.execute("""
            ALTER TABLE qa_link_checks
            ADD COLUMN IF NOT EXISTS final_url TEXT
        """)
        cur.execute("""
            ALTER TABLE qa_link_checks
            ADD COLUMN IF NOT EXISTS error_msg TEXT
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_qa_link_checks_status_checked
            ON qa_link_checks (status, checked_at DESC)
            WHERE status IS NOT NULL
        """)
    finally:
        cur.close()
    conn.commit()


def ensure_qa_link_checks_advisory_comment(conn) -> None:
    """v6.4.6 : pose un COMMENT ON TABLE pour avertir au niveau Postgres
    que les classifications de qa_link_checks sont advisory et NE DOIVENT
    PAS être consommées par des queries user-facing tant que le workflow
    `LINK_HEALTH_AUTO_HIDE` n'est pas validé.

    Visible via `\\d+ qa_link_checks` ou `SELECT obj_description('qa_link_checks'::regclass)`.
    Idempotent : COMMENT remplace toute valeur précédente, safe à relancer.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            COMMENT ON TABLE qa_link_checks IS
            'advisory data — do NOT consume in user-facing queries until LINK_HEALTH_AUTO_HIDE workflow is validated'
        """)
    finally:
        cur.close()
    conn.commit()


def run_schema_v642(conn) -> dict:
    """Point d'entrée v6.4.2 — à appeler depuis `app.py` APRÈS
    `run_schema_v641` (logique : v642 étend une table créée par v640 et
    une autre — `properties` — créée par le bootstrap initial).

    Idempotent : safe à relancer à chaque boot. Marquage `schema_v642`
    dans `migrations_applied` uniquement si toutes les étapes ont passé
    (sinon le prochain boot retentera).
    """
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    steps = [
        ('properties_last_checked_at',          ensure_properties_last_checked_at),
        ('qa_link_checks_extensions',           ensure_qa_link_checks_extensions),
        ('qa_link_checks_advisory_comment',     ensure_qa_link_checks_advisory_comment),
    ]
    all_ok = True
    for label, fn in steps:
        try:
            fn(conn)
            stats[label] = 'ok'
        except Exception as e:
            log.exception("%s failed: %s", label, e)
            stats[label] = f'error: {e}'
            all_ok = False

    if all_ok:
        try:
            mark_applied(
                conn,
                'schema_v642',
                notes=(
                    'link health: properties.last_checked_at + '
                    'qa_link_checks.{status,final_url,error_msg}'
                ),
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v642 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (some step failed)'

    return stats
