"""
Migrations de schéma v6.4.6 — support de la PUBLICATION par des particuliers.

Contexte produit : bonhome s'ouvre en marketplace. Jusqu'ici la table
`properties` n'était alimentée que par les scrapers (source = portail/agence).
On ajoute la possibilité qu'un utilisateur connecté publie SON bien : même
table, `source = 'prive'`, mais avec deux colonnes de plus :

1. `properties.owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE`
   - NULL pour toutes les annonces scrapées (aucun propriétaire interne).
   - Renseigné uniquement pour les annonces `source='prive'` : le vendeur.
   - ON DELETE CASCADE : si le compte est supprimé, ses annonces partent avec.
   - Index partiel WHERE owner_user_id IS NOT NULL → "mes annonces" rapide,
     sans indexer les ~8000 lignes scrapées qui ont owner_user_id NULL.

2. `properties.listing_status TEXT DEFAULT 'active'`
   - Cycle de vie d'une annonce de privé : 'draft' → 'active' → 'sold'/'rented'
     → 'archived'. Permet la modération ('pending') et le retrait sans DELETE.
   - DEFAULT 'active' : les annonces scrapées existantes restent 'active',
     comportement inchangé côté front (qui filtre déjà sur is_active).

Pattern strictement identique à schema_v642 : ADD COLUMN IF NOT EXISTS,
idempotent, safe à relancer à chaque boot. Marquage `schema_v646` dans
`migrations_applied` seulement si toutes les étapes passent.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def ensure_properties_owner_columns(conn) -> None:
    """Ajoute `owner_user_id` + `listing_status` à `properties` (+ index partiel)."""
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE properties
            ADD COLUMN IF NOT EXISTS owner_user_id INTEGER
                REFERENCES users(id) ON DELETE CASCADE
        """)
        cur.execute("""
            ALTER TABLE properties
            ADD COLUMN IF NOT EXISTS listing_status TEXT DEFAULT 'active'
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_properties_owner
            ON properties (owner_user_id)
            WHERE owner_user_id IS NOT NULL
        """)
    finally:
        cur.close()
    conn.commit()


def run_schema_v646(conn) -> dict:
    """Point d'entrée v6.4.6 — à appeler depuis `app.py` APRÈS run_schema_v645.
    Idempotent. N'étend que `properties` (créée par le bootstrap initial)."""
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    steps = [
        ('properties_owner_columns', ensure_properties_owner_columns),
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
                'schema_v646',
                notes='marketplace: properties.owner_user_id + listing_status (publication privé)',
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v646 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (some step failed)'

    return stats
