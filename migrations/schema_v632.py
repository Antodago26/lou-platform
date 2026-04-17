"""
Migrations de schéma v6.3.2 — idempotentes, safe à relancer à chaque boot.

1. Table `migrations_applied` — registre générique des migrations one-shot.
   Remplace les "flags one-off" éparpillés. Toute migration enregistre son
   nom + timestamp pour éviter la ré-exécution.

2. Colonne `properties.gps_source` — traçabilité de l'origine des coords.
   Values: 'scraped', 'npa_fallback', 'city_fallback', 'geo_admin', NULL (inconnu).
   Résout le blocage structurel v6.3.2 : auparavant, on ne pouvait pas
   distinguer un bien avec GPS scrapé réel d'un bien dont les lat/lng
   venaient de NPA_COORDS fallback — heuristique "coords rondes" fragile.
"""
import logging

log = logging.getLogger('lou-app')


def ensure_migrations_table(conn) -> None:
    """Crée la table migrations_applied si absente."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS migrations_applied (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT NOW(),
            notes TEXT
        )
    """)
    conn.commit()
    cur.close()


def is_applied(conn, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM migrations_applied WHERE name = %s", (name,))
    row = cur.fetchone()
    cur.close()
    return row is not None


def mark_applied(conn, name: str, notes: str = None) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO migrations_applied (name, notes)
        VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING
        """,
        (name, notes),
    )
    conn.commit()
    cur.close()


def ensure_gps_source_column(conn) -> None:
    """Ajoute properties.gps_source si absente."""
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE properties
        ADD COLUMN IF NOT EXISTS gps_source TEXT
    """)
    conn.commit()
    cur.close()


def run_schema_v632(conn) -> dict:
    """Point d'entrée — à appeler au boot, avant les autres migrations v6.3.2."""
    stats = {}
    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    try:
        ensure_gps_source_column(conn)
        stats['gps_source_column'] = 'ok'
    except Exception as e:
        log.exception("gps_source ADD COLUMN failed: %s", e)
        stats['gps_source_column'] = f'error: {e}'

    return stats
