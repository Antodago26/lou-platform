"""
Migrations de schéma v6.4.3 — backfill correctif des classifications 5xx
mal étiquetées en 'broken' au run cron `lou-qa-recall` du 26/04 (commit
d8fb6e2, Phase 2 link_health initiale).

Contexte : `_classify` mappait initialement les 5xx en 'broken'. Sur ce
1er run, ScrapingBee Premium a renvoyé HTTP 500 systematic sur ~110 URLs
Homegate (DataDome via SB ou SB-side failure indistinguable, body 33553
bytes consistent sans markers DataDome lisibles dans le HTML). Résultat :
111 Homegate étiquetés 'broken' à tort (false positives).

Fix code v6.4.6 : `_classify` mappe désormais 5xx → 'unreachable'
(safer default sur incertitude). Mais les rows existants en DB doivent
être backfillés.

Ce backfill ré-étiquette les rows pré-fix :
    UPDATE qa_link_checks
       SET status   = 'unreachable',
           error_msg = 'backfill_v6.4.6_5xx_was_misclassified_as_broken'
     WHERE status   = 'broken'
       AND http_status >= 500;

Idempotent via le registre `migrations_applied` (gate
`is_applied('schema_v643')`). One-shot : si on re-runnait par accident,
on ne ré-écraserait PAS des futurs rows légitimes (le fix v6.4.6
garantit qu'aucun nouveau 5xx n'aboutit en 'broken' depuis le déploiement).

À noter : pas de modification de `properties.last_checked_at` ni de
qa_runs ; on touche uniquement les rows qa_link_checks de la run faulty.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, is_applied, mark_applied

log = logging.getLogger('lou-app')


def run_schema_v643(conn) -> dict:
    """Point d'entrée v6.4.3. Appelé depuis `app.py` APRÈS
    `run_schema_v642` (qui pose le COMMENT ON TABLE advisory).

    Si déjà appliqué (registre `migrations_applied`) : skip silencieux.
    Si nouveau : exécute le UPDATE, log le rowcount, marque appliqué.
    """
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'
        return stats

    # Gate idempotente — backfill one-shot, ne jamais re-runner.
    # Sans ça, si un futur bug similaire mal étiquetait 5xx en broken et
    # qu'on re-runnait ce script, on ré-écraserait les rows légitimes.
    try:
        if is_applied(conn, 'schema_v643'):
            log.info("v6.4.3 backfill already applied — skipping (idempotent gate)")
            stats['backfill'] = 'skipped_already_applied'
            return stats
    except Exception as e:
        # Si la lecture du registre échoue, on préfère ne pas runner le
        # UPDATE plutôt que risquer un double backfill silencieux.
        log.exception("is_applied lookup failed, skipping backfill: %s", e)
        stats['backfill'] = f'error_lookup: {e}'
        return stats

    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE qa_link_checks
            SET status    = 'unreachable',
                error_msg = 'backfill_v6.4.6_5xx_was_misclassified_as_broken'
            WHERE status      = 'broken'
              AND http_status >= 500
        """)
        rowcount = cur.rowcount
    except Exception as e:
        log.exception("v6.4.3 backfill UPDATE failed: %s", e)
        stats['backfill'] = f'error: {e}'
        cur.close()
        return stats
    cur.close()
    conn.commit()

    log.info(
        f"v6.4.3 backfill: {rowcount} rows mis à jour "
        f"(status='broken' + http_status>=500 → 'unreachable'). "
        "Cible : false positives du run d8fb6e2 (26/04)."
    )
    stats['backfill'] = 'applied'
    stats['rows_updated'] = rowcount

    try:
        mark_applied(
            conn,
            'schema_v643',
            notes=(
                f'backfill broken+5xx→unreachable post-v6.4.6 fix, '
                f'{rowcount} rows updated'
            ),
        )
        stats['mark_applied'] = 'ok'
    except Exception as e:
        log.exception("mark_applied schema_v643 failed: %s", e)
        stats['mark_applied'] = f'error: {e}'

    return stats
