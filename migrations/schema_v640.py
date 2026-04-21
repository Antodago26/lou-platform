"""
Migrations de schéma v6.4.0 — tables QA (refonte endpoint /api/stats/listings-qa).

Contexte : l'endpoint historique fait du scrape live synchrone (60-300s), ce qui
timeout systématiquement Neuchâtel + Boudry côté edge Render (502). On bascule
vers une architecture snapshot : un cron Render à 04:00 UTC écrit les recalls
du jour, et l'endpoint lit ensuite la DB en < 100 ms sans jamais scraper.

Ce module crée le socle DB. Il n'introduit PAS de logique de run — les
workers (recall / link_check / field_validation) arriveront dans les commits
suivants. Aucun autre fichier n'est touché par ce commit : le hook de boot
app.py sera ajouté au commit 2 quand il y aura effectivement quelque chose
à orchestrer.

4 tables + 1 ENUM :

1. `qa_recall_snapshots` — 1 row par (city × captured_at). Le cron écrit ici
   source_total_listings vs our_total_listings, recall_pct, missing IDs et
   un raw_snapshot JSONB qui porte le breakdown par portail × transaction
   (on ne fait pas exploser les rows sur ces axes — la granularité ville
   suffit à l'endpoint, le JSONB sert au debug manuel).

2. `qa_link_checks` — 1 row par check d'URL. Le worker link_check échantillonne
   10 listings/portail/ville/jour et vérifie http_status via ScrapingBee.

3. `qa_field_validations` — 1 row par (listing_id × field_name × validated_at).
   Le worker applique les règles de qa_rules.py à toutes les propriétés actives.

4. `qa_runs` — registre d'audit. 1 row par invocation du cron (tous types
   confondus), avec status + listings_processed + errors_count + metadata.

Idempotence : `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` +
bloc `DO` anonyme pour l'ENUM (Postgres 16 n'a pas `CREATE TYPE IF NOT EXISTS`).
Safe à relancer à chaque boot sans effet de bord.

Registre : une entrée `schema_v640` dans `migrations_applied` après succès
complet (pattern `schema_v632.py`, réutilise `ensure_migrations_table` et
`mark_applied` directement).
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


# --- ENUM ------------------------------------------------------------------

def ensure_qa_run_type_enum(conn) -> None:
    """Crée le type `qa_run_type` s'il n'existe pas.

    Postgres 16 n'a pas `CREATE TYPE IF NOT EXISTS` : on passe par un bloc
    DO anonyme qui swallow l'erreur `duplicate_object`. Alternative plus
    verbose (SELECT FROM pg_type) pas nécessaire ici, le pattern DO est
    standard et testé.
    """
    cur = conn.cursor()
    cur.execute("""
        DO $$ BEGIN
            CREATE TYPE qa_run_type AS ENUM (
                'recall', 'link_check', 'field_validation'
            );
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END $$
    """)
    conn.commit()
    cur.close()


# --- Tables ----------------------------------------------------------------

def ensure_qa_recall_snapshots(conn) -> None:
    """Snapshots nocturnes du recall par ville.

    Shape volontairement "plate" : 1 row par (city, captured_at) avec le
    breakdown portail × transaction dans `raw_snapshot` JSONB. L'endpoint
    lit `DISTINCT ON (city) ORDER BY captured_at DESC` pour servir le
    snapshot du jour.

    `recall_pct` peut être NULL (source_total_listings=0 → division
    impossible, pas un bug).

    `missing_listing_ids` est un JSONB (array d'IDs portails) pour
    permettre des stats type `jsonb_array_length(missing_listing_ids)`
    sans parser la string.
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_recall_snapshots (
            id                    BIGSERIAL   PRIMARY KEY,
            city                  TEXT        NOT NULL,
            captured_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_total_listings INTEGER     NOT NULL,
            our_total_listings    INTEGER     NOT NULL,
            recall_pct            NUMERIC(5,2),
            missing_listing_ids   JSONB       NOT NULL DEFAULT '[]'::jsonb,
            raw_snapshot          JSONB       NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_recall_city_captured
        ON qa_recall_snapshots (city, captured_at DESC)
    """)
    conn.commit()
    cur.close()


def ensure_qa_link_checks(conn) -> None:
    """Historique des checks d'URL par listing + portail.

    `listing_id` FK sur `properties(id)` (SERIAL → INTEGER) avec CASCADE :
    si une propriété est purgée (rare — on désactive plutôt via is_active),
    les checks orphelins partent avec. Évite l'accumulation silencieuse.

    Index uniquement sur `checked_at DESC` (le plus fréquent : "quels liens
    j'ai checkés récemment"). Pas d'index sur `listing_id` — si besoin de
    requête par listing, on l'ajoutera quand le volume le justifiera.
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_link_checks (
            id          BIGSERIAL   PRIMARY KEY,
            listing_id  INTEGER     NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            portal      TEXT        NOT NULL,
            url         TEXT        NOT NULL,
            http_status INTEGER,
            checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ok          BOOLEAN     NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_link_checked_at
        ON qa_link_checks (checked_at DESC)
    """)
    conn.commit()
    cur.close()


def ensure_qa_field_validations(conn) -> None:
    """Résultat des validations de champs par listing.

    1 row par (listing × field × validated_at) — on garde l'historique
    pour pouvoir tracer une correction manuelle (si un champ passe de
    ok=false à ok=true au run suivant).

    Index (listing_id, validated_at DESC) = question principale : "quelles
    violations sur cette annonce récemment ?". Les requêtes par field_name
    (ex: "liste tous les listings avec address cassée") feront un scan
    filtré — acceptable tant que le volume reste raisonnable. À réindexer
    si ça devient lent (probablement > 100k rows).
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_field_validations (
            id            BIGSERIAL   PRIMARY KEY,
            listing_id    INTEGER     NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            field_name    TEXT        NOT NULL,
            expected_rule TEXT        NOT NULL,
            actual_value  TEXT,
            ok            BOOLEAN     NOT NULL,
            validated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_field_listing_validated
        ON qa_field_validations (listing_id, validated_at DESC)
    """)
    conn.commit()
    cur.close()


def ensure_qa_runs(conn) -> None:
    """Registre d'audit des runs QA.

    `run_type` = ENUM strict (typos → erreur Postgres côté INSERT).
    `status` = TEXT libre pour tolérer l'ajout de statuts ('pending',
    'running', 'success', 'failed', 'partial', 'timeout', ...) sans
    nouvelle migration.

    `metadata` JSONB porte le contexte spécifique au run_type (ex: pour
    'recall' → `{"cities": [...], "total_scrapes": N}`).
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_runs (
            id                  BIGSERIAL   PRIMARY KEY,
            run_type            qa_run_type NOT NULL,
            started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at        TIMESTAMPTZ,
            status              TEXT        NOT NULL,
            listings_processed  INTEGER     NOT NULL DEFAULT 0,
            errors_count        INTEGER     NOT NULL DEFAULT 0,
            metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_runs_type_started
        ON qa_runs (run_type, started_at DESC)
    """)
    conn.commit()
    cur.close()


# --- Entry point -----------------------------------------------------------

def run_schema_v640(conn) -> dict:
    """Point d'entrée v6.4.0 — à appeler au boot depuis `app.py` après
    `run_schema_v632`. Retourne un dict de stats (ok / error par étape)
    pour que l'appelant puisse logger finement.

    Ordre important : ENUM avant `qa_runs` (qui référence le type). Si
    l'ENUM échoue, `qa_runs` échouera aussi — tant pis, prochain boot
    retry les deux (idempotent).

    Marquage dans `migrations_applied` uniquement si TOUT a réussi. Sinon
    le registre reste vide et le prochain boot rejoue — les CREATE IF NOT
    EXISTS rendent le replay gratuit.
    """
    stats = {}

    # Registry (no-op si déjà créée par schema_v632, mais on ne fait
    # jamais confiance à l'ordre d'exécution au boot).
    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    steps = [
        ('qa_run_type_enum',     ensure_qa_run_type_enum),
        ('qa_recall_snapshots',  ensure_qa_recall_snapshots),
        ('qa_link_checks',       ensure_qa_link_checks),
        ('qa_field_validations', ensure_qa_field_validations),
        ('qa_runs',              ensure_qa_runs),
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
                'schema_v640',
                notes='QA tables: qa_recall_snapshots + qa_link_checks + '
                      'qa_field_validations + qa_runs (+ ENUM qa_run_type)',
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v640 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (some step failed)'

    return stats
