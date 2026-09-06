"""
Migration de schema v6.4.8 — seed des agences sur template hebergé Apimo
(2e backend du pivot « scraping direct des sites d'agences »).

Contexte
--------
schema_v647 a cree la table `agency_sources` + seed 38 agences Immomig. Apimo
est le 2e backend couvert (parser generique : scraper_apimo.py, template hebergé
« Design by Apimo »). Apimo est minoritaire en Suisse romande, mais les 3
agences confirmees (juin 2026) partagent le meme gabarit -> 1 parser les couvre
toutes, et 2 sont en zone Neuchateloise (terrain bonhome).

Seed
----
3 domaines Apimo CONFIRMES par detection reelle (empreinte apimo.net +
extraction validee sur .price/.rooms/.area/.city/.subtype) :
  - reference5.ch                  (NE / Montreux)
  - lerezo.ch                      (VS)
  - agence-immobiliere-immoglobe.ch (NE, + biens FR)

Idempotent : INSERT ... ON CONFLICT (domain) DO NOTHING. Depend de la table
creee par v647 -> a lancer APRES run_schema_v647.

/!\ omnia.ch (Apimo via plugin WordPress sur-mesure) n'est PAS seedé ici : son
gabarit est propre a l'agence, hors périmètre du parser hebergé. A traiter en
scraper bespoke si besoin.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')

_SEED_APIMO = [
    "reference5.ch",
    "lerezo.ch",
    "agence-immobiliere-immoglobe.ch",
]


def _name_from_domain(domain):
    stem = domain.rsplit(".", 1)[0]
    return stem.replace("-", " ").replace(".", " ").title()


def seed_apimo_agencies(conn) -> int:
    """Insere les agences Apimo confirmees. Idempotent. Renvoie le nb insere.
    Suppose la table agency_sources deja creee (v647)."""
    cur = conn.cursor()
    inserted = 0
    try:
        for domain in _SEED_APIMO:
            cur.execute(
                """
                INSERT INTO agency_sources (domain, name, backend, status, notes)
                VALUES (%s, %s, 'Apimo', 'active', %s)
                ON CONFLICT (domain) DO NOTHING
                """,
                (domain, _name_from_domain(domain),
                 'seed v648 — template hebergé Apimo confirme (juin 2026)'),
            )
            inserted += cur.rowcount
    finally:
        cur.close()
    conn.commit()
    return inserted


def run_schema_v648(conn) -> dict:
    """Point d'entree v6.4.8 — a appeler depuis app.py APRES run_schema_v647.
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
        n = seed_apimo_agencies(conn)
        stats['seed_apimo'] = f'ok ({n} inserted)'
    except Exception as e:
        log.exception("seed_apimo failed: %s", e)
        stats['seed_apimo'] = f'error: {e}'
        all_ok = False

    if all_ok:
        try:
            mark_applied(
                conn,
                'schema_v648',
                notes='pivot agences: seed 3 agences Apimo (template hebergé)',
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v648 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (some step failed)'

    return stats
