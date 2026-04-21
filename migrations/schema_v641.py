"""
Migrations de schéma v6.4.1 — widen qa_recall_snapshots.recall_pct.

Contexte : le 1er run cron `lou-qa-recall` (commit a2b6a37) a crashé sur
NumericValueOutOfRange lors de l'INSERT d'un snapshot où `total_our >>
total_source`. Le top-level recall_pct est calculé comme `total_our /
total_source * 100` et peut dépasser 999.99 (limite NUMERIC(5,2)) dans
ces conditions pathologiques.

Exemple observé : source_total=1, our_total=380 → recall_pct = 38000%.
Ce n'est pas un bug à masquer — c'est un signal diagnostique clair que
le scraping live est incomplet (ou que la DB est stale). On garde la
valeur réelle et on élargit la colonne.

NUMERIC(5,2) → NUMERIC(7,2) :
  - max 999.99 → max 99999.99
  - couvre les cas pathologiques observés
  - le worker clamp Python à 99999.99 en double filet (cf. qa_recall_worker.py)

Idempotence : on lit `information_schema.columns` avant l'ALTER — si la
précision est déjà (7,2), noop. ALTER TABLE ... TYPE NUMERIC→NUMERIC est
non-destructif sur Postgres (cast implicite, pas de rewrite des rows).
"""
import logging

from migrations.schema_v632 import ensure_migrations_table, mark_applied

log = logging.getLogger('lou-app')


def _current_recall_pct_precision(conn):
    """Retourne (numeric_precision, numeric_scale) de la colonne
    qa_recall_snapshots.recall_pct, ou None si la table/colonne n'existe
    pas (v640 pas encore appliquée, par exemple)."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name   = 'qa_recall_snapshots'
              AND column_name  = 'recall_pct'
        """)
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    # Tolère dict (RealDictCursor) ET tuple
    if isinstance(row, dict):
        vals = list(row.values())
        p, s = vals[0], vals[1]
    else:
        p, s = row[0], row[1]
    return (int(p) if p is not None else None, int(s) if s is not None else None)


def widen_recall_pct(conn) -> str:
    """ALTER TABLE ... TYPE NUMERIC(7,2) si pas déjà la bonne précision.

    Retourne un code texte pour logging :
      - 'applied'       : l'ALTER a été exécuté
      - 'noop'          : déjà en NUMERIC(7,2) ou plus large
      - 'missing_table' : qa_recall_snapshots absente (v640 pas appliquée)
    """
    prec = _current_recall_pct_precision(conn)
    if prec is None:
        log.warning(
            "v6.4.1: qa_recall_snapshots.recall_pct introuvable — v640 pas "
            "encore appliquée ? La migration sera no-op, v640 retentera au "
            "prochain boot puis v641 s'appliquera."
        )
        return 'missing_table'

    precision, scale = prec
    # Accepter aussi toute précision > 7 (si quelqu'un a déjà élargi plus
    # loin) pour que la migration soit strictement "au moins 7.2".
    if precision is not None and precision >= 7 and scale == 2:
        return 'noop'

    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE qa_recall_snapshots
            ALTER COLUMN recall_pct TYPE NUMERIC(7,2)
        """)
    finally:
        cur.close()
    conn.commit()
    return 'applied'


def run_schema_v641(conn) -> dict:
    """Point d'entrée v6.4.1 — à appeler au boot depuis `app.py` APRÈS
    `run_schema_v640` (logique : v641 modifie une table créée par v640).

    Marquage `schema_v641` dans `migrations_applied` si widen_recall_pct
    a retourné 'applied' ou 'noop'. Si 'missing_table' ou erreur, on ne
    marque pas — le prochain boot retentera.
    """
    stats = {}

    try:
        ensure_migrations_table(conn)
        stats['migrations_applied_table'] = 'ok'
    except Exception as e:
        log.exception("migrations_applied create failed: %s", e)
        stats['migrations_applied_table'] = f'error: {e}'

    try:
        res = widen_recall_pct(conn)
        stats['widen_recall_pct'] = res
        if res == 'applied':
            log.info("v6.4.1: qa_recall_snapshots.recall_pct NUMERIC(5,2) → NUMERIC(7,2)")
    except Exception as e:
        log.exception("widen_recall_pct failed: %s", e)
        stats['widen_recall_pct'] = f'error: {e}'

    if stats.get('widen_recall_pct') in ('applied', 'noop'):
        try:
            mark_applied(
                conn,
                'schema_v641',
                notes='widen qa_recall_snapshots.recall_pct to NUMERIC(7,2)',
            )
            stats['mark_applied'] = 'ok'
        except Exception as e:
            log.exception("mark_applied schema_v641 failed: %s", e)
            stats['mark_applied'] = f'error: {e}'
    else:
        stats['mark_applied'] = 'skipped (widen did not succeed)'

    return stats
