"""
v6.3 backfill migrations — à déposer dans migrations/backfill_v63.py (créer le dossier si absent)

Ordre d'exécution (dépendances) :
  1. backfill_rooms()            — rooms NULL/0 → regex extraction
  2. backfill_homegate_titles()  — utilise rooms déjà populé
  3. cleanup_addresses()         — indépendant
  4. backfill_property_gps()     — avant rescore
  5. (rescore auto via _rescore_all_on_boot dans app.py)

Hypothèses sur le schéma (confirmées par audit code) :
  - Table `properties` avec colonnes : id, title, source_url, description,
    property_type, rooms, address, city, latitude, longitude
  - psycopg2 avec RealDictCursor (db.py:37, db.py:78) → rows = dicts
  - scoring_engine._lookup_city_coords(key) → Optional[Tuple[float, float]]
    accepte soit un nom de ville soit un NPA 4 chiffres (car il consulte
    CITY_COORDS et NPA_COORDS)
  - DB connection via db.get_db() / db.return_db() (pas de pool exposé)

Aucun appel ScrapingBee — uniquement des UPDATEs sur la DB existante.
"""

import re
import logging
from typing import Optional, Tuple, Callable

logger = logging.getLogger('lou-app')


# ------------------------------------------------------------------
# Regex helpers
# ------------------------------------------------------------------

_ROOMS_PATTERNS = [
    re.compile(r'(\d+[.,]?\d*)\s*(?:pi[eè]ces?|pcs|rooms?|Zimmer|p\.)', re.IGNORECASE),
    re.compile(r'(?:appartement|maison|villa|loft|studio)-(\d+[.,]?\d*)-pi(?:eè)?ces?', re.IGNORECASE),
    # "T4" = 4 pièces (français) — word boundary strict pour éviter "T1B2", "WiFi T1", "IoT 4"
    re.compile(r'(?:^|[\s,/\-\(])T([1-9])(?=[\s,/\-\.\)]|$)'),
]


def extract_rooms_from_text(text: Optional[str]) -> Optional[float]:
    """Extrait le nombre de pièces d'un texte libre (titre, URL, description)."""
    if not text:
        return None
    for pattern in _ROOMS_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            val = float(m.group(1).replace(',', '.'))
        except (ValueError, IndexError):
            continue
        # Sanity check
        if 0.5 <= val <= 20:
            return val
    return None


# ------------------------------------------------------------------
# Address cleanup
# ------------------------------------------------------------------

_LEADING_JUNK = re.compile(r'^\s*(?:CH|ch)\s+')
_LEADING_DOT  = re.compile(r'^\s*\.\s*')
_LEADING_NPA  = re.compile(r'^\s*\d{4}\s+')
_TRAILING_SLOGAN = re.compile(r'\s+[A-ZÀ-ÖØ-Þ]{3,}[^a-zà-ÿ]*$')  # "VOTRE NOUVEL HAVRE..."


def clean_address(raw: Optional[str], city: Optional[str] = None) -> str:
    """Normalise une address sale :
       - retire préfixes 'CH ', '.', NPA 4 chiffres
       - coupe tout slogan/description accolé après la ville (si city connue)
    """
    if not raw:
        return (city or '').strip()

    s = raw.strip()
    # Strip leading junk in order
    s = _LEADING_JUNK.sub('', s)
    s = _LEADING_DOT.sub('', s)
    s = _LEADING_NPA.sub('', s)

    # Si city est connue et apparaît dans la chaîne, couper la queue marketing
    if city:
        city_norm = city.strip()
        idx = s.lower().find(city_norm.lower())
        if idx >= 0:
            end = idx + len(city_norm)
            tail = s[end:]
            # Si le tail commence par une majuscule slogan (ex "Charmante maison",
            # "VOTRE NOUVEL HAVRE"), on tronque.
            # On garde si c'est ", Rue X Y" (virgule = continuation address).
            if tail.strip() and not tail.lstrip().startswith(','):
                # Heuristique : tail commence par lettre capitale → slogan
                m = re.match(r'^\s+[A-ZÀ-ÖØ-Þ]', tail)
                if m:
                    s = s[:end]

    # Trim ponctuation résiduelle
    return s.strip(' ,.-')


# ------------------------------------------------------------------
# Backfill #1 — rooms
# ------------------------------------------------------------------

def backfill_rooms(conn) -> Tuple[int, int]:
    """Remplit rooms pour les properties où rooms IS NULL OR rooms = 0.
    Retourne (resolved, total_candidates).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, source_url, description
        FROM properties
        WHERE rooms IS NULL OR rooms = 0
    """)
    rows = cur.fetchall()  # RealDictCursor → liste de dicts
    resolved = 0
    for row in rows:
        rooms = (
            extract_rooms_from_text(row.get('title'))
            or extract_rooms_from_text(row.get('source_url'))
            or extract_rooms_from_text(row.get('description'))
        )
        if rooms is None:
            continue
        cur.execute(
            "UPDATE properties SET rooms = %s WHERE id = %s",
            (rooms, row['id']),
        )
        resolved += 1
    conn.commit()
    cur.close()
    logger.info("Rooms backfill: resolved %s/%s properties", resolved, len(rows))
    return resolved, len(rows)


# ------------------------------------------------------------------
# Backfill #2 — titres Homegate (.â)
# ------------------------------------------------------------------

def backfill_homegate_titles(conn) -> int:
    """Régénère le titre pour les biens dont le titre contient '.â' (mojibake)
    ou commence par 'CHF ' suivi d'un nombre (le scraper a stocké le prix).
    Doit être appelé APRÈS backfill_rooms().
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE properties
        SET title = CASE
            WHEN rooms > 0 THEN
                INITCAP(COALESCE(property_type, 'Bien')) || ' ' ||
                CASE
                    WHEN rooms = FLOOR(rooms) THEN rooms::int::text
                    ELSE trim(trailing '0' from rooms::text)
                END || ' pièces'
            ELSE INITCAP(COALESCE(property_type, 'Bien'))
        END
        WHERE title LIKE '%â%'
           OR title ~ '^CHF\s'
    """)
    rowcount = cur.rowcount
    conn.commit()
    cur.close()
    logger.info("Homegate title regen: updated %s rows", rowcount)
    return rowcount


# ------------------------------------------------------------------
# Backfill #3 — addresses
# ------------------------------------------------------------------

def cleanup_addresses(conn) -> Tuple[int, int]:
    """Nettoie les addresses sales en DB."""
    cur = conn.cursor()
    cur.execute(r"""
        SELECT id, address, city
        FROM properties
        WHERE address LIKE 'CH %'
           OR address LIKE '.%'
           OR address ~ '^\d{4}'
           OR address ~ '[A-Z]{3,}'
    """)
    rows = cur.fetchall()  # RealDictCursor → liste de dicts
    resolved = 0
    for row in rows:
        addr = row.get('address')
        cleaned = clean_address(addr, row.get('city'))
        if cleaned and cleaned != addr:
            cur.execute(
                "UPDATE properties SET address = %s WHERE id = %s",
                (cleaned, row['id']),
            )
            resolved += 1
    conn.commit()
    cur.close()
    logger.info("Address cleanup: cleaned %s/%s rows", resolved, len(rows))
    return resolved, len(rows)


# ------------------------------------------------------------------
# Backfill #4 — GPS properties
# ------------------------------------------------------------------

def backfill_property_gps(
    conn,
    lookup_city_coords: Callable[[str], Optional[Tuple[float, float]]],
) -> Tuple[int, int]:
    """Remplit latitude/longitude pour les properties sans GPS.

    Args:
        conn: psycopg2 connection.
        lookup_city_coords: callable(key) -> (lat, lon) | None
            — accepte city name ou NPA 4 chiffres
            (utilise typiquement scoring_engine._lookup_city_coords
            qui lit CITY_COORDS + NPA_COORDS).

    Doit être appelé AVANT _rescore_all_on_boot() pour que le
    rescore utilise les nouvelles coordonnées.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT id, address, city
        FROM properties
        WHERE latitude IS NULL OR longitude IS NULL
    """)
    rows = cur.fetchall()  # RealDictCursor → liste de dicts
    resolved = 0
    for row in rows:
        addr = row.get('address')
        city = row.get('city')

        coords = None
        # 1. NPA dans l'address
        if addr:
            m = re.search(r'\b(\d{4})\b', addr)
            if m:
                coords = lookup_city_coords(m.group(1))
        # 2. City
        if not coords and city:
            coords = lookup_city_coords(city)

        if coords:
            lat, lon = coords
            cur.execute(
                "UPDATE properties SET latitude = %s, longitude = %s WHERE id = %s",
                (lat, lon, row['id']),
            )
            resolved += 1
    conn.commit()
    cur.close()
    logger.info("Property GPS backfill: resolved %s/%s properties", resolved, len(rows))
    return resolved, len(rows)


# ------------------------------------------------------------------
# Orchestrateur
# ------------------------------------------------------------------

def run_all(conn, lookup_city_coords) -> dict:
    """Exécute les 4 backfills dans le bon ordre.
    À appeler dans app.py au boot, APRÈS la migration GPS zones et
    AVANT _rescore_all_on_boot.

    Renvoie un dict de stats utilisable dans les logs/tests.
    """
    logger.info("=== v6.3 backfill: start ===")
    stats = {}

    try:
        r, t = backfill_rooms(conn)
        stats['rooms'] = {'resolved': r, 'total': t}
    except Exception as e:
        logger.exception("rooms backfill failed: %s", e)
        stats['rooms'] = {'error': str(e)}

    try:
        n = backfill_homegate_titles(conn)
        stats['titles'] = {'updated': n}
    except Exception as e:
        logger.exception("title regen failed: %s", e)
        stats['titles'] = {'error': str(e)}

    try:
        r, t = cleanup_addresses(conn)
        stats['addresses'] = {'resolved': r, 'total': t}
    except Exception as e:
        logger.exception("address cleanup failed: %s", e)
        stats['addresses'] = {'error': str(e)}

    try:
        r, t = backfill_property_gps(conn, lookup_city_coords)
        stats['gps'] = {'resolved': r, 'total': t}
    except Exception as e:
        logger.exception("GPS backfill failed: %s", e)
        stats['gps'] = {'error': str(e)}

    logger.info("=== v6.3 backfill done: %s ===", stats)
    return stats
