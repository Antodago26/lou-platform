"""Bon Home : pipeline anibis.ch (canton de Neuchatel par defaut).

    python cron_anibis.py                # NE, location + achat
    ANIBIS_CANTON=VD python cron_anibis.py
    ANIBIS_MAX_PAGES=3 ANIBIS_MAX_DETAILS=20 python cron_anibis.py   # test rapide

Etapes : lecture des pages de recherche, fiche pour les nouveautes,
enregistrement (save_to_db fait le dedoublonnage), desactivation des
annonces anibis disparues (seulement si toutes les pages ont ete lues),
puis rescoring de tous les profils actifs. Pas de ScrapingBee.
"""
import os
import sys
import logging
from datetime import datetime

import psycopg2
import psycopg2.extras

from scrapers import save_to_db
from scraper_anibis import scrape_anibis, SOURCE
from scoring_engine import score_all_for_profile

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('lou-app')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
CANTON = os.environ.get('ANIBIS_CANTON', 'NE')
MAX_PAGES = int(os.environ.get('ANIBIS_MAX_PAGES', '40'))
MAX_DETAILS = int(os.environ.get('ANIBIS_MAX_DETAILS', '200'))
TRANSACTIONS = [t for t in os.environ.get('ANIBIS_TRANSACTIONS', 'location,achat').split(',') if t]


def _known_ids(db):
    cur = db.cursor()
    cur.execute("SELECT external_id FROM properties WHERE source = %s", (SOURCE,))
    ids = {r['external_id'] for r in cur.fetchall()}
    cur.close()
    return ids


def _deactivate_missing(db, seen_ids):
    """Desactive les annonces anibis du canton qui ne sont plus en ligne."""
    if not seen_ids:
        return 0
    cur = db.cursor()
    cur.execute("""
        UPDATE properties SET is_active = FALSE
        WHERE source = %s AND is_active = TRUE AND canton = %s
          AND NOT (external_id = ANY(%s))
    """, (SOURCE, CANTON, list(seen_ids)))
    n = cur.rowcount
    db.commit()
    cur.close()
    return n


def run_anibis(db, canton=CANTON, transactions=TRANSACTIONS, max_pages=MAX_PAGES,
               max_details=MAX_DETAILS, rescore=True):
    """Renvoie un dict de stats. Reutilisable depuis cron_job.py."""
    stats = {'canton': canton, 'read': 0, 'saved': 0, 'deactivated': 0, 'complete': True}
    known = _known_ids(db)
    log.info(f"[Anibis] {len(known)} annonces deja connues")
    seen = set()
    for tx in transactions:
        try:
            props, meta = scrape_anibis(canton=canton, transaction=tx, max_pages=max_pages,
                                        known_ids=known, max_details=max_details,
                                        return_meta=True)
        except Exception as e:
            log.error(f"[Anibis] {tx} en erreur: {e}", exc_info=True)
            stats['complete'] = False
            continue
        stats['read'] += len(props)
        seen.update(p['external_id'] for p in props)
        # Une lecture partielle (pages manquantes) ne doit pas desactiver le reste.
        if not meta.get('complete'):
            stats['complete'] = False
        log.info(f"[Anibis] {tx}: {meta}")
        if props:
            try:
                stats['saved'] += save_to_db(db, props)
            except Exception as e:
                log.error(f"[Anibis] enregistrement {tx} en erreur: {e}", exc_info=True)
                db.rollback()
                stats['complete'] = False
    if stats['complete'] and seen:
        stats['deactivated'] = _deactivate_missing(db, seen)
    else:
        log.warning("[Anibis] lecture incomplete, pas de desactivation cette fois")
    if rescore:
        cur = db.cursor()
        cur.execute("SELECT id, user_id FROM search_profiles WHERE is_active = TRUE")
        profiles = cur.fetchall()
        cur.close()
        for p in profiles:
            try:
                n = score_all_for_profile(db, p['id'])
                log.info(f"[Anibis] profil {p['id']}: {n} annonces scorees")
            except Exception as e:
                log.error(f"[Anibis] scoring profil {p['id']} en erreur: {e}")
                db.rollback()
    log.info(f"[Anibis] termine: {stats}")
    return stats


def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL manquant")
        sys.exit(1)
    db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        run_anibis(db)
    finally:
        db.close()


if __name__ == '__main__':
    main()
