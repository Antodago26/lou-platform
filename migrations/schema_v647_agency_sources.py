"""
Migration de schema v6.4.7 — table `agency_sources` (pivot « scraping direct
des sites d'agences »).

Contexte produit
----------------
On quitte le scraping des PORTAILS (Homegate/ImmoScout deja coupes, cf. flags
ENABLE_*) pour scraper DIRECTEMENT les sites d'agences via le modele « 1 parser
par backend » (voir scraper_immomig.py). Jusqu'ici les 3 agences NE (Jouval,
Muller&Christe, Fidimmobil) etaient codees en dur dans scrapers.py. Ca ne passe
pas a l'echelle : on veut une TABLE qui liste les agences a scraper, leur
backend, leur etat de sante et leur cadence.

Table
-----
`agency_sources` :
  - domain            : cle logique + valeur `source` des biens (ex 'bulliard.ch')
  - name              : libelle lisible (derive du domaine par defaut)
  - backend           : 'Immomig' | 'Apimo' | 'CASASOFT' | 'Estatik' | ...
  - immomig_client_id : rempli au 1er scrape reussi (diagnostic)
  - canton            : canton principal si connu (sinon derive par bien)
  - status            : 'active' | 'paused' | 'error'  (pilote scrape_all)
  - last_scraped_at / last_count / consecutive_failures : monitoring sante
  - notes / created_at

Seed
----
38 domaines Immomig CONFIRMES (detection reelle, juin 2026 — cf.
prototypes/immomig/DECOUVERTE.md). Inseres `status='active'`. Idempotent :
INSERT ... ON CONFLICT (domain) DO NOTHING, donc relancable a chaque boot sans
ecraser un statut modifie a la main.

Pattern strictement calque sur schema_v646 : CREATE TABLE / INDEX IF NOT EXISTS,
marquage `schema_v647` seulement si toutes les etapes passent.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')

# 38 agences Immomig confirmees (backend_repetable=OUI dans agences_priorisees.csv).
# Le canton n'est pas toujours connu de facon fiable -> laisse a NULL, le parser
# derive le canton bien par bien depuis la ville. On seed juste domaine + backend.
_SEED_IMMOMIG = [
    "sallin-immobilier.ch", "digirent.swiss", "gs-immobilier.ch", "gva-immo.ch",
    "ivac.ch", "rfsa.ch", "swissmls.ch", "avanthaypartners.com", "avenaris.com",
    "bulliard.ch", "ci-leman.ch", "cnc-immobilier.ch", "derham.ch", "dreamo.ch",
    "dv-immo.ch", "engadin-rem.ch", "fontanasothebysrealty.com",
    "gerances-giroud.ch", "global-immo.ch", "home-visit.ch", "ic-groupe.ch",
    "immobiliere-de-lausanne.ch", "immocrans.ch", "muller-immobilier.ch",
    "primeproperty.ch", "regis-sa.ch", "sovalco.ch", "stalder-immobilier.ch",
    "switzerland-sothebysrealty.ch", "trendwerk.ch", "truvag.ch",
    "valimmobilier.ch", "verbel.ch", "vermoegenszentrum.ch", "vesa.ch",
    "vpi-sa.ch", "zurichsothebysrealty.com", "mulleretchriste.ch",
]


def _name_from_domain(domain):
    """'muller-immobilier.ch' -> 'Muller Immobilier'."""
    stem = domain.rsplit(".", 1)[0]
    return stem.replace("-", " ").replace(".", " ").title()


def ensure_agency_sources_table(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agency_sources (
                id                    SERIAL PRIMARY KEY,
                domain                VARCHAR(120) NOT NULL UNIQUE,
                name                  VARCHAR(160),
                backend               VARCHAR(40) NOT NULL DEFAULT 'Immomig',
                immomig_client_id     VARCHAR(20),
                canton                VARCHAR(4),
                status                VARCHAR(20) NOT NULL DEFAULT 'active',
                last_scraped_at       TIMESTAMP,
                last_count            INTEGER NOT NULL DEFAULT 0,
                consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                notes                 TEXT,
                created_at            TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_agency_sources_active
            ON agency_sources (status)
            WHERE status = 'active'
        """)
    finally:
        cur.close()
    conn.commit()


def seed_immomig_agencies(conn) -> int:
    """Insere les 38 agences Immomig confirmees. Idempotent (ON CONFLICT DO
    NOTHING). Renvoie le nombre de lignes reellement inserees."""
    cur = conn.cursor()
    inserted = 0
    try:
        for domain in _SEED_IMMOMIG:
            cur.execute(
                """
                INSERT INTO agency_sources (domain, name, backend, status, notes)
                VALUES (%s, %s, 'Immomig', 'active', %s)
                ON CONFLICT (domain) DO NOTHING
                """,
                (domain, _name_from_domain(domain),
                 'seed v647 — detection Immomig confirmee (juin 2026)'),
            )
            inserted += cur.rowcount
    finally:
        cur.close()
    conn.commit()
    return inserted


def run_schema_v647(conn) -> dict:
    """Point d'entree v6.4.7 — a appeler depuis app.py APRES run_schema_v646.
    Idempotent."""
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    all_ok = True
    try:
        ensure_agency_sources_table(conn)
        stats['agency_sources_table'] = 'ok'
    except Exception as e:
        log.exception("agency_sources table failed: %s", e)
        stats['agency_sources_table'] = f'error: {e}'
        all_ok = False

    if all_ok:
        try:
            n = seed_immomig_agencies(conn)
            stats['seed_immomig'] = f'ok ({n} inserted)'
        except Exception as e:
            log.exception("seed_immomig failed: %s", e)
            stats['seed_immomig'] = f'error: {e}'
            all_ok = False

    if all_ok:
        try:
            mark_applied(
                conn,
                'schema_v647',
                notes='pivot agences: table agency_sources + seed 38 Immomig',
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v647 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (some step failed)'

    return stats
