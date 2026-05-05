"""
Migrations de schéma v6.4.5 — indexes manquants identifiés par l'audit
complet 2026-05-04.

Quatre indexes ajoutés, tous via CREATE INDEX IF NOT EXISTS (idempotents) :

1. idx_prop_lower_city_dedup
   Cible : scrapers.save_to_db._find_cross_portal_duplicate (scrapers.py:~3038)
   qui utilise WHERE LOWER(city) = LOWER(%s) AND source != %s AND price BETWEEN
   ... AND rooms = ... — la fonction LOWER() défait l'index existant
   idx_prop_city. Nouvel index fonctionnel sur (LOWER(city), source, price,
   rooms) pour que la dedup cross-portail soit O(log N) plutôt que O(N).
   Audit M5/M11.

2. idx_prop_link_health_select  (PARTIEL)
   Cible : qa_link_health_worker._select_urls_to_check, qui fait
   SELECT ... WHERE is_active=TRUE AND source_url IS NOT NULL ORDER BY
   last_checked_at NULLS FIRST LIMIT 1000. Index partiel sur
   last_checked_at + filtre is_active+source_url réduit la table à scanner
   uniquement aux candidats éligibles (typiquement < 30% du stock).

3. idx_prop_first_seen_active  (PARTIEL)
   Cible : routes_properties.get_properties qui filtre les nouveautés via
   first_seen_at > NOW() - INTERVAL '24 hours'. Index partiel sur
   (first_seen_at) WHERE is_active=TRUE — le scan ne touche que les
   annonces actives, ~70% du stock évité.

4. idx_scored_user_zone_score
   Cible : dashboard hot path — SELECT scored_properties WHERE user_id=X
   AND score_zone >= 80 ORDER BY total_score DESC. Index composite
   (user_id, score_zone DESC, total_score DESC) pour servir la requête
   sans tri additionnel.

Note : pas de CREATE INDEX CONCURRENTLY car le pattern de migrations existant
utilise une transaction. Sur les volumes actuels (~8000 properties), le lock
de quelques secondes au boot est acceptable. Bascule en CONCURRENTLY si la
table grossit > 100k.

Idempotent (CREATE INDEX IF NOT EXISTS). Marqué dans `migrations_applied`
une fois la création OK.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def ensure_audit_indexes(conn) -> dict:
    """Crée les 4 indexes audit-2026-05 si absents. Retourne stats par index."""
    cur = conn.cursor()
    stats = {}

    indexes = [
        (
            'idx_prop_lower_city_dedup',
            """
            CREATE INDEX IF NOT EXISTS idx_prop_lower_city_dedup
            ON properties (LOWER(city), source, price, rooms)
            """,
        ),
        (
            'idx_prop_link_health_select',
            """
            CREATE INDEX IF NOT EXISTS idx_prop_link_health_select
            ON properties (last_checked_at NULLS FIRST)
            WHERE is_active = TRUE AND source_url IS NOT NULL
            """,
        ),
        (
            'idx_prop_first_seen_active',
            """
            CREATE INDEX IF NOT EXISTS idx_prop_first_seen_active
            ON properties (first_seen_at DESC)
            WHERE is_active = TRUE
            """,
        ),
        (
            'idx_scored_user_zone_score',
            """
            CREATE INDEX IF NOT EXISTS idx_scored_user_zone_score
            ON scored_properties (user_id, score_zone DESC, total_score DESC)
            """,
        ),
    ]

    for name, sql in indexes:
        try:
            cur.execute(sql)
            stats[name] = 'ok'
        except Exception as e:
            log.exception("Index %s creation failed: %s", name, e)
            stats[name] = f'error: {e}'

    conn.commit()
    cur.close()
    return stats


def run_schema_v645(conn) -> dict:
    """Point d'entrée v6.4.5. Appelé depuis app.py APRÈS run_schema_v644."""
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    try:
        index_stats = ensure_audit_indexes(conn)
        stats.update(index_stats)
    except Exception as e:
        log.exception("ensure_audit_indexes failed: %s", e)
        stats['ensure_audit_indexes'] = f'error: {e}'
        return stats

    try:
        mark_applied(
            conn,
            'schema_v645',
            notes=(
                'audit-2026-05 indexes: cross-portal dedup, link-health select, '
                'first-seen-active, scored-user-zone-score'
            ),
        )
        stats['mark_applied'] = 'ok'
    except Exception as e:
        log.exception("mark_applied schema_v645 failed: %s", e)
        stats['mark_applied'] = f'error: {e}'

    return stats
