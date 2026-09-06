#!/usr/bin/env python3
"""Bon Home : anibis depuis le Mac (IP suisse), envoye a bonhome.ch.

anibis.ch refuse les adresses IP des serveurs (403), mais repond depuis une
connexion residentielle suisse. Ce script tourne donc sur le Mac d'Antony,
toutes les deux heures (launchd), et pousse les annonces via /api/import
avec la cle d'ingestion (~/.bonhome/ingest.key, meme valeur que INGEST_KEY
sur Render).

    ~/.bonhome/venv/bin/python local_anibis.py            # canton NE
    BONHOME_CANTON=VD ... local_anibis.py
    BONHOME_MAX_PAGES=2 BONHOME_MAX_DETAILS=10 ... (test rapide)

Memoire locale : ~/.bonhome/anibis_seen.json (ids deja envoyes avec fiche)
Journal        : ~/.bonhome/anibis.log
"""
import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper_anibis import scrape_anibis, SOURCE  # noqa: E402

HOME = Path.home() / '.bonhome'
HOME.mkdir(exist_ok=True)
LOG = HOME / 'anibis.log'
SEEN = HOME / 'anibis_seen.json'
KEY_FILE = HOME / 'ingest.key'

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG, encoding='utf-8'), logging.StreamHandler()],
)
log = logging.getLogger('lou-app')

BACKEND = os.environ.get('BONHOME_URL', 'https://bonhome.ch').rstrip('/')
CANTON = os.environ.get('BONHOME_CANTON', 'NE')
MAX_PAGES = int(os.environ.get('BONHOME_MAX_PAGES', '40'))
MAX_DETAILS = int(os.environ.get('BONHOME_MAX_DETAILS', '250'))
TRANSACTIONS = [t for t in os.environ.get('BONHOME_TRANSACTIONS', 'location,achat').split(',') if t]
BATCH = 100


def _key():
    k = os.environ.get('BONHOME_INGEST_KEY', '').strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
    if not k:
        log.error(f"Cle d'ingestion absente ({KEY_FILE})")
        sys.exit(1)
    return k


def _load_seen():
    try:
        return set(json.loads(SEEN.read_text()))
    except Exception:
        return set()


def _save_seen(ids):
    SEEN.write_text(json.dumps(sorted(ids)))


def _post(payload, key):
    r = requests.post(f"{BACKEND}/api/import", json=payload,
                      headers={'X-Ingest-Key': key, 'Content-Type': 'application/json'}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _serializable(p):
    out = {}
    for k, v in p.items():
        if isinstance(v, datetime):
            v = v.isoformat()
        out[k] = v
    return out


def main():
    key = _key()
    seen = _load_seen()
    log.info(f"anibis {CANTON} -> {BACKEND} ({len(seen)} fiches deja envoyees)")
    all_ids, complete, sent = set(), True, 0
    for tx in TRANSACTIONS:
        try:
            props, meta = scrape_anibis(canton=CANTON, transaction=tx, max_pages=MAX_PAGES,
                                        known_ids=seen, max_details=MAX_DETAILS, return_meta=True)
        except Exception as e:
            log.error(f"{tx}: scrape en erreur: {e}", exc_info=True)
            complete = False
            continue
        log.info(f"{tx}: {meta}")
        if not meta.get('complete'):
            complete = False
        for i in range(0, len(props), BATCH):
            batch = [_serializable(p) for p in props[i:i + BATCH]]
            try:
                res = _post({'listings': batch}, key)
                sent += res.get('saved', 0)
                log.info(f"{tx}: lot {i // BATCH + 1}: {res.get('saved')} enregistrees")
            except Exception as e:
                log.error(f"{tx}: envoi lot {i // BATCH + 1} en erreur: {e}")
                complete = False
                continue
            for p in props[i:i + BATCH]:
                all_ids.add(p['external_id'])
                if p.get('latitude') or (p.get('images') and len(p['images']) > 1):
                    seen.add(p['external_id'])
        time.sleep(1)
    _save_seen(seen)
    final = {'rescore': True}
    if complete and all_ids:
        final['deactivate'] = {'source': SOURCE, 'canton': CANTON, 'seen_ids': sorted(all_ids)}
    try:
        res = _post(final, key)
        log.info(f"final: {res}")
    except Exception as e:
        log.error(f"final en erreur: {e}")
    log.info(f"termine: {sent} enregistrees, {len(all_ids)} vues, complet={complete}")


if __name__ == '__main__':
    main()
