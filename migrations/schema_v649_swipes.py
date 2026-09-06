"""
Migration de schema v6.4.9 : table `swipes` pour le feed mobile.

Le feed montre un bien par ecran. Chaque geste de l'utilisateur est enregistre
ici : like (garder, cree aussi un favori), pass (pas pour moi), skip (a fait
defiler sans decider). Le feed exclut les biens likes ou passes pour toujours,
et les biens skippes pendant 7 jours.

Idempotent : CREATE TABLE IF NOT EXISTS + index IF NOT EXISTS.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def create_swipes_table(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS swipes (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                action      VARCHAR(8) NOT NULL CHECK (action IN ('like', 'pass', 'skip')),
                created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, property_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_swipes_user_created
            ON swipes (user_id, created_at DESC)
        """)
    finally:
        cur.close()
    conn.commit()


def run_schema_v649(conn) -> dict:
    """Point d'entree v6.4.9, appele depuis app.py apres run_schema_v648."""
    stats = {}
    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    try:
        create_swipes_table(conn)
        stats['swipes_table'] = 'ok'
    except Exception as e:
        log.exception("swipes table failed: %s", e)
        stats['swipes_table'] = f'error: {e}'
        stats['mark_applied'] = 'skipped (some step failed)'
        return stats

    try:
        mark_applied(conn, 'schema_v649', notes='feed mobile: table swipes (like/pass/skip)')
        stats['mark_applied'] = 'ok'
    except Exception as e:
        log.exception("mark_applied schema_v649 failed: %s", e)
        stats['mark_applied'] = f'error: {e}'
    return stats
