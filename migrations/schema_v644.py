"""
Migrations de schéma v6.4.4 — table `qa_source_health` (repurpose Phase 1
du cron lou-qa-recall après le drop Homegate + ImmoScout24, décision CEO
30/04).

Contexte : la Phase 1 historique scrappait Homegate live pour 8 villes NE
afin de calculer un recall (live vs DB). Sans Homegate, ce calcul n'a
plus de sens. Nouveau rôle Phase 1 : audit santé des scrapers primaires
(per-source, pas per-city).

Pourquoi une nouvelle table plutôt qu'adapter `qa_recall_snapshots` :
  - Granularité différente : recall = (city × captured_at), health =
    (source × captured_at). Réutiliser la table imposerait soit de
    tordre `city` en source name (sémantique cassée, casse les index
    et les futures requêtes), soit d'ajouter des colonnes nullable
    (source TEXT NULL, total_active INT NULL, ...) → schéma hybride
    illisible, requêtes pleines de COALESCE.
  - Champs distincts : pas de notion de `recall_pct` / `missing_ids`
    ici, mais on a `total_active` / `scraped_7d` / `scraped_30d` /
    `last_scrape` / `status` qui n'existent pas côté recall.
  - Coût migration : ~30 lignes (ce fichier), zéro impact sur les
    snapshots recall historiques (qui restent lisibles pour audit
    rétro). `qa_recall_snapshots` n'est plus écrite après ce déploiement
    mais on la garde — pas de DROP TABLE, on ne purge jamais d'historique
    QA sans décision explicite produit.

Schéma :
  - source         : TEXT, nom du portail tel que stocké dans
                     properties.source ('Flatfox', 'Immobilier.ch', ...).
  - captured_at    : TIMESTAMPTZ, instant du run.
  - total_active   : nombre de properties is_active=TRUE pour cette source.
  - scraped_7d     : sous-ensemble scraped_at >= NOW() - 7 days.
  - scraped_30d    : sous-ensemble scraped_at >= NOW() - 30 days.
  - last_scrape    : MAX(scraped_at) pour cette source (NULL si jamais
                     scrappée — théoriquement impossible dans
                     properties mais on tolère).
  - status         : 'ok' | 'warn' | 'fail' (cf. qa_source_health_worker
                     pour les seuils exacts ; voir aussi le commit
                     feat(qa): drop homegate+immoscout pour la décision
                     produit).

Idempotent (CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS).
Marqué dans `migrations_applied` une fois la création OK.
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def ensure_qa_source_health(conn) -> None:
    """Crée la table qa_source_health si absente."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_source_health (
            id            BIGSERIAL   PRIMARY KEY,
            source        TEXT        NOT NULL,
            captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            total_active  INTEGER     NOT NULL,
            scraped_7d    INTEGER     NOT NULL,
            scraped_30d   INTEGER     NOT NULL,
            last_scrape   TIMESTAMPTZ,
            status        TEXT        NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_source_health_source_captured
        ON qa_source_health (source, captured_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_qa_source_health_captured
        ON qa_source_health (captured_at DESC)
    """)
    conn.commit()
    cur.close()


def run_schema_v644(conn) -> dict:
    """Point d'entrée v6.4.4. Appelé depuis app.py APRÈS run_schema_v643.

    Retourne un dict de stats par étape. Marque la migration appliquée
    uniquement si ensure_qa_source_health a réussi.
    """
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    try:
        ensure_qa_source_health(conn)
        stats['qa_source_health'] = 'ok'
    except Exception as e:
        log.exception("qa_source_health create failed: %s", e)
        stats['qa_source_health'] = f'error: {e}'
        return stats

    try:
        mark_applied(
            conn,
            'schema_v644',
            notes=(
                'qa_source_health table — repurpose Phase 1 cron lou-qa-recall '
                'after Homegate+ImmoScout24 drop (30/04 CEO decision)'
            ),
        )
        stats['mark_applied'] = 'ok'
    except Exception as e:
        log.exception("mark_applied schema_v644 failed: %s", e)
        stats['mark_applied'] = f'error: {e}'

    return stats
