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


def ensure_geo_cache_table(conn) -> None:
    """
    Table de cache persistant pour geo.admin.ch + dict local.

    Stratégie :
      - Hits (lat/lng non null) : pas de TTL, coords communes suisses stables.
      - Miss (latitude=0 & longitude=0 & source='miss') : TTL 7j, re-tenter
        au cas où une nouvelle commune/alias apparaît côté geo.admin.
      - query normalisé (lowercase, accents strippés, trim) — PRIMARY KEY
        pour idempotence des write-through.
      - source : 'geo.admin.ch' | 'local_dict' | 'miss'.
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS geo_cache (
            query TEXT PRIMARY KEY,
            postal_code TEXT,
            name TEXT NOT NULL,
            latitude FLOAT NOT NULL,
            longitude FLOAT NOT NULL,
            source TEXT NOT NULL,
            hit_count INT DEFAULT 1,
            created_at TIMESTAMP DEFAULT NOW(),
            last_hit_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_geo_cache_postal
        ON geo_cache(postal_code)
    """)
    conn.commit()
    cur.close()


def ensure_unresolved_locations_table(conn) -> None:
    """
    Table d'audit pour les résolutions géographiques échouées (step 5).

    Utilisée par le chat UX : quand une zone entrée par l'user n'est ni dans
    CITY_COORDS/NPA_COORDS, ni dans geo_cache, ni résolue par geo.admin.ch,
    on log ici la query + les suggestions proposées + le choix final de l'user
    (ou NULL si abandon). Audit hebdo pour enrichir CITY_COORDS et prioriser
    les ajustements.

    user_id est nullable + anon_session_id TEXT pour supporter le chat anonyme
    (pré-signup flow). Index sur created_at pour scan chronologique.
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS unresolved_locations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            anon_session_id TEXT,
            query TEXT NOT NULL,
            suggestions_json JSONB,
            chosen TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_unresolved_locations_created_at
        ON unresolved_locations(created_at DESC)
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

    try:
        ensure_geo_cache_table(conn)
        stats['geo_cache_table'] = 'ok'
    except Exception as e:
        log.exception("geo_cache create failed: %s", e)
        stats['geo_cache_table'] = f'error: {e}'

    try:
        ensure_unresolved_locations_table(conn)
        stats['unresolved_locations_table'] = 'ok'
    except Exception as e:
        log.exception("unresolved_locations create failed: %s", e)
        stats['unresolved_locations_table'] = f'error: {e}'

    return stats
