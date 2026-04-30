"""
Bon Home — Scrapers immobiliers suisses (v3 — ScrapingBee)
Utilise ScrapingBee pour contourner Cloudflare et scraper tous les portails.

Portails couverts:
  - Homegate, ImmoScout24, Immobilier.ch, Anibis, Acheter-Louer
  - Flatfox (API directe), Comparis, Properstar

Usage:
    from scrapers import scrape_all
    results = scrape_all(city="Lausanne", transaction="location")
"""

import os
import re
import json
import time
import hashlib
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import unicodedata

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')


def _normalize_city(name):
    """Remove accents and lowercase for city comparison.
    'Neuchâtel' → 'neuchatel', 'La Tène' → 'la tene'"""
    s = unicodedata.normalize('NFD', (name or '').lower().strip())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


# --- C2.2 — helpers to clean noisy city strings coming from portals ----------
_POSTAL_PREFIX_RE = re.compile(r'^\s*\d{4}\s*')
_CANTON_SUFFIX_RE = re.compile(r'\s*\(\s*[A-Z]{2}\s*\)\s*$')

def _clean_city(city, fallback_city=None):
    """Normalize a portal-provided city string.
    - strip leading 4-digit postal code ("2016 Cortaillod" → "Cortaillod")
    - strip trailing canton suffix ("Cortaillod (NE)" → "Cortaillod")
    - collapse whitespace
    - if the result is too short (<2 chars), use fallback_city (search term)
    """
    if not city:
        return (fallback_city or '').strip() or None
    s = _POSTAL_PREFIX_RE.sub('', str(city))
    s = _CANTON_SUFFIX_RE.sub('', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,;-')
    if len(s) < 2:
        return (fallback_city or '').strip() or None
    return s


# --- C2.1 — source_url validation --------------------------------------------
# Domain(s) each source must resolve to. The check is "any domain in the list
# appears in the URL host" (accepts www., m., api., subdomains, etc.).
SOURCE_DOMAINS = {
    'Homegate':       ['homegate.ch'],
    'ImmoScout24':    ['immoscout24.ch'],
    'Immobilier.ch':  ['immobilier.ch'],
    'Anibis':         ['anibis.ch'],
    'Acheter-Louer':  ['acheter-louer.ch'],
    'Flatfox':        ['flatfox.ch'],
    'Comparis':       ['comparis.ch'],
    'Properstar':     ['properstar.com', 'properstar.ch'],
    'jouval':         ['jouval.ch'],
    'muller-christe': ['muller-christe.ch', 'mullerchriste.ch'],
    'fidimmobil':     ['fidimmobil.ch'],
}

_INVALID_URL_TOKENS = ('undefined', 'null', 'javascript:', '#', 'about:blank')

def _is_valid_source_url(url, source):
    """Reject empty / garbage URLs, non-http(s) schemes, and domain mismatches."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not u:
        return False
    low = u.lower()
    if low in ('#', '/'):
        return False
    if any(low.startswith(tok) or low == tok for tok in _INVALID_URL_TOKENS):
        return False
    if not (low.startswith('http://') or low.startswith('https://')):
        return False
    expected = SOURCE_DOMAINS.get(source)
    if expected:
        # Extract host from the URL
        try:
            from urllib.parse import urlparse
            host = (urlparse(u).hostname or '').lower()
        except Exception:
            return False
        if not any(host == d or host.endswith('.' + d) for d in expected):
            return False
    return True

SCRAPINGBEE_KEY = os.environ.get('SCRAPINGBEE_API_KEY', '')
SCRAPINGBEE_URL = 'https://app.scrapingbee.com/api/v1'

# --- Scrape cache ----------------------------------------------------------
# DB-backed TTL cache to skip ScrapingBee calls for URLs fetched recently.
# If an URL was scraped successfully < SCRAPE_CACHE_TTL_HOURS ago, _sb_get
# returns (304, '') and the caller treats it as an empty/terminal page.
# The listings from the previous run are still in `properties`, so no data
# is lost — we just save credits.
SCRAPE_CACHE_TTL_HOURS = int(os.environ.get('SCRAPE_CACHE_TTL_HOURS', '12'))
SCRAPE_CACHE_DISABLED = os.environ.get('SCRAPE_CACHE_DISABLE', '').lower() in ('1', 'true', 'yes')

# v6.3.4 (P0 SSL "bad record mac" fix — 2026-04-20) :
# La conn cache était AUPARAVANT un singleton module-level (_CACHE_CONN) partagé
# entre tous les threads gthread (4/worker) + les ThreadPoolExecutor des scrapers.
# psycopg2 connections NE SONT PAS thread-safe pour usage concurrent : deux
# `cur.execute()` simultanés sur la même conn entrelacent les octets sur la
# socket TLS → "SSL error: decryption failed or bad record mac" sur la lecture
# suivante. Fix : thread-local conn + auto-reconnect sur erreur + un schema
# init flag partagé (CREATE TABLE IF NOT EXISTS, une seule fois par worker).
import threading as _cache_threading
_CACHE_TLS = _cache_threading.local()
_CACHE_SCHEMA_READY = False
_CACHE_SCHEMA_LOCK = _cache_threading.Lock()
_CACHE_GLOBAL_FAILED = False  # set True si DATABASE_URL absent → on skip définitivement


def _cache_ensure_schema(conn):
    """CREATE TABLE IF NOT EXISTS, idempotent, une fois par process."""
    global _CACHE_SCHEMA_READY
    if _CACHE_SCHEMA_READY:
        return
    with _CACHE_SCHEMA_LOCK:
        if _CACHE_SCHEMA_READY:
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_cache (
                url_hash VARCHAR(64) PRIMARY KEY,
                content_hash VARCHAR(64),
                scraped_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.close()
        _CACHE_SCHEMA_READY = True


def _cache_conn():
    """Return a thread-local autocommit psycopg2 conn for the scrape cache.
    Each thread gets its OWN connection → zero concurrent-use on a single
    SSL socket. Reconnects automatically if the previous conn is dead."""
    global _CACHE_GLOBAL_FAILED
    if SCRAPE_CACHE_DISABLED or _CACHE_GLOBAL_FAILED:
        return None
    conn = getattr(_CACHE_TLS, 'conn', None)
    if conn is not None:
        # Cheap liveness check : psycopg2 sets .closed non-zero when the
        # backend has gone away. Real health is validated by autocommit
        # semantics — si la conn est vraiment HS, l'appelant va catcher et
        # déclencher _cache_drop_conn() qui invalide le tls pour ce thread.
        if getattr(conn, 'closed', 0) == 0:
            return conn
        # Conn dropped — clear and reopen below
        _cache_drop_conn()
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        _CACHE_GLOBAL_FAILED = True
        return None
    try:
        import psycopg2 as _pg
        conn = _pg.connect(dsn)
        conn.autocommit = True
        _cache_ensure_schema(conn)
        _CACHE_TLS.conn = conn
        return conn
    except Exception as e:
        log.warning(f"scrape_cache connect failed (thread={_cache_threading.get_ident()}): {e}")
        return None


def _cache_drop_conn():
    """Close + forget the thread-local cache conn so the next call reconnects.
    Called by _cache_is_fresh / _cache_mark when a query raises (any error =
    suspect SSL state, don't reuse)."""
    conn = getattr(_CACHE_TLS, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    try:
        del _CACHE_TLS.conn
    except AttributeError:
        pass


def _url_hash(url):
    return hashlib.sha256((url or '').encode('utf-8')).hexdigest()


def _cache_is_fresh(url):
    """True if we scraped this URL successfully less than TTL hours ago.
    Sur toute exception, on dégage la conn thread-local (SSL state suspect)
    pour qu'un futur appel reconnecte proprement."""
    conn = _cache_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM scrape_cache WHERE url_hash = %s AND scraped_at > NOW() - (%s || ' hours')::interval",
            (_url_hash(url), str(SCRAPE_CACHE_TTL_HOURS))
        )
        hit = cur.fetchone() is not None
        cur.close()
        return hit
    except Exception as e:
        log.warning(f"scrape_cache read failed (dropping conn): {e}")
        _cache_drop_conn()
        return False


def _cache_mark(url, html):
    """Upsert a cache entry after a successful scrape.
    Sur toute exception, on dégage la conn thread-local (cf _cache_is_fresh)."""
    conn = _cache_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scrape_cache (url_hash, content_hash, scraped_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (url_hash) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                scraped_at = NOW()
        """, (_url_hash(url), hashlib.sha256((html or '').encode('utf-8', errors='ignore')).hexdigest()))
        cur.close()
    except Exception as e:
        log.warning(f"scrape_cache write failed (dropping conn): {e}")
        _cache_drop_conn()

# Swiss city → canton mapping
CITY_CANTONS = {
    'lausanne': 'VD', 'geneve': 'GE', 'genève': 'GE', 'neuchatel': 'NE',
    'neuchâtel': 'NE', 'fribourg': 'FR', 'sion': 'VS', 'montreux': 'VD',
    'nyon': 'VD', 'morges': 'VD', 'yverdon': 'VD', 'yverdon-les-bains': 'VD',
    'la chaux-de-fonds': 'NE', 'bienne': 'BE', 'biel': 'BE',
    'delemont': 'JU', 'delémont': 'JU', 'berne': 'BE', 'bern': 'BE',
    'vevey': 'VD', 'renens': 'VD', 'zurich': 'ZH', 'zürich': 'ZH',
    'basel': 'BS', 'bâle': 'BS', 'lugano': 'TI', 'lucerne': 'LU',
    'luzern': 'LU', 'winterthur': 'ZH', 'st. gallen': 'SG',
    'carouge': 'GE', 'meyrin': 'GE', 'prilly': 'VD', 'pully': 'VD',
    'ecublens': 'VD', 'sierre': 'VS', 'martigny': 'VS',
    'colombier': 'NE', 'peseux': 'NE', 'boudry': 'NE', 'cortaillod': 'NE',
    'marin-epagnier': 'NE', 'hauterive': 'NE', 'saint-blaise': 'NE',
    'le locle': 'NE', 'val-de-travers': 'NE', 'fleurier': 'NE',
    'milvignes': 'NE', 'la tène': 'NE', 'le landeron': 'NE',
    'bevaix': 'NE', 'val-de-ruz': 'NE', 'corcelles-cormondrèche': 'NE',
}


def _direct_get(url, timeout=20):
    """Fetch a URL directly (no ScrapingBee, no credits used).
    Returns (status_code, html_text). Use only on portals that don't block scrapers.
    """
    try:
        r = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.8',
        }, timeout=timeout)
        r.encoding = 'utf-8'
        log.info(f"[Direct] {url[:60]}... → HTTP {r.status_code} ({len(r.text)} bytes)")
        return r.status_code, r.text
    except Exception as e:
        log.error(f"[Direct] Error fetching {url}: {e}")
        return 0, ''


def _smart_get(url, render_js=False, try_direct=False):
    """Try direct request first (0 credits). Fall back to ScrapingBee if direct fails.
    Use this on portals where direct sometimes works (saves credits).
    """
    if try_direct:
        status, html = _direct_get(url)
        # Accept if status 200 AND content looks like a real page (not a bot-block redirect)
        if status == 200 and len(html) > 3000 and 'captcha' not in html.lower()[:5000]:
            return status, html
        log.info(f"[Direct] Fallback to ScrapingBee for {url[:60]}...")
    return _sb_get(url, render_js=render_js)


# v6.3.3 (QA1) : thread-local flag pour bypasser le cache DB de _sb_get.
# Utilisé par l'endpoint /api/stats/listings-qa qui doit mesurer la fraîcheur
# live d'un portail — sinon on rejoue du cache post-cron et live_count=0.
import threading as _threading
_SB_CONTEXT = _threading.local()
_SB_BUDGET_TLS = _threading.local()

def _sb_bypass_cache_active():
    return bool(getattr(_SB_CONTEXT, 'bypass_cache', False))


class sb_bypass_cache:
    """Context manager : `with sb_bypass_cache(): scrape_homegate(...)`
    désactive le cache DB pour ce thread uniquement. Re-entrant safe."""
    def __enter__(self):
        self._prev = getattr(_SB_CONTEXT, 'bypass_cache', False)
        _SB_CONTEXT.bypass_cache = True
        return self
    def __exit__(self, *_):
        _SB_CONTEXT.bypass_cache = self._prev


class sb_budget:
    """Context manager : borne le temps mural TOTAL des appels _sb_get sur ce
    thread. Budget épuisé → _sb_get retourne (0, '') sans crédit ni attente.
    Utilisé par les routes synchrones (listings_qa) pour fail-fast plutôt que
    bloquer un worker 300s sur un portail lent. Re-entrant safe (stacké)."""
    def __init__(self, seconds):
        self.seconds = float(seconds)
    def __enter__(self):
        self._prev = getattr(_SB_BUDGET_TLS, 'deadline', None)
        _SB_BUDGET_TLS.deadline = time.monotonic() + self.seconds
        return self
    def __exit__(self, *_):
        _SB_BUDGET_TLS.deadline = self._prev


def _sb_remaining():
    """Secondes restantes dans le sb_budget du thread, ou None si inactif."""
    deadline = getattr(_SB_BUDGET_TLS, 'deadline', None)
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _sb_get(url, render_js=False, premium_proxy=False):
    """Fetch a URL via ScrapingBee. Returns (status_code, html_text).
    If the URL was scraped successfully less than SCRAPE_CACHE_TTL_HOURS ago,
    returns (304, '') without spending a credit — callers treat it as an
    empty/terminal page. Bypassed if `sb_bypass_cache` context active.

    Si un `sb_budget` est actif sur ce thread, borne chaque requests.get au
    temps restant (tuple connect/read) et coupe retries + sleeps dès que le
    budget passe sous le seuil — pour éviter de tenir un worker gunicorn
    pendant 300s sur un portail lent (cas /api/stats/listings-qa).

    v6.4.5 : kwarg `premium_proxy` (default False = stealth_proxy comme avant).
    Si True, démarre directement avec premium_proxy=true pour passer DataDome
    et autres anti-bots qui résistent au stealth (~+10 crédits/call). Utilisé
    par le link health worker sur Homegate où le stealth est inefficace."""
    remaining = _sb_remaining()
    if remaining is not None and remaining <= 0:
        log.info(f"[ScrapingBee] budget exhausted, skipping {url[:60]}...")
        return 0, ''

    # Cache short-circuit — save credits on URLs we already hit recently
    if not _sb_bypass_cache_active() and _cache_is_fresh(url):
        log.info(f"[cache-hit] skip {url[:80]}... (< {SCRAPE_CACHE_TTL_HOURS}h)")
        return 304, ''

    if not SCRAPINGBEE_KEY:
        log.warning("SCRAPINGBEE_API_KEY not set — falling back to direct request")
        try:
            r = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            }, timeout=15)
            if r.status_code == 200 and r.text:
                _cache_mark(url, r.text)
            return r.status_code, r.text
        except Exception as e:
            log.error(f"Direct request failed: {e}")
            return 0, ''

    # v6.3.3 (SC2) : jusqu'à 3 tentatives.
    #   - 500/502/504 : retry simple (3s).
    #   - 403/429 : retry avec premium_proxy=true (consomme +10 crédits mais
    #     passe les protections anti-bot qui ont caused le block).
    # v6.3.4 (P0 sync-hang fix) : avec sb_budget actif, 2 tentatives et 1s
    # de sleep — on préfère rendre la main à l'appelant vite que d'épuiser
    # le budget sur un portail bloqué.
    def _build_params(premium=False):
        p = {
            'api_key': SCRAPINGBEE_KEY,
            'url': url,
            'country_code': 'ch',
        }
        if premium:
            p['premium_proxy'] = 'true'
        else:
            p['stealth_proxy'] = 'true'
        if render_js:
            p['render_js'] = 'true'
            p['wait'] = 3000
        return p

    # v6.4.5 : si caller force `premium_proxy=True`, on skip directement
    # le mode stealth ; `premium_retried = True` empêche le retry "403→bascule
    # premium" puisqu'on est déjà premium dès la 1ère tentative.
    params = _build_params(premium=premium_proxy)
    premium_retried = bool(premium_proxy)

    has_budget = remaining is not None
    max_attempts = 2 if has_budget else 3
    sleep_5xx = 1 if has_budget else 3
    sleep_block = 1 if has_budget else 2
    # Connect timeout court — un DNS/TCP lent ne doit pas consommer toute la
    # fenêtre read. Passe en tuple (connect, read) à requests pour borner les
    # deux indépendamment : sinon timeout=N ne s'applique qu'au read.
    connect_to = 5

    for attempt in range(max_attempts):
        remaining = _sb_remaining()
        if remaining is not None and remaining <= 0:
            log.info(f"[ScrapingBee] budget exhausted mid-retry, aborting {url[:60]}...")
            return 0, ''
        if remaining is not None:
            # Laisse 0.5s de marge pour que le budget expire proprement AVANT
            # que requests ne lève un Timeout (évite l'exception quand on aurait
            # pu juste retourner (0, '')).
            read_to = max(5, min(175, remaining - connect_to - 0.5))
        else:
            read_to = 175
        try:
            r = requests.get(SCRAPINGBEE_URL, params=params, timeout=(connect_to, read_to))
            r.encoding = 'utf-8'  # Force UTF-8 to avoid Latin-1 misdetection
            log.info(f"[ScrapingBee] {url[:60]}... → HTTP {r.status_code} ({len(r.text)} bytes)")

            # 5xx : retry stealth
            if r.status_code in (500, 502, 504) and attempt < max_attempts - 1:
                remaining = _sb_remaining()
                if remaining is not None and remaining < sleep_5xx:
                    log.warning(f"[ScrapingBee] Server error {r.status_code}, budget<{sleep_5xx}s, giving up")
                    return r.status_code, r.text
                log.warning(f"[ScrapingBee] Server error {r.status_code}, retrying in {sleep_5xx}s...")
                time.sleep(sleep_5xx)
                continue

            # 403/429 : retry avec premium_proxy (anti-bot contourné)
            if r.status_code in (403, 429) and not premium_retried and attempt < max_attempts - 1:
                remaining = _sb_remaining()
                if remaining is not None and remaining < sleep_block:
                    log.warning(f"[ScrapingBee] Blocked ({r.status_code}), budget<{sleep_block}s, giving up")
                    return r.status_code, r.text
                log.warning(f"[ScrapingBee] Blocked ({r.status_code}), retrying with premium_proxy")
                premium_retried = True
                params = _build_params(premium=True)
                time.sleep(sleep_block)
                continue

            if r.status_code == 200 and r.text:
                _cache_mark(url, r.text)
            return r.status_code, r.text
        except requests.exceptions.Timeout:
            # Timeout géré par le retry + sb_budget → l'app renvoie quand même
            # (0, '') au caller qui renvoie 200. Pas d'alerte Sentry ERROR pour
            # ça : c'est une condition nominale sous charge/portail lent.
            log.warning(f"[ScrapingBee] TIMEOUT fetching {url} (attempt {attempt+1})")
            if attempt < max_attempts - 1:
                remaining = _sb_remaining()
                if remaining is not None and remaining < sleep_5xx:
                    return 0, ''
                time.sleep(sleep_5xx)
                continue
            return 0, ''
        except Exception as e:
            log.error(f"[ScrapingBee] Error fetching {url}: {e}")
            return 0, ''
    return 0, ''


def _make_property(external_id, source, source_url, title, description,
                   property_type, transaction, price, rooms, surface,
                   floor, address, city, canton, postal_code,
                   latitude, longitude, features, images, published_at,
                   search_city=None):
    """Create a standardized property dict. `search_city` is used as fallback
    when the portal-provided city is empty or garbage (C2.2)."""
    # C2.2: clean portal-provided city
    city = _clean_city(city, fallback_city=search_city)
    # Fix truncated sale prices: no property in Switzerland sells for < 10'000 CHF
    # Portals like Properstar sometimes display "965" meaning 965'000
    if transaction == 'achat' and price and price < 10000:
        price = price * 1000
    # Clean title: keep accents, remove only control chars
    clean_title = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title or '').strip()
    # If title is just a price like "CHF 688,270." or "701'380" or "1,630.–", clear it
    if clean_title and re.match(r'^(?:CHF\s*)?[\d\s\'\',.]+[.\u2013\u2014\-]*$', clean_title):
        clean_title = ''
    # If title starts with "CHF" followed by numbers (possibly with trailing text like ".–Plus"), strip that
    # Handles: "CHF 1,630.–", "CHF 2,200.–Plus", "CHF 840,000.Premium"
    clean_title = re.sub(r'^CHF\s*[\d\s\'\',.]+[.\u2013\u2014\-]*\w*\s*', '', clean_title).strip()
    # Remove bare price prefix at start: "1'630.–Plus ...", "701'380 ..."
    clean_title = re.sub(r'^[\d\s\'\',.]+[.\u2013\u2014\-]*(?:CHF|Fr\.?)?\s*', '', clean_title).strip() if re.match(r'^[\d\',.]+', clean_title) else clean_title
    # Remove postal codes at start of title (e.g., "2016 Cortaillod ...")
    clean_title = re.sub(r'^\d{4}\s+', '', clean_title).strip()
    # Remove "Travel time X min" residuals from Homegate
    clean_title = re.sub(r'\bTravel time\s+\d+\s*min\b', '', clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r'\btemps de trajet\s+\d+\s*min\b', '', clean_title, flags=re.IGNORECASE).strip()
    # Final cleanup: if title is now just dashes/dots/whitespace, clear it
    if clean_title and re.match(r'^[\s.\u2013\u2014\-]+$', clean_title):
        clean_title = ''

    desc_clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', (description or '')[:1500]).strip()
    # Language filter: skip clearly non-French listings
    if not _is_french_or_neutral(clean_title) and not _is_french_or_neutral(desc_clean):
        return None  # Will be filtered out by caller

    return {
        'external_id': str(external_id),
        'source': source,
        'source_url': source_url or '',
        'title': clean_title,
        'description': desc_clean,
        'property_type': property_type or 'appartement',
        'transaction': transaction,
        'price': price,
        'currency': 'CHF',
        'price_unit': 'mois' if transaction == 'location' else 'total',
        'rooms': rooms,
        'surface': surface,
        'floor': floor,
        'address': re.sub(r'\bTravel time\s+\d+\s*min\b', '', address or '', flags=re.IGNORECASE).strip(),
        'city': city,
        'canton': canton or CITY_CANTONS.get((city or '').lower(), ''),
        'postal_code': postal_code,
        'latitude': latitude,
        'longitude': longitude,
        'features': features or [],
        'images': images or [],
        'contact_name': None, 'contact_phone': None, 'contact_email': None,
        'published_at': published_at,
        'scraped_at': datetime.now().isoformat(),
    }


def _is_french_or_neutral(text):
    """Check if text is in French or language-neutral (numbers, addresses).
    Returns False for clearly German/Italian listings."""
    if not text or len(text) < 5:
        return True  # Too short to determine, keep it
    t = text.lower()
    # German indicators
    de_words = ['wohnung', 'zimmer', 'mieten', 'kaufen', 'haus', 'strasse', 'wohnfläche',
                'verkauf', 'mietwohnung', 'eigentumswohnung', 'erdgeschoss', 'obergeschoss',
                'stockwerk', 'sofort', 'bezugsbereit', 'neubau', 'renoviert', 'möbliert',
                'balkon', 'terrasse', 'garten', 'stellplatz', 'tiefgarage', 'waschküche']
    # Italian indicators
    it_words = ['appartamento', 'affitto', 'vendita', 'camera', 'locale', 'monolocale',
                'bilocale', 'trilocale', 'piano', 'disponibile', 'subito']
    de_count = sum(1 for w in de_words if w in t)
    it_count = sum(1 for w in it_words if w in t)
    return (de_count + it_count) < 2  # Allow 1 common word (balkon, terrasse)


def _guess_type(text):
    if not text:
        return 'appartement'
    t = text.lower()
    if any(w in t for w in ['maison', 'villa', 'chalet', 'house']):
        return 'maison'
    if 'studio' in t:
        return 'studio'
    if 'loft' in t:
        return 'loft'
    if any(w in t for w in ['attique', 'penthouse', 'attic']):
        return 'attique'
    if 'duplex' in t:
        return 'duplex'
    return 'appartement'


def _extract_postal(address):
    if not address:
        return None
    m = re.search(r'\b(\d{4})\b', address)
    return m.group(1) if m else None


def _is_numeric(val):
    """Check if a value can be safely converted to float."""
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        try:
            float(val)
            return True
        except (ValueError, TypeError):
            return False
    return False


def _clean_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    text = str(val)
    # Remove all thousand separators and currency symbols
    # Remove currency, units, and all thousand separators
    text = re.sub(r'(?i)CHF|Fr\.?|/mois|/m|/an|par mois|mensuel', '', text)
    text = text.replace('\u2019', '').replace('\u00a0', '').replace("'", '')
    text = text.replace(',', '').replace('.–', '').replace('.-', '').replace(' ', '')
    # Strip any remaining non-ASCII characters (garbled UTF-8 like â, Â, etc.)
    text = re.sub(r'[^\x00-\x7F]', '', text)
    # Handle dots as thousand separator (e.g., 2.864.400): if multiple dots, they're separators
    if text.count('.') > 1:
        text = text.replace('.', '')
    elif '.' in text:
        # Single dot could be decimal (e.g., 1500.00) — remove decimal part
        text = text.split('.')[0]
    nums = re.findall(r'\d+', text)
    if nums:
        # Join all digit groups to handle "1 300" → "1300"
        n = int(''.join(nums))
        return n if n > 0 else None
    return None


def _clean_rooms(text):
    if not text:
        return None
    text = str(text).replace('½', '.5')
    # Look for explicit room patterns first: "3.5 pièces", "4 rooms", "3½ pcs"
    room_pat = re.search(r'(\d+\.?\d*)\s*(?:pi[eè]ces?|rooms?|pcs?|chambres?|zimmer|½)', text, re.IGNORECASE)
    if room_pat:
        val = float(room_pat.group(1))
        if 0.5 <= val <= 20:
            return val
    # Fallback: find reasonable room number (skip IDs, surfaces, etc.)
    nums = re.findall(r'(\d+\.5|\d+\.\d)', text)
    for n in nums:
        val = float(n)
        if 0.5 <= val <= 20:
            return val
    return None


def _clean_surface(text):
    if not text:
        return None
    s = str(text)
    # Try explicit m²/m2 pattern first
    m2 = re.search(r'(\d+)\s*m[²2\u00B2]', s)
    if m2:
        return int(m2.group(1))
    # Try "XX m" (space before m)
    m = re.search(r'(\d+)\s*m\b', s)
    if m:
        return int(m.group(1))
    # Pure numeric string (e.g. "85" or "85.0" from structured data)
    try:
        val = int(float(s))
        if 5 <= val <= 5000:  # Reasonable surface range
            return val
    except (ValueError, TypeError):
        pass
    return None


# ============================================================
# HOMEGATE — via ScrapingBee + __NEXT_DATA__
# ============================================================

def _homegate_parse_next_data(html, transaction, city):
    """
    Parse le __NEXT_DATA__ d'une page Homegate (Next.js app).
    Retourne une list[dict] de properties prêtes, ou [] si parsing KO.

    Les paths Homegate sont instables : on tente quelques paths connus
    puis un deep-search récursif pour tomber sur la première liste
    d'objets qui ressemble à des listings (id + price/listingType).
    """
    nd_match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not nd_match:
        return []
    try:
        data = json.loads(nd_match.group(1))
    except Exception as e:
        log.warning(f"[Homegate] __NEXT_DATA__ JSON error: {e}")
        return []

    # Paths connus (à compléter quand Homegate bouge).
    items = []
    try:
        for path_fn in [
            lambda d: d.get('props', {}).get('pageProps', {}).get('initialState', {}).get('searchResultList', {}).get('listings', []),
            lambda d: d.get('props', {}).get('pageProps', {}).get('listings', []),
            lambda d: d.get('props', {}).get('pageProps', {}).get('searchResult', {}).get('listings', []),
            lambda d: d.get('props', {}).get('pageProps', {}).get('items', []),
        ]:
            try:
                out = path_fn(data) or []
                if out:
                    items = out
                    break
            except (AttributeError, TypeError):
                continue
    except Exception:
        pass

    # Deep-search si paths connus ne matchent pas.
    if not items:
        def _find(obj, depth=0):
            if depth > 6:
                return []
            if isinstance(obj, list) and len(obj) >= 2:
                if all(isinstance(i, dict) and ('id' in i or 'listingId' in i) and any(k in i for k in ('price', 'prices', 'listing', 'rooms')) for i in obj[:3]):
                    return obj
            if isinstance(obj, dict):
                for v in obj.values():
                    r = _find(v, depth + 1)
                    if r:
                        return r
            return []
        items = _find(data)

    if not items:
        return []

    results = []
    for it in items:
        try:
            listing = it.get('listing', it) if isinstance(it, dict) else {}
            lid = (
                it.get('id') or it.get('listingId') or
                listing.get('id') or listing.get('listingId')
            )
            if not lid:
                continue

            # URL : Homegate expose souvent un path relatif.
            href = (
                listing.get('url') or listing.get('listingUrl') or
                listing.get('detailUrl') or it.get('url') or
                f"/{'mieten' if transaction == 'location' else 'kaufen'}/{lid}"
            )
            if href.startswith('/'):
                href = 'https://www.homegate.ch' + href

            # Price : chercher dans plusieurs champs.
            price = None
            for p in (
                listing.get('prices', {}).get('rent', {}).get('gross') if transaction == 'location' else listing.get('prices', {}).get('buy', {}).get('price'),
                listing.get('price'),
                listing.get('grossPrice'),
                it.get('price'),
            ):
                if p:
                    try:
                        price = float(p)
                        break
                    except (TypeError, ValueError):
                        continue

            # Rooms / surface / title / address.
            chars = listing.get('characteristics', {}) if isinstance(listing, dict) else {}
            rooms = chars.get('numberOfRooms') or listing.get('rooms') or it.get('rooms')
            surface = chars.get('livingSpace') or listing.get('surface') or it.get('surface')
            try:
                rooms = float(rooms) if rooms is not None else None
            except (TypeError, ValueError):
                rooms = None
            try:
                surface = int(surface) if surface is not None else None
            except (TypeError, ValueError):
                surface = None

            title = (
                (listing.get('localization', {}).get('de', {}) or {}).get('text', {}).get('title')
                or listing.get('title')
                or it.get('title', '')
            )
            address_obj = listing.get('address', {}) if isinstance(listing, dict) else {}
            address = ''
            if isinstance(address_obj, dict):
                street = address_obj.get('street') or ''
                plz = address_obj.get('postalCode') or ''
                loc = address_obj.get('locality') or ''
                address = ', '.join(x for x in (street, f"{plz} {loc}".strip()) if x).strip(', ')

            # Images.
            images = []
            for img in (listing.get('pictures') or listing.get('images') or []):
                if isinstance(img, dict):
                    url = img.get('url') or img.get('originalUrl') or img.get('src')
                else:
                    url = img
                if url:
                    images.append(url)
            images = list(dict.fromkeys(images))[:5]

            card_text = f"{title} {address}".strip()

            results.append(_make_property(
                external_id=f"hg-{lid}", source='Homegate',
                source_url=href,
                title=title or '', description='',
                property_type=_guess_type(card_text),
                transaction=transaction,
                price=price, rooms=rooms, surface=surface, floor=None,
                address=address, city=city,
                canton=CITY_CANTONS.get(city.lower(), ''),
                postal_code=_extract_postal(address),
                latitude=None, longitude=None,
                features=[], images=images, published_at=None,
                search_city=city,
            ))
        except Exception as e:
            log.debug(f"[Homegate JSON] Item parse error: {e}")
    return results


def scrape_homegate(city="Lausanne", transaction="location", max_pages=4):
    log.info(f"[Homegate] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = _normalize_city(city).replace(' ', '-')
    # Add canton suffix for ambiguous city names (e.g., colombier-ne, hauterive-ne).
    # v6.4.3 BUG B (correction) : `saint-blaise` RÉINTRODUIT après validation
    # empirique du run nocturne. Logs : `city-saint-blaise` → HTTP 404 sur
    # Homegate, `city-saint-blaise-ne` → HTTP 200. La désambig est nécessaire
    # côté Homegate (le slug nu n'est pas reconnu, peut-être parce qu'IS24/
    # Homegate ont chacun leur propre indexation et Homegate impose le suffixe
    # canton pour cette commune en particulier). À noter : retiré côté
    # ImmoScout24 où le slug nu n'a pas montré de 404 (logs IS24 montraient
    # timeout/504, pas 404).
    canton = CITY_CANTONS.get(city.lower(), '')
    if canton and slug in ('colombier', 'hauterive', 'saint-blaise', 'corcelles-cormondrèche', 'corcelles-cormondr', 'corcelles-cormondrche'):
        slug = f"{slug}-{canton.lower()}"

    consecutive_errors = 0
    consecutive_empty = 0   # v6.4.2 BUG A : pages HTTP 200 avec 0 listings extraits
    for page in range(1, max_pages + 1):
        url = f"https://www.homegate.ch/{tx}/real-estate/city-{slug}/matching-list?ep={page}"
        # render_js=False → 1 credit instead of 5. Homegate's SSR payload contains all listings.
        status, html = _sb_get(url, render_js=False)

        if status != 200:
            log.warning(f"[Homegate] Page {page}: HTTP {status}")
            consecutive_errors += 1
            if consecutive_empty + consecutive_errors >= 2:
                log.warning(f"[Homegate] stopping pagination: {consecutive_empty} empty + {consecutive_errors} errors consecutive")
                break
            continue
        consecutive_errors = 0

        # v6.4.2 BUG A : capture len(results) avant parsing pour détecter les
        # pages HTTP 200 qui n'ont produit aucun listing (JSON vide, cards=[],
        # ou cards présentes mais toutes fail de parse). Signal fin de pagination.
        results_before_page = len(results)

        soup = BeautifulSoup(html, 'html.parser')

        # ============================================================
        # PRIMARY : Homegate is a Next.js app → parse __NEXT_DATA__ JSON.
        # ============================================================
        json_listings = _homegate_parse_next_data(html, transaction, city)
        if json_listings:
            log.info(f"[Homegate] Page {page}: {len(json_listings)} listings via __NEXT_DATA__")
            results.extend(json_listings)
            consecutive_empty = 0   # v6.4.2 BUG A : reset sur page productive
            time.sleep(1)
            continue

        # ============================================================
        # FALLBACK : HTML parsing via selectors multi-layers.
        # L'ancien sélecteur unique [data-test="result-list-item"] était
        # SPOF : si Homegate renommait cet attr, on récoltait 0. Cascade.
        # ============================================================
        cards = (
            soup.select('[data-test="result-list-item"]') or
            soup.select('[data-testid="result-list-item"]') or
            soup.select('article[data-test^="result"]') or
            soup.select('[role="listitem"][data-test]') or
            soup.select('[class*="ResultListItem"]') or
            soup.select('[class*="result-list-item"]')
        )
        log.info(f"[Homegate] Page {page}: {len(cards)} cards via HTML fallback")
        if not cards:
            log.warning(f"[Homegate] No listings found on page {page} (JSON + HTML both empty) — layout may have changed")

        for card in cards:
            try:
                # Link and ID
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.homegate.ch' + href
                eid = re.search(r'/(\d+)', href)
                lid = eid.group(1) if eid else ''
                if not lid:
                    continue

                # Get all text from the card
                card_text = card.get_text(' ', strip=True)

                # Price: look for CHF pattern or number followed by .–
                price = None
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'',.\u2019\u00a0]+)", card_text)
                if not price_match:
                    price_match = re.search(r"([\d'',.\u2019\u00a0]+)\s*(?:CHF|Fr|\.–|/mois|/m)", card_text)
                if price_match:
                    price = _clean_price(price_match.group(1))

                # Rooms: look for X.5 pièces or X½ or similar
                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pièce|piece|room|Zimmer|pi\.|pcs)', card_text, re.IGNORECASE)
                if not rooms_match:
                    # Try "X.5" or "X,5" pattern (common Swiss format)
                    rooms_match = re.search(r'(\d+[.,]5)\b', card_text)
                if not rooms_match:
                    # Try "X rooms" or "X Zi" or "X ½"
                    rooms_match = re.search(r'(\d+)\s*½', card_text)
                    if rooms_match:
                        rooms = float(rooms_match.group(1)) + 0.5
                if rooms_match and rooms is None:
                    rooms = _clean_rooms(rooms_match.group(1))

                # v6.3 fallback: if card_text regex missed, try URL & (not-yet-set) title.
                # Homegate URLs often carry "/appartement-3-5-pieces-..." which is reliable.
                if rooms is None:
                    try:
                        from migrations.backfill_v63 import extract_rooms_from_text
                        rooms = extract_rooms_from_text(href)
                    except Exception:
                        pass

                # Surface: look for XX m²
                surface = None
                surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surface_match:
                    surface = int(surface_match.group(1))

                # Address: look for postal code pattern (4 digits + city name)
                address = ''
                addr_match = re.search(r'(\d{4}\s+\w[\w\s-]+)', card_text)
                if addr_match:
                    address = addr_match.group(1).strip()

                # Title: first meaningful text (skip if it's just a price)
                title = ''
                for el in card.select('h2, h3, [class*="title"], [class*="Title"]'):
                    t = el.get_text(strip=True)
                    if t and len(t) > 3 and not re.match(r'^(?:CHF\s*)?[\d\s\'\',.]+\.?$', t):
                        title = t
                        break
                if not title:
                    # Build a descriptive fallback title
                    parts = []
                    if rooms:
                        parts.append(f"{rooms} pcs")
                    if surface:
                        parts.append(f"{surface} m\u00B2")
                    if address:
                        parts.append(address)
                    title = ', '.join(parts) if parts else ''

                # Images: look for img tags in the card
                images = []
                for img_el in card.select('img[src]'):
                    src = img_el.get('src', '')
                    if src and ('homegate' in src or 'cloudinary' in src or src.startswith('http')) and 'logo' not in src.lower() and 'icon' not in src.lower():
                        images.append(src)
                # Also check lazy-loaded images
                for img_el in card.select('img[data-src], [data-lazy], [data-original]'):
                    src = img_el.get('data-src') or img_el.get('data-lazy') or img_el.get('data-original') or ''
                    if src and src.startswith('http') and 'logo' not in src.lower():
                        images.append(src)
                # Also check background-image in style attributes
                for el in card.select('[style*="background"]'):
                    style = el.get('style', '')
                    bg_match = re.search(r'url\(["\']?(https?://[^"\')\s]+)', style)
                    if bg_match:
                        images.append(bg_match.group(1))
                images = list(dict.fromkeys(images))[:5]  # dedupe, max 5

                results.append(_make_property(
                    external_id=f"hg-{lid}", source='Homegate',
                    source_url=href,
                    title=title, description='',
                    property_type=_guess_type(card_text),
                    transaction=transaction,
                    price=price, rooms=rooms, surface=surface, floor=None,
                    address=address, city=city,
                    canton=CITY_CANTONS.get(city.lower(), ''),
                    postal_code=_extract_postal(address),
                    latitude=None, longitude=None,
                    features=[], images=images, published_at=None,
                                    search_city=city,
))
            except Exception as e:
                log.debug(f"[Homegate] Card parse error: {e}")

        # v6.4.2 BUG A : empty-page detection. HTTP 200 + 0 listings extraits
        # = fin de pagination (ou layout portail cassé). Sans ça, le scraper
        # brûlait 10-18 pages vides avant de stop sur "2 consecutive errors",
        # cramait 300s de sb_budget → ImmoScout n'était jamais appelé.
        # Page 1 vide → ville inconnue du portail, break immédiat.
        # Sinon break dès que empty+errors >= 2 consécutifs.
        if len(results) == results_before_page:
            consecutive_empty += 1
            if page == 1:
                log.info(f"[Homegate] Page 1 empty → ville sans résultat sur ce portail, pagination stoppée")
                break
            if consecutive_empty + consecutive_errors >= 2:
                log.info(f"[Homegate] stopping pagination: {consecutive_empty} empty + {consecutive_errors} errors consecutive")
                break
        else:
            consecutive_empty = 0

        time.sleep(1)

    log.info(f"[Homegate] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOSCOUT24 — via ScrapingBee
# ============================================================

# DEPRECATED for monitoring (DataDome blocked, see commit history v6.4.4).
# Kept for legacy/reference. Not called from qa_recall_worker.py anymore.
# Reason : DataDome challenge sur stealth_proxy → 504 systématique ; bascule
# en premium_proxy = 5× le coût/page. Décision produit : retirer IS24 plutôt
# que payer pour scraper un acteur qui essaie activement de nous bloquer.
# Le cron prod (cron_job.py) peut encore l'utiliser si besoin futur ; le
# QA monitoring (qa_recall_worker.py) ne l'appelle plus.
def scrape_immoscout(city="Lausanne", transaction="location", max_pages=4):
    log.info(f"[ImmoScout24] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = _normalize_city(city).replace(' ', '-')
    canton = CITY_CANTONS.get(city.lower(), '')
    # Add canton suffix for disambiguation (e.g., colombier-ne).
    # v6.4.2 BUG B : `saint-blaise` retiré par symétrie avec Homegate.
    # ImmoScout n'a pas été observé 404 sur city-saint-blaise-ne (BUG A
    # cramait le budget avant qu'ImmoScout soit appelé), mais la raison
    # sous-jacente est portail-agnostique : pas d'autre Saint-Blaise en CH.
    # Si IS24 nécessitait vraiment le suffixe, on le remettra ciblé.
    if canton and slug in ('colombier', 'hauterive'):
        slug = f"{slug}-{canton.lower()}"

    consecutive_errors = 0
    consecutive_empty = 0   # v6.4.2 BUG A : pages HTTP 200 avec 0 listings
    for page in range(1, max_pages + 1):
        # ImmoScout24 switched to English URL paths (mid-2025)
        url = f"https://www.immoscout24.ch/en/real-estate/{tx}/city-{slug}?pn={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            log.warning(f"[ImmoScout24] Page {page}: HTTP {status}")
            consecutive_errors += 1
            if consecutive_empty + consecutive_errors >= 2:
                log.warning(f"[ImmoScout24] stopping pagination: {consecutive_empty} empty + {consecutive_errors} errors consecutive")
                break
            continue
        consecutive_errors = 0

        # v6.4.2 BUG A : capture len(results) avant parsing pour détecter les
        # pages HTTP 200 sans yield (JSON parse fail + HTML cards vides).
        results_before_page = len(results)

        found_structured = False

        # Method 1: Try __INITIAL_STATE__ (Vue.js/Nuxt — NOT Next.js)
        # Strategy: find the assignment, then extract JSON by brace-counting (regex is fragile)
        is_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', html)
        if is_match and not found_structured:
            try:
                json_start = html.index('{', max(0, is_match.end() - 1))
            except ValueError:
                json_start = -1
            raw = ''
            if json_start >= 0:
                # Brace-counting to find matching closing brace
                depth = 0
                in_str = False
                escape = False
                for i in range(json_start, min(json_start + 5_000_000, len(html))):
                    c = html[i]
                    if escape:
                        escape = False
                        continue
                    if c == '\\' and in_str:
                        escape = True
                        continue
                    if c == '"' and not escape:
                        in_str = not in_str
                        continue
                    if not in_str:
                        if c == '{':
                            depth += 1
                        elif c == '}':
                            depth -= 1
                            if depth == 0:
                                raw = html[json_start:i+1]
                                break
            if raw:
                try:
                    # Replace JS 'undefined' only outside of quoted strings
                    raw = re.sub(r'(?<!["\w])undefined(?!["\w])', 'null', raw)
                    state = json.loads(raw)
                    # Log top-level keys for debugging structure changes
                    log.info(f"[ImmoScout24] __INITIAL_STATE__ keys: {list(state.keys())[:10]}")

                    # Navigate various possible structures
                    items = []
                    # Try known paths (ImmoScout24 changes these)
                    for path_fn in [
                        lambda s: s.get('resultList', {}).get('search', {}).get('fullSearch', {}).get('result', {}).get('listings', []),
                        lambda s: s.get('resultList', {}).get('search', {}).get('items', []),
                        lambda s: s.get('pages', {}).get('searchResult', {}).get('listings', []),
                        lambda s: s.get('listings', []),
                        lambda s: s.get('searchResult', {}).get('listings', []),
                        lambda s: s.get('search', {}).get('listings', []),
                    ]:
                        try:
                            items = path_fn(state) or []
                            if items:
                                break
                        except (AttributeError, TypeError):
                            continue

                    # Deep search: recursively find any list of dicts with 'id' and 'title'/'price'
                    if not items:
                        def _find_listings(obj, depth=0):
                            if depth > 5:
                                return []
                            if isinstance(obj, list) and len(obj) > 2:
                                # Require 'id' AND at least one of 'title'/'price' to avoid matching breadcrumbs/menus
                                if all(isinstance(i, dict) and 'id' in i and ('title' in i or 'price' in i or 'prices' in i) for i in obj[:3]):
                                    return obj
                            if isinstance(obj, dict):
                                for v in obj.values():
                                    found = _find_listings(v, depth + 1)
                                    if found:
                                        return found
                            return []
                        items = _find_listings(state)
                        if items:
                            log.info(f"[ImmoScout24] Found {len(items)} items via deep search")

                    if items:
                        found_structured = True
                        log.info(f"[ImmoScout24] Page {page}: {len(items)} items via __INITIAL_STATE__")
                        for item in items:
                            listing = item.get('listing', item)
                            lid = listing.get('id', item.get('id', ''))
                            if not lid:
                                continue

                            # Extract full URL if available (ImmoScout needs slug + ID)
                            seo_info = listing.get('seoInformation', {}) or item.get('seoInformation', {}) or {}
                            listing_url = (
                                seo_info.get('listingUrl') or
                                seo_info.get('url') or
                                seo_info.get('seoUrl') or
                                seo_info.get('link') or
                                listing.get('url') or
                                listing.get('listingUrl') or
                                listing.get('detailUrl') or
                                item.get('url') or
                                item.get('detailUrl') or
                                ''
                            )
                            if listing_url and listing_url.startswith('/'):
                                listing_url = 'https://www.immoscout24.ch' + listing_url
                            if not listing_url or 'immoscout24' not in listing_url:
                                # No SEO URL available. Use /fr/d/{id} — IS24 redirects
                                # this to the canonical slug URL when the ID is valid.
                                # Previously fell back to /en/real-estate/.../city-{slug}
                                # which is a SEARCH page (bad UX — user had to find the
                                # listing again manually).
                                listing_url = f"https://www.immoscout24.ch/fr/d/{lid}"
                                log.debug(f"[ImmoScout24] No seoUrl for listing {lid}, using /fr/d/{lid}")

                            # Price — check multiple known field names
                            addr = listing.get('address', {}) or {}
                            if isinstance(addr, str):
                                addr = {'formatted': addr}
                            chars = listing.get('characteristics', {}) or {}
                            prices = listing.get('prices', {}) or {}
                            if isinstance(prices, dict):
                                rent = prices.get('rent')
                                buy = prices.get('buy')
                                price_val = ((rent.get('gross') if isinstance(rent, dict) else rent) or
                                            (buy.get('price') if isinstance(buy, dict) else buy) or
                                            prices.get('value'))
                            else:
                                price_val = prices
                            if not price_val:
                                price_val = listing.get('price') or listing.get('priceFormatted') or item.get('price')

                            # Rooms & surface — safe conversion
                            rooms_raw = chars.get('numberOfRooms') or listing.get('numberOfRooms') or item.get('rooms')
                            surface_raw = chars.get('livingSpace') or chars.get('surfaceLiving') or listing.get('surfaceLiving') or item.get('surface')

                            # Images.
                            # ImmoScout24's payload shape for image entries has
                            # shifted a few times (sometimes {url}, sometimes
                            # {src}, sometimes {originalUrl}/{imageUrl}/…).
                            # When we only looked at 'url'/'src' we silently
                            # produced empty strings for dicts with any other
                            # schema, which then leaked into the DB and made
                            # the frontend show "Pas d'image". Try all known
                            # keys and hard-filter to valid http URLs.
                            _IMG_KEYS = ('url', 'src', 'originalUrl',
                                         'imageUrl', 'fullSizeUrl',
                                         'thumbUrl', 'uri')
                            img_list = []
                            for img in (listing.get('images', []) or item.get('images', []) or []):
                                src = None
                                if isinstance(img, dict):
                                    for k in _IMG_KEYS:
                                        v = img.get(k)
                                        if v:
                                            src = v
                                            break
                                elif isinstance(img, str):
                                    src = img
                                if src and isinstance(src, str) and src.startswith('http') and src not in img_list:
                                    img_list.append(src)

                            # Address
                            addr_str = ''
                            if isinstance(addr, dict):
                                addr_str = f"{addr.get('street', '')} {addr.get('postalCode', '')} {addr.get('locality', '')}".strip()
                                if not addr_str:
                                    addr_str = addr.get('formatted', '')

                            results.append(_make_property(
                                external_id=f"is24-{lid}", source='ImmoScout24',
                                source_url=listing_url,
                                title=listing.get('title', item.get('title', '')),
                                description=listing.get('description', '')[:1500],
                                property_type=_guess_type(listing.get('propertyType', listing.get('title', ''))),
                                transaction=transaction,
                                price=_clean_price(price_val),
                                rooms=_clean_rooms(str(rooms_raw)) if rooms_raw else None,
                                surface=int(float(surface_raw)) if surface_raw and _is_numeric(surface_raw) else _clean_surface(str(surface_raw or '')),
                                floor=chars.get('floor') or listing.get('floor'),
                                address=addr_str,
                                city=(addr.get('locality') if isinstance(addr, dict) else None) or city,
                                canton=(addr.get('region') if isinstance(addr, dict) else None) or canton,
                                postal_code=str(addr.get('postalCode', '')) if isinstance(addr, dict) and addr.get('postalCode') else _extract_postal(addr_str),
                                latitude=listing.get('latitude') or (addr.get('latitude') if isinstance(addr, dict) else None),
                                longitude=listing.get('longitude') or (addr.get('longitude') if isinstance(addr, dict) else None),
                                features=[], images=img_list[:5],
                                published_at=listing.get('publishDate') or listing.get('created'),
                                                            search_city=city,
))
                    else:
                        log.warning(f"[ImmoScout24] __INITIAL_STATE__ parsed but no listings found. Top keys: {list(state.keys())}")
                except json.JSONDecodeError as e:
                    log.error(f"[ImmoScout24] __INITIAL_STATE__ JSON error: {e}")
                except Exception as e:
                    log.error(f"[ImmoScout24] __INITIAL_STATE__ parse error: {e}")

        # Method 3: HTML card-based fallback with broad selectors
        if not found_structured:
            soup = BeautifulSoup(html, 'html.parser')
            # Try multiple card selectors (ImmoScout24 changes classes frequently)
            cards = (soup.select('[class*="ResultList"] [class*="listItem"]') or
                     soup.select('[class*="result"] article') or
                     soup.select('article[class*="card"]') or
                     soup.select('[data-test*="result"] > div') or
                     soup.select('a[href*="/en/d/"], a[href*="/fr/d/"]'))
            log.info(f"[ImmoScout24] Page {page}: {len(cards)} cards via HTML fallback")

            for card in cards:
                try:
                    # Find link
                    link_el = card if card.name == 'a' else card.select_one('a[href*="/d/"]') or card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.immoscout24.ch' + href
                    # ID can be numeric (/d/12345) or slug-based (/d/apartment-name)
                    eid = re.search(r'/d/(\d+)', href)
                    if eid:
                        lid = eid.group(1)
                    else:
                        # Use slug or hash as ID for slug-based URLs
                        slug_match = re.search(r'/d/([^/?]+)', href)
                        lid = slug_match.group(1) if slug_match else ''
                        if not lid:
                            # Fallback: hash the href
                            lid = hashlib.sha256(href.encode()).hexdigest()[:12]
                    if not lid:
                        continue

                    card_text = card.get_text(' ', strip=True)

                    # Price
                    price = None
                    price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'',.\u2019\u00a0\s]+)", card_text)
                    if not price_match:
                        price_match = re.search(r"([\d'',.\u2019\u00a0]+)\s*(?:CHF|Fr|\.–|/mois)", card_text)
                    if price_match:
                        price = _clean_price(price_match.group(1))

                    # Rooms
                    rooms = None
                    rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pi[èe]ce|room|Zimmer|pi\.)', card_text, re.IGNORECASE)
                    if rooms_match:
                        rooms = _clean_rooms(rooms_match.group(0))

                    # Surface
                    surface = None
                    surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                    if surface_match:
                        surface = int(surface_match.group(1))

                    # Title
                    title = ''
                    for el in card.select('h2, h3, [class*="title"], [class*="Title"]'):
                        t = el.get_text(strip=True)
                        if t and len(t) > 3:
                            title = t
                            break
                    if not title:
                        title = f"{rooms or '?'} pièces" + (f", {surface} m²" if surface else '')

                    # Images
                    images = []
                    for img_el in card.select('img[src], img[data-src]'):
                        src = img_el.get('src', '') or img_el.get('data-src', '')
                        if src and src.startswith('http') and 'logo' not in src.lower() and 'icon' not in src.lower():
                            images.append(src)
                    images = list(dict.fromkeys(images))[:5]

                    # Extract address from card
                    addr_text = ''
                    addr_el = card.select_one('[class*="address"], [class*="location"], [class*="Address"]')
                    if addr_el:
                        addr_text = addr_el.get_text(strip=True)
                    if not addr_text:
                        addr_match = re.search(r'(\d{4}\s+\w[\w\s-]+)', card_text)
                        if addr_match:
                            addr_text = addr_match.group(1).strip()

                    results.append(_make_property(
                        external_id=f"is24-{lid}", source='ImmoScout24',
                        source_url=href, title=title, description='',
                        property_type=_guess_type(card_text), transaction=transaction,
                        price=price, rooms=rooms, surface=surface, floor=None,
                        address=addr_text, city=city, canton=canton,
                        postal_code=_extract_postal(addr_text), latitude=None, longitude=None,
                        features=[], images=images, published_at=None,
                                            search_city=city,
))
                except Exception as e:
                    log.debug(f"[ImmoScout24] Card parse error: {e}")

            if not cards:
                log.info(f"[ImmoScout24] Page {page}: No structured data found, HTML size: {len(html)}")

        # v6.4.2 BUG A : empty-page detection. Même logique que Homegate —
        # HTTP 200 + 0 listings extraits = fin de pagination. Page 1 vide →
        # break immédiat (ville inconnue du portail). Sinon break dès que
        # consecutive_empty + consecutive_errors >= 2.
        if len(results) == results_before_page:
            consecutive_empty += 1
            if page == 1:
                log.info(f"[ImmoScout24] Page 1 empty → ville sans résultat sur ce portail, pagination stoppée")
                break
            if consecutive_empty + consecutive_errors >= 2:
                log.info(f"[ImmoScout24] stopping pagination: {consecutive_empty} empty + {consecutive_errors} errors consecutive")
                break
        else:
            consecutive_empty = 0

        time.sleep(1)

    log.info(f"[ImmoScout24] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOBILIER.CH — via ScrapingBee
# ============================================================


# Immobilier.ch canton code → URL slug mapping
_IMCH_CANTON_SLUGS = {
    'NE': 'neuchatel', 'VD': 'vaud', 'GE': 'geneve', 'VS': 'valais',
    'FR': 'fribourg', 'BE': 'berne', 'JU': 'jura', 'ZH': 'zurich',
    'BS': 'bale', 'LU': 'lucerne', 'TI': 'tessin', 'SG': 'saint-gall',
    'AG': 'argovie', 'SO': 'soleure', 'BL': 'bale-campagne',
}
# Cities with non-obvious slugs on immobilier.ch
_IMCH_CITY_SLUGS = {
    'la chaux-de-fonds': 'chaux-fonds',
    'colombier': 'colombier-ne',
    'hauterive': 'hauterive-ne',
    'corcelles-cormondrèche': 'corcelles-ne',
    'saint-blaise': 'st-blaise',
    'val-de-travers': 'val-de-travers',
    'val-de-ruz': 'val-de-ruz',
    'marin-epagnier': 'marin-epagnier',
    'yverdon-les-bains': 'yverdon',
    'la tène': 'la-tene',
    'le landeron': 'le-landeron',
    'le locle': 'le-locle',
}


def scrape_immobilier_ch(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Immobilier.ch] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    canton = CITY_CANTONS.get(city.lower(), '')
    canton_slug = _IMCH_CANTON_SLUGS.get(canton, '')

    # Determine city slug — check special cases first, then normalize
    city_slug = _IMCH_CITY_SLUGS.get(city.lower(), '')
    if not city_slug:
        city_slug = _normalize_city(city).replace(' ', '-')

    consecutive_errors = 0
    for page in range(1, max_pages + 1):
        # immobilier.ch uses /canton-slug/city-slug for city-level searches
        # and just /canton-slug for canton-level searches
        urls = []
        if canton_slug:
            urls.append(f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{canton_slug}/{city_slug}?page={page}")
            # Fallback: canton-level (catches small towns not indexed individually)
            if page == 1:
                urls.append(f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{canton_slug}?page={page}")
        # Last resort: direct slug (works when city = canton name)
        urls.append(f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{city_slug}?page={page}")

        html = ''
        status = 0
        for try_url in urls:
            # Try direct first (0 credits) — immobilier.ch is SSR-friendly
            status, html = _smart_get(try_url, render_js=True, try_direct=True)
            if status == 200 and len(html) > 5000:
                # Check if the page actually has results (data-count > 0)
                count_match = re.search(r'data-count="(\d+)"', html)
                if count_match and int(count_match.group(1)) > 0:
                    log.info(f"[Immobilier.ch] URL worked: {try_url[:80]}... (count={count_match.group(1)})")
                    break
                elif not count_match:
                    break  # No count attribute, try parsing anyway
                else:
                    log.info(f"[Immobilier.ch] URL returned 0 results: {try_url[:80]}...")
                    continue  # Try next URL pattern
            status = 0  # Reset to trigger next URL

        if status != 200:
            consecutive_errors += 1
            if consecutive_errors >= 2:
                log.warning(f"[Immobilier.ch] 2 consecutive errors, stopping pagination")
                break
            continue
        consecutive_errors = 0

        soup = BeautifulSoup(html, 'html.parser')

        # Method 1: HTML cards with div.filter-item[data-id] — primary approach
        # immobilier.ch uses filter-item cards with data-id and data-latlng attributes
        cards = soup.select('div.filter-item[data-id]')
        if not cards:
            # Fallback to broader selectors
            tx_slug = 'acheter' if transaction == 'achat' else 'louer'
            cards = soup.select(f'a[href*="/fr/{tx_slug}/"]')
            if not cards:
                cards = (soup.select('[class*="property"]') or
                         soup.select('[class*="listing"]') or
                         soup.select('article'))
        log.info(f"[Immobilier.ch] Page {page}: {len(cards)} cards")

        for card in cards:
            try:
                # ID from data-id attribute
                data_id = card.get('data-id', '')

                # Lat/Lng from data-latlng attribute
                latlng = card.get('data-latlng', '')
                lat, lng = None, None
                if latlng and ',' in latlng:
                    parts = latlng.split(',')
                    try:
                        lat = float(parts[0].strip())
                        lng = float(parts[1].strip())
                    except (ValueError, IndexError):
                        pass

                # Link: a#link-result-item-{ID} or first <a> with href
                link_el = card.select_one(f'a#link-result-item-{data_id}') if data_id else None
                if not link_el:
                    link_el = card.select_one('a[href*="/fr/"]')
                if not link_el:
                    link_el = card if card.name == 'a' else card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.immobilier.ch' + href
                # Extract ID from URL if not from data-id
                if not data_id:
                    eid_m = re.search(r'[-/](\d{5,})', href)
                    data_id = eid_m.group(1) if eid_m else ''
                if not data_id and not href:
                    continue

                # Price: strong.title contains "CHF 1'090.-/mois (+190.- charges)"
                price = None
                price_el = card.select_one('strong.title')
                if price_el:
                    price = _clean_price(price_el.get_text(strip=True))
                if not price:
                    card_text = card.get_text(' ', strip=True)
                    price_match = re.search(r"(?:CHF)\s*([\d'',.\u2019\u00a0\s]+)", card_text)
                    if price_match:
                        price = _clean_price(price_match.group(1))

                # Type + rooms: p.object-type contains "Appartement 3 pièces"
                type_el = card.select_one('p.object-type')
                type_text = type_el.get_text(strip=True) if type_el else ''
                rooms = _clean_rooms(type_text)
                prop_type = _guess_type(type_text)

                # Address: 3rd <p> in .filter-item-content (no class) — "Peseux, Rue de Corcelles"
                address = ''
                content_el = card.select_one('.filter-item-content')
                if content_el:
                    paragraphs = content_el.select('p')
                    # Address is typically the last <p> without a specific class like object-type
                    for p in paragraphs:
                        if 'object-type' not in (p.get('class') or []):
                            addr_text = p.get_text(strip=True)
                            if addr_text and not re.match(r'^(?:CHF\s*)?[\d\s\'\',.]+', addr_text):
                                address = addr_text

                # Title: use type_text + address as descriptive title
                title = type_text
                if not title:
                    title_el = card.select_one('h2, h3, strong')
                    title = title_el.get_text(strip=True) if title_el else ''

                # Surface: span.space inside .characteristic-list
                surface = None
                space_el = card.select_one('span.space')
                if space_el:
                    surface = _clean_surface(space_el.get_text(strip=True))
                if not surface:
                    # Fallback: search card text for surface
                    card_text = card.get_text(' ', strip=True)
                    surf_match = re.search(r'(\d+)\s*m[²2]', card_text)
                    if surf_match:
                        surface = int(surf_match.group(1))

                # Rooms from characteristics if not found in type
                if not rooms:
                    plan_el = card.select_one('i.icon-plan')
                    if plan_el and plan_el.next_sibling:
                        rooms = _clean_rooms(str(plan_el.next_sibling))

                # Images: lazy-loaded with data-src inside .filter-carousel
                images = []
                for img_el in card.select('.filter-carousel img[data-src]'):
                    src = img_el.get('data-src', '')
                    if src and '/Medias/' in src:
                        if src.startswith('/'):
                            src = 'https://www.immobilier.ch' + src
                        images.append(src)
                # Also check regular img src
                if not images:
                    for img_el in card.select('img[src]'):
                        src = img_el.get('src', '')
                        if src and src.startswith('http') and 'logo' not in src.lower() and 'pixel' not in src.lower():
                            images.append(src)
                images = list(dict.fromkeys(images))[:5]

                # Extract city from address ("Peseux, Rue de Corcelles" → "Peseux")
                card_city = city
                if address and ',' in address:
                    card_city = address.split(',')[0].strip()

                if title or price:
                    results.append(_make_property(
                        external_id=f"imch-{data_id or hashlib.sha256(href.encode()).hexdigest()[:12]}",
                        source='Immobilier.ch', source_url=href,
                        title=title, description='',
                        property_type=prop_type, transaction=transaction,
                        price=price, rooms=rooms,
                        surface=surface, floor=None,
                        address=address, city=card_city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=lat, longitude=lng,
                        features=[], images=images, published_at=None,
                                            search_city=city,
))
            except Exception as e:
                log.debug(f"[Immobilier.ch] Card parse error: {e}")

        time.sleep(1)

    log.info(f"[Immobilier.ch] Total: {len(results)} listings")
    return results


# ============================================================
# ANIBIS — via ScrapingBee
# ============================================================

def scrape_anibis(city="Lausanne", transaction="location", max_pages=2):
    """Scrape Anibis.ch via SSR HTML + __NEXT_DATA__ (Next.js app).
    The GraphQL API at c.anibis.ch is behind Cloudflare and returns 403.
    Instead, we fetch the SSR-rendered real estate page and parse __NEXT_DATA__
    which contains the dehydrated React Query state with listings."""
    log.info(f"[Anibis] Searching {city} ({transaction})")
    results = []
    city_norm = _normalize_city(city)
    canton = CITY_CANTONS.get(city.lower(), '').upper()

    # Anibis real estate pages — these are SSR with __NEXT_DATA__
    # /fr/immobilien is the main real estate page (all listings)
    # The search is done via opaque search tokens, but the landing page has listings
    urls_to_try = [
        'https://www.anibis.ch/fr/immobilien',  # Main real estate page with ~18K rental listings
    ]

    for url in urls_to_try:
        status, html = _sb_get(url, render_js=True)
        if status != 200 or len(html) < 5000:
            log.warning(f"[Anibis] {url[:60]}... → HTTP {status}")
            continue

        soup = BeautifulSoup(html, 'html.parser')

        # Parse __NEXT_DATA__ — contains dehydrated React Query state
        nd = soup.select_one('script#__NEXT_DATA__')
        if not nd:
            log.warning("[Anibis] No __NEXT_DATA__ found")
            continue

        try:
            ndata = json.loads(nd.string or '{}')
            page_props = ndata.get('props', {}).get('pageProps', {})

            # Navigate dehydratedState → queries → state.data for listings
            dehydrated = page_props.get('dehydratedState', {})
            queries = dehydrated.get('queries', [])
            all_items = []

            for q in queries:
                state_data = q.get('state', {}).get('data', {})
                # Check multiple possible locations for listings
                for key in ['listings', 'searchListingsByConstraints', 'edges', 'nodes', 'items']:
                    items = state_data.get(key, [])
                    if items:
                        # Handle edges/node pattern
                        if items and isinstance(items[0], dict) and 'node' in items[0]:
                            items = [e['node'] for e in items if 'node' in e]
                        all_items.extend(items)
                        break

                # Also check nested search results
                search_results = state_data.get('searchListingsByConstraints', {})
                if isinstance(search_results, dict):
                    edges = search_results.get('edges', [])
                    for e in edges:
                        if 'node' in e:
                            all_items.append(e['node'])

            log.info(f"[Anibis] Found {len(all_items)} items in __NEXT_DATA__")

            for item in all_items:
                lid = str(item.get('listingID', '') or item.get('id', ''))
                if not lid:
                    continue

                title = item.get('title', '')

                # Price
                price = _clean_price(item.get('formattedPrice') or item.get('price'))

                # Location info
                postcode_info = item.get('postcodeInformation', {}) or {}
                loc_name = postcode_info.get('locationName', '')
                postcode = postcode_info.get('postcode', '')
                item_canton = (postcode_info.get('canton', {}) or {}).get('shortName', '')

                # Filter by canton if specified (Anibis returns all of Switzerland)
                if canton and item_canton and item_canton != canton:
                    continue

                # Filter by city (fuzzy match on location name)
                if loc_name and city_norm:
                    loc_norm = _normalize_city(loc_name)
                    if city_norm not in loc_norm and loc_norm not in city_norm:
                        # Allow same postal code area
                        if not postcode:
                            continue

                # Images
                images = []
                thumb = item.get('thumbnail', {}) or {}
                retina = (thumb.get('retinaRendition') or {}).get('src', '')
                normal = (thumb.get('normalRendition') or {}).get('src', '')
                if retina:
                    images.append(retina)
                elif normal:
                    images.append(normal)
                # Additional images from imageUrls if present
                for img in (item.get('images') or item.get('imageUrls') or []):
                    if isinstance(img, dict):
                        src = (img.get('retinaRendition') or {}).get('src', '') or (img.get('normalRendition') or {}).get('src', '')
                    elif isinstance(img, str):
                        src = img
                    else:
                        continue
                    if src and src not in images:
                        images.append(src)
                images = [u for u in images if u and u.startswith('http')][:5]

                # SEO slug for detail URL
                seo_info = item.get('seoInformation', {}) or {}
                fr_slug = seo_info.get('frSlug', '')
                detail_url = f"https://www.anibis.ch/fr/vi/{fr_slug}/{lid}" if fr_slug else f"https://www.anibis.ch/fr/vi/{lid}"

                # Rooms and surface from properties/attributes
                rooms = None
                surface = None
                for prop in (item.get('properties') or item.get('attributes') or []):
                    prop_id = (prop.get('listingPropertyID') or prop.get('name') or '').lower()
                    prop_text = prop.get('text', '') or prop.get('value', '')
                    if 'room' in prop_id or 'piece' in prop_id:
                        rooms = _clean_rooms(str(prop_text))
                    elif 'size' in prop_id or 'surface' in prop_id or 'area' in prop_id:
                        surface = _clean_surface(str(prop_text))

                if not rooms:
                    rooms = _clean_rooms(title)

                results.append(_make_property(
                    external_id=f"anibis-{lid}", source='Anibis',
                    source_url=detail_url,
                    title=title, description=(item.get('body') or '')[:1500],
                    property_type=_guess_type(title),
                    transaction=transaction,
                    price=price, rooms=rooms, surface=surface, floor=None,
                    address=loc_name or city,
                    city=loc_name or city,
                    canton=item_canton or canton or CITY_CANTONS.get(city.lower(), ''),
                    postal_code=str(postcode) if postcode else None,
                    latitude=None, longitude=None,
                    features=[], images=images, published_at=item.get('timestamp'),
                                    search_city=city,
))

        except Exception as e:
            log.error(f"[Anibis] __NEXT_DATA__ parse error: {e}")

        if results:
            break  # Got results from this URL, no need to try more

    log.info(f"[Anibis] Total: {len(results)} listings")
    return results


# ============================================================
# ACHETER-LOUER — via ScrapingBee
# ============================================================

def scrape_acheter_louer(city="Lausanne", transaction="location", max_pages=1):
    """Scrape acheter-louer.ch. Site loads all results on one page (no pagination).
    Uses div.vignette cards with Bootstrap 3 layout. Behind Cloudflare — needs ScrapingBee."""
    log.info(f"[Acheter-Louer] Searching {city} ({transaction})")
    results = []
    slug = _normalize_city(city).replace(' ', '-')

    if transaction == 'achat':
        url_patterns = [
            f"https://www.acheter-louer.ch/acheter/{slug}-appartements-a-vendre.html",
            f"https://www.acheter-louer.ch/acheter/{slug}-maisons-a-vendre.html",
        ]
    else:
        url_patterns = [
            f"https://www.acheter-louer.ch/louer/{slug}-appartements-a-louer.html",
            f"https://www.acheter-louer.ch/louer/{slug}-maisons-a-louer.html",
        ]

    for url in url_patterns:
        # Try direct first (0 credits) — SSR-friendly, Cloudflare sometimes lets through
        status, html = _smart_get(url, render_js=True, try_direct=True)
        if status != 200 or len(html) < 2000:
            continue

        soup = BeautifulSoup(html, 'html.parser')

        # Acheter-Louer uses div.vignette cards (default, gold, star variants)
        cards = soup.select('div.vignette')
        if not cards:
            # Broader fallback
            cards = soup.select('[class*="vignette"]')
        log.info(f"[Acheter-Louer] {url.split('/')[-1]}: {len(cards)} vignette cards")

        for card in cards:
            try:
                # Property ID from favorite button
                fav_el = card.select_one('[data-idobj]')
                data_id = fav_el.get('data-idobj', '') if fav_el else ''

                # Detail link from image wrapper
                link_el = card.select_one('div.imgObj a[href]') or card.select_one('a[href*="/fr/"]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.acheter-louer.ch' + href
                if not href and not data_id:
                    continue

                # Price: div.price > span (e.g., "2'350.--")
                price = None
                price_el = card.select_one('div.price span')
                if price_el:
                    price = _clean_price(price_el.get_text(strip=True))
                # Check for "Prix sur demande"
                if not price and card.select_one('span.no-price'):
                    price = None  # Explicit: no price available

                # Rooms: td.rooms > first span (e.g., "3" or "4.5")
                rooms = None
                rooms_el = card.select_one('td.rooms span')
                if rooms_el:
                    rooms = _clean_rooms(rooms_el.get_text(strip=True))

                # Surface: td.surface > first span (e.g., "71")
                surface = None
                surface_el = card.select_one('td.surface span')
                if surface_el:
                    surface = _clean_surface(surface_el.get_text(strip=True))

                # Title: h2.vign-title contains type + city + address
                title = ''
                title_el = card.select_one('h2.vign-title')
                if title_el:
                    title = title_el.get_text(' ', strip=True)

                # Description: div.vign-desc
                desc = ''
                desc_el = card.select_one('div.vign-desc')
                if desc_el:
                    desc = desc_el.get_text(' ', strip=True)[:300]

                # Image: main listing image
                img_el = card.select_one('div.imgObj img[src]')
                images = []
                if img_el:
                    src = img_el.get('src', '')
                    if src and src.startswith('http') and 'logo' not in src.lower():
                        images.append(src)

                # Extract city from title (e.g., "Appartement à louer\n1092 BELMONT-SUR-LAUSANNE")
                card_city = city
                postal = None
                if title:
                    npa_match = re.search(r'(\d{4})\s+([A-ZÀ-Ü][\w-]+(?:\s+[\w-]+)*)', title)
                    if npa_match:
                        postal = npa_match.group(1)
                        card_city = npa_match.group(2).strip().title()

                # Address from span inside title
                address = ''
                addr_span = card.select_one('h2.vign-title span.vign-title')
                if addr_span:
                    address = addr_span.get_text(strip=True).strip(' -–')

                if title or price:
                    eid = f"al-{data_id}" if data_id else f"al-{hashlib.sha256(href.encode()).hexdigest()[:12]}"
                    results.append(_make_property(
                        external_id=eid,
                        source='Acheter-Louer', source_url=href,
                        title=title, description=desc,
                        property_type=_guess_type(title or desc), transaction=transaction,
                        price=price, rooms=rooms,
                        surface=surface, floor=None,
                        address=address or card_city, city=card_city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=postal or _extract_postal(title),
                        latitude=None, longitude=None,
                        features=[], images=images, published_at=None,
                                            search_city=city,
))
            except Exception as e:
                log.debug(f"[Acheter-Louer] Card parse error: {e}")

        time.sleep(1)

    log.info(f"[Acheter-Louer] Total: {len(results)} listings")
    return results


# ============================================================
# FLATFOX — Direct API with bounding box geo-search
# ============================================================

# Bounding boxes (south, west, north, east) for Swiss cities — ~5km radius around center
CITY_BBOXES = {
    'lausanne':             (46.505, 6.585, 46.555, 6.680),
    'geneve':               (46.170, 6.100, 46.230, 6.200),
    'genève':               (46.170, 6.100, 46.230, 6.200),
    'neuchatel':            (46.975, 6.890, 47.020, 6.960),
    'neuchâtel':            (46.975, 6.890, 47.020, 6.960),
    'fribourg':             (46.780, 7.130, 46.820, 7.180),
    'sion':                 (46.220, 7.330, 46.250, 7.400),
    'montreux':             (46.420, 6.890, 46.460, 6.940),
    'nyon':                 (46.370, 6.220, 46.400, 6.260),
    'morges':               (46.500, 6.480, 46.520, 6.520),
    'yverdon':              (46.760, 6.620, 46.790, 6.660),
    'yverdon-les-bains':    (46.760, 6.620, 46.790, 6.660),
    'la chaux-de-fonds':    (47.085, 6.790, 47.120, 6.850),
    'bienne':               (47.120, 7.220, 47.160, 7.280),
    'biel':                 (47.120, 7.220, 47.160, 7.280),
    'delemont':             (47.350, 7.330, 47.370, 7.370),
    'delémont':             (47.350, 7.330, 47.370, 7.370),
    'berne':                (46.930, 7.410, 46.970, 7.480),
    'bern':                 (46.930, 7.410, 46.970, 7.480),
    'vevey':                (46.450, 6.830, 46.470, 6.870),
    'renens':               (46.525, 6.570, 46.545, 6.600),
    'zurich':               (47.340, 8.480, 47.410, 8.580),
    'zürich':               (47.340, 8.480, 47.410, 8.580),
    'basel':                (47.530, 7.560, 47.580, 7.620),
    'bâle':                 (47.530, 7.560, 47.580, 7.620),
    'lugano':               (45.990, 8.920, 46.020, 8.970),
    'lucerne':              (47.030, 8.270, 47.070, 8.330),
    'luzern':               (47.030, 8.270, 47.070, 8.330),
    'winterthur':           (47.480, 8.700, 47.520, 8.760),
    'st. gallen':           (47.410, 9.350, 47.440, 9.410),
    'carouge':              (46.175, 6.130, 46.195, 6.155),
    'meyrin':               (46.220, 6.065, 46.240, 6.095),
    'prilly':               (46.525, 6.590, 46.540, 6.610),
    'pully':                (46.505, 6.650, 46.530, 6.680),
    'ecublens':             (46.520, 6.540, 46.540, 6.570),
    'sierre':               (46.280, 7.510, 46.310, 7.560),
    'martigny':             (46.090, 7.060, 46.120, 7.090),
    'colombier':            (46.960, 6.850, 46.980, 6.880),
    'peseux':               (46.980, 6.850, 46.995, 6.870),
    'boudry':               (46.940, 6.820, 46.960, 6.860),
    'cortaillod':           (46.935, 6.830, 46.950, 6.860),
    'marin-epagnier':       (47.000, 6.970, 47.020, 7.000),
    'hauterive':            (46.990, 6.930, 47.005, 6.950),
    'saint-blaise':         (47.005, 6.970, 47.020, 6.995),
    'le locle':             (47.050, 6.730, 47.070, 6.760),
    'val-de-travers':       (46.900, 6.570, 46.930, 6.640),
    'fleurier':             (46.895, 6.570, 46.920, 6.600),
    'milvignes':            (46.990, 6.900, 47.010, 6.930),
    'la tène':              (47.000, 6.970, 47.015, 6.995),
    'le landeron':          (47.050, 7.060, 47.070, 7.090),
    'bevaix':               (46.925, 6.810, 46.945, 6.830),
    'val-de-ruz':           (47.030, 6.890, 47.060, 6.930),
    'corcelles-cormondrèche': (46.970, 6.860, 46.990, 6.890),
}

def scrape_flatfox(city="Lausanne", transaction="location", limit=50):
    log.info(f"[Flatfox] Searching {city} ({transaction})")
    results = []
    offer_type = 'RENT' if transaction == 'location' else 'SELL'
    city_norm = _normalize_city(city)

    headers = {
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    }

    bbox = CITY_BBOXES.get(city.lower())
    if not bbox:
        # Try normalized name
        bbox = CITY_BBOXES.get(city_norm)
    if not bbox:
        log.warning(f"[Flatfox] No bounding box for {city}, skipping")
        return results

    south, west, north, east = bbox

    # Step 1: Get all listing PKs via the pin (geo-search) API
    pin_url = "https://flatfox.ch/api/v1/pin/"
    try:
        r = requests.get(pin_url, params={
            'north': north,
            'east': east,
            'south': south,
            'west': west,
            'offer_type': offer_type,
            'max_count': 500,
        }, headers=headers, timeout=30)

        if r.status_code != 200:
            log.warning(f"[Flatfox] Pin API → HTTP {r.status_code}")
            return results

        pins = r.json()
        if not isinstance(pins, list):
            pins = pins.get('results', [])

        # Extract PKs from pins
        pks = []
        for pin in pins:
            pk = pin.get('pk') or pin.get('id')
            if pk:
                pks.append(str(pk))

        log.info(f"[Flatfox] Pin API returned {len(pks)} listings in bbox for {city}")

        if not pks:
            return results

    except Exception as e:
        log.error(f"[Flatfox] Pin API error: {e}")
        return results

    # Step 2: Fetch full listing details in batches via public-listing API
    detail_url = "https://flatfox.ch/api/v1/public-listing/"
    batch_size = 50

    for i in range(0, len(pks), batch_size):
        batch = pks[i:i + batch_size]
        try:
            r = requests.get(detail_url, params=[('pk', pk) for pk in batch],
                             headers=headers, timeout=30)

            if r.status_code != 200:
                log.warning(f"[Flatfox] Detail batch {i // batch_size} → HTTP {r.status_code}")
                continue

            data = r.json()
            items = data.get('results', [])
            log.info(f"[Flatfox] Detail batch {i // batch_size}: {len(items)} items")

            for item in items:
                # Double-check city match (pins might include edge cases)
                item_city = _normalize_city(item.get('city') or '')
                if item_city != city_norm:
                    continue
                if item.get('offer_type') != offer_type:
                    continue

                pk = item.get('pk', '')
                slug = item.get('slug', pk)

                if transaction == 'location':
                    price = item.get('rent_gross') or item.get('rent_net') or item.get('price_display')
                else:
                    price = item.get('price_display')

                # Build image URLs (defensive: skip missing/non-http values)
                images = []
                cover = item.get('cover_image')
                if isinstance(cover, dict):
                    img_url = cover.get('url') or cover.get('src') or ''
                    if img_url:
                        if img_url.startswith('/'):
                            img_url = 'https://flatfox.ch' + img_url
                        if img_url.startswith('http'):
                            images.append(img_url)
                # Pick up additional images if API returns a list
                extra = item.get('images') or item.get('gallery') or []
                if isinstance(extra, list):
                    for ex in extra[:4]:
                        u = ''
                        if isinstance(ex, dict):
                            u = ex.get('url') or ex.get('src') or ''
                        elif isinstance(ex, str):
                            u = ex
                        if u and u.startswith('/'):
                            u = 'https://flatfox.ch' + u
                        if u and u.startswith('http') and u not in images:
                            images.append(u)

                # Extract features from attributes
                features = [a.get('name', '') for a in (item.get('attributes') or []) if a.get('name')]

                prop = _make_property(
                    external_id=f"ff-{pk}", source='Flatfox',
                    source_url=f"https://flatfox.ch/fr/flat/{slug}/{pk}/",
                    title=item.get('short_title') or item.get('public_title') or '',
                    description=item.get('description') or '',
                    property_type=_guess_type((item.get('object_type') or '') + ' ' + (item.get('object_category') or '')),
                    transaction=transaction,
                    price=_clean_price(price),
                    rooms=item.get('number_of_rooms'),
                    surface=item.get('surface_living') or item.get('surface_usable'),
                    floor=item.get('floor'),
                    address=item.get('public_address') or ((item.get('street') or '') + ', ' + (item.get('city') or city)),
                    city=item.get('city') or city,
                    canton=item.get('state') or CITY_CANTONS.get(city.lower(), ''),
                    postal_code=str(item.get('zipcode') or '') or None,
                    latitude=item.get('latitude'), longitude=item.get('longitude'),
                    features=features,
                    images=images,
                    published_at=item.get('published') or item.get('created'),
                                    search_city=city,
)
                if prop:
                    results.append(prop)

        except Exception as e:
            log.error(f"[Flatfox] Detail batch error: {e}")

        if i + batch_size < len(pks):
            time.sleep(0.3)

    log.info(f"[Flatfox] Total: {len(results)} listings for {city}")
    return results


# ============================================================
# COMPARIS — via ScrapingBee
# ============================================================

def scrape_comparis(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Comparis] Searching {city} ({transaction})")
    results = []
    canton = CITY_CANTONS.get(city.lower(), '')
    deal_type = 10 if transaction == 'location' else 20

    consecutive_errors = 0
    for page in range(1, max_pages + 1):
        url = f"https://www.comparis.ch/immobilien/result/list?requestobject=%7B%22DealType%22%3A{deal_type}%2C%22Keyword%22%3A%22{quote(city)}%22%2C%22Sort%22%3A4%2C%22Page%22%3A{page}%7D"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            log.warning(f"[Comparis] Page {page}: HTTP {status}")
            consecutive_errors += 1
            if consecutive_errors >= 2:
                log.warning(f"[Comparis] 2 consecutive errors, stopping pagination")
                break
            continue
        consecutive_errors = 0
        found = False

        # Method 1: Try __NEXT_DATA__
        nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd_match:
            try:
                data = json.loads(nd_match.group(1))
                pp = data.get('props', {}).get('pageProps', {})
                items = (pp.get('listings', []) or pp.get('results', []) or
                         pp.get('searchResults', {}).get('items', []) or [])
                if items:
                    found = True
                    log.info(f"[Comparis] {len(items)} items via __NEXT_DATA__")
                    for item in items:
                        lid = item.get('id', '')
                        # Extract images from __NEXT_DATA__
                        img_list = []
                        for img in (item.get('images', []) or item.get('pictures', []) or []):
                            if isinstance(img, dict):
                                img_list.append(img.get('url', img.get('src', '')))
                            elif isinstance(img, str):
                                img_list.append(img)
                        if not img_list and item.get('imageUrl'):
                            img_list.append(item['imageUrl'])
                        if not img_list and item.get('image'):
                            img_list.append(item['image'])
                        img_list = [u for u in img_list if u][:5]

                        item_url = item.get('url', '')
                        if item_url and item_url.startswith('/'):
                            item_url = 'https://www.comparis.ch' + item_url
                        if not item_url:
                            item_url = f"https://www.comparis.ch/immobilien/detail/{lid}"

                        results.append(_make_property(
                            external_id=f"comp-{lid}",
                            source='Comparis',
                            source_url=item_url,
                            title=item.get('title', ''), description='',
                            property_type=_guess_type(item.get('title', '')),
                            transaction=transaction,
                            price=_clean_price(item.get('price') or item.get('priceFormatted')),
                            rooms=_clean_rooms(str(item.get('rooms', '') or item.get('numberOfRooms', ''))),
                            surface=_clean_surface(str(item.get('surface', '') or item.get('livingSpace', ''))),
                            floor=None,
                            address=item.get('address', ''), city=city, canton=canton,
                            postal_code=_extract_postal(item.get('address', '')),
                            latitude=None, longitude=None,
                            features=[], images=img_list, published_at=None,
                                                    search_city=city,
))
            except Exception as e:
                log.error(f"[Comparis] __NEXT_DATA__ parse error: {e}")

        # Method 2: HTML card parsing with broad selectors
        if not found:
            soup = BeautifulSoup(html, 'html.parser')
            # Try many possible card selectors — Comparis uses obfuscated class names
            cards = (soup.select('[class*="ListItem"]') or
                     soup.select('[class*="result-item"]') or
                     soup.select('[class*="listing"]') or
                     soup.select('a[href*="/immobilien/"]') or
                     soup.select('[data-testid*="listing"]') or
                     soup.select('article'))
            # Filter to only cards that contain price-like text
            real_cards = []
            for c in cards:
                txt = c.get_text(' ', strip=True)
                if re.search(r'CHF|Fr\.?\s*\d|\d+\s*(?:pièce|room|Zimmer)', txt, re.IGNORECASE):
                    real_cards.append(c)
            cards = real_cards or cards
            log.info(f"[Comparis] {len(cards)} cards found via HTML")

            for card in cards:
                try:
                    card_text = card.get_text(' ', strip=True)
                    title_el = card.select_one('h3, h2, [class*="title"], [class*="Title"]')
                    title = title_el.get_text(strip=True) if title_el else ''

                    price = None
                    price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'',.\u2019\u00a0\s]+)", card_text)
                    if not price_match:
                        price_match = re.search(r"([\d'',.\u2019\u00a0]+)\s*(?:CHF|Fr|\.–|/mois)", card_text)
                    if price_match:
                        price = _clean_price(price_match.group(1))

                    addr_el = card.select_one('[class*="address"], [class*="location"]')
                    address = addr_el.get_text(strip=True) if addr_el else ''

                    link_el = card if card.name == 'a' else card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.comparis.ch' + href

                    # Images
                    images = []
                    for img_el in card.select('img[src], img[data-src]'):
                        src = img_el.get('src', '') or img_el.get('data-src', '')
                        if src and src.startswith('http') and 'logo' not in src.lower() and 'icon' not in src.lower():
                            images.append(src)
                    images = list(dict.fromkeys(images))[:5]

                    if title or price:
                        results.append(_make_property(
                            external_id=f"comp-{hashlib.sha256((title+address+str(price)).encode()).hexdigest()[:12]}",
                            source='Comparis', source_url=href,
                            title=title, description='',
                            property_type=_guess_type(title), transaction=transaction,
                            price=price, rooms=_clean_rooms(card_text),
                            surface=_clean_surface(card_text), floor=None,
                            address=address, city=city, canton=canton,
                            postal_code=_extract_postal(address),
                            latitude=None, longitude=None,
                            features=[], images=images, published_at=None,
                                                    search_city=city,
))
                except Exception:
                    pass

        time.sleep(1)

    log.info(f"[Comparis] Total: {len(results)} listings")
    return results


# ============================================================
# PROPERSTAR — via ScrapingBee
# ============================================================

def scrape_properstar(city="Lausanne", transaction="location", max_pages=1):
    log.info(f"[Properstar] Searching {city} ({transaction})")
    results = []
    slug = _normalize_city(city).replace(' ', '-')
    tx_fr = "louer" if transaction == "location" else "acheter"

    # Scrape both apartments and houses
    all_html = []
    for prop_type in ['appartement', 'maison']:
        url = f"https://www.properstar.ch/suisse/{slug}/{tx_fr}/{prop_type}"
        # Try direct first (0 credits), fall back to ScrapingBee if blocked
        status, html = _smart_get(url, render_js=True, try_direct=True)
        if status == 200 and len(html) > 2000:
            all_html.append(html)

    # Track existing IDs across both appartement + maison pages to avoid duplicates
    existing_ids = set()

    for html in all_html:
        soup = BeautifulSoup(html, 'html.parser')

        # Method 1: JSON-LD ItemList (only first 5 items but structured)
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string or '{}')
                if ld.get('@type') != 'ItemList':
                    continue
                for item in ld.get('itemListElement', []):
                    obj = item.get('item', item)
                    if obj.get('@type') != 'RealEstateListing':
                        continue
                    obj_url = obj.get('url', '')
                    eid_m = re.search(r'/(\d+)', obj_url)
                    eid = eid_m.group(1) if eid_m else hashlib.sha256(obj_url.encode()).hexdigest()[:12]
                    if f"ps-{eid}" in existing_ids:
                        continue
                    existing_ids.add(f"ps-{eid}")
                    offers = obj.get('offers', {}) or {}
                    entity = obj.get('mainEntity', {}) or {}
                    addr = entity.get('address', {}) or {}
                    results.append(_make_property(
                        external_id=f"ps-{eid}", source='Properstar',
                        source_url=obj_url if obj_url.startswith('http') else f"https://www.properstar.ch{obj_url}",
                        title=obj.get('name', ''), description='',
                        property_type=_guess_type(entity.get('@type', '') + ' ' + obj.get('name', '')),
                        transaction=transaction,
                        price=_clean_price(offers.get('price')),
                        rooms=None, surface=None, floor=None,
                        address=addr.get('addressLocality', city),
                        city=addr.get('addressLocality') or city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None, latitude=None, longitude=None,
                        features=[], images=[], published_at=obj.get('datePosted'),
                                            search_city=city,
))
            except Exception as e:
                log.debug(f"[Properstar] JSON-LD error: {e}")

        # Method 2: HTML cards (article.item-adaptive) — up to 20 per page
        cards = soup.select('article.item-adaptive')
        if not cards:
            # Fallback to broader selectors
            cards = soup.select('article.card-global, article.card-extended')
        if not cards:
            cards = soup.select('.listing-card, .property-card, article')
        log.info(f"[Properstar] {len(cards)} HTML cards found")

        for card in cards:
            try:
                # Link: a.listing-title has the detail href
                link_el = card.select_one('a.listing-title')
                if not link_el:
                    link_el = card.select_one('a[href*="/annonce/"]')
                if not link_el:
                    link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.properstar.ch' + href
                if not href:
                    continue

                # ID from /annonce/{id}
                eid_m = re.search(r'/annonce/(\d+)', href) or re.search(r'/(\d+)', href)
                eid = eid_m.group(1) if eid_m else hashlib.sha256(href.encode()).hexdigest()[:12]
                if f"ps-{eid}" in existing_ids:
                    continue

                # Price: .listing-price-main span
                price = None
                price_el = card.select_one('.listing-price-main span')
                if not price_el:
                    price_el = card.select_one('[class*="price"]')
                if price_el:
                    price = _clean_price(price_el.get_text(strip=True))

                # Title: a.listing-title text
                title = link_el.get_text(strip=True) if link_el else ''

                # Location: .item-location
                location_el = card.select_one('.item-location')
                address = location_el.get('title', '') or (location_el.get_text(strip=True) if location_el else '')
                if not address:
                    address = city

                # Highlights: "Appartement • 2.5 pces • 65 m²"
                highlights_el = card.select_one('.item-highlights')
                highlights = highlights_el.get_text(strip=True) if highlights_el else ''
                rooms = _clean_rooms(highlights)
                surface = _clean_surface(highlights)
                prop_type = _guess_type(highlights + ' ' + title)

                # Images: .image-gallery-picture img + srcSet
                images = []
                for img_el in card.select('.image-gallery-picture img, .item-picture-img img'):
                    src = img_el.get('src', '')
                    if src and src.startswith('http') and 'logo' not in src.lower():
                        images.append(src)
                # Also check <source> srcSet for avif/webp
                for source_el in card.select('.image-gallery-picture source, .item-picture-img source'):
                    srcset = source_el.get('srcSet', '') or source_el.get('srcset', '')
                    if srcset:
                        # Take first URL from srcSet
                        first_url = srcset.split(',')[0].strip().split(' ')[0]
                        if first_url.startswith('http'):
                            images.append(first_url)
                images = list(dict.fromkeys(images))[:5]

                if price or title:
                    existing_ids.add(f"ps-{eid}")
                    results.append(_make_property(
                        external_id=f"ps-{eid}", source='Properstar',
                        source_url=href, title=title, description='',
                        property_type=prop_type, transaction=transaction,
                        price=price, rooms=rooms, surface=surface, floor=None,
                        address=address, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=None, longitude=None,
                        features=[], images=images, published_at=None,
                                            search_city=city,
))
            except Exception as e:
                log.debug(f"[Properstar] Card parse error: {e}")

    log.info(f"[Properstar] Total: {len(results)} listings")
    return results


# ============================================================
# JOUVAL — Régie locale Neuchâtel (WordPress + Estatik plugin)
# ============================================================

def scrape_jouval(city=None, transaction="location", max_pages=30):
    """Scrape jouval.ch (Régie Jouval, Neuchâtel).
    All listings are NE — city param is ignored (returns all NE biens).
    Direct requests, no ScrapingBee. Site has ~159 properties total.
    Estatik plugin: /property-category/{slug}/page/N/ (6 per page).
    """
    log.info(f"[Jouval] Fetching all listings (transaction={transaction})")
    results = []
    seen_ids = set()

    # Use the proper category URL slug (visible in card "terms")
    cat_slug = 'location' if transaction == 'location' else 'vente'
    base_url = f"https://jouval.ch/property-category/{cat_slug}/"

    for page in range(1, max_pages + 1):
        if page == 1:
            url = base_url
        else:
            url = f"{base_url}page/{page}/"

        status, html = _direct_get(url)
        if status != 200 or len(html) < 5000:
            log.warning(f"[Jouval] Page {page}: HTTP {status} — stop pagination")
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('div.js-es-listing.es-listing[data-post-id]')
        if not cards:
            log.info(f"[Jouval] Page {page}: no cards found — stop")
            break

        page_count = 0
        for card in cards:
            try:
                ext_id = card.get('data-post-id', '').strip()
                if not ext_id or ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                # Title + URL
                title_link = card.select_one('h3.es-listing__title a')
                if not title_link:
                    continue
                title = (title_link.get_text() or '').strip()
                source_url = title_link.get('href', '').strip()

                # Price: "CHF 950" or "CHF 1'250" or "CHF 580'000.-"
                price_el = card.select_one('span.es-price')
                price = None
                if price_el:
                    price_txt = price_el.get_text(strip=True)
                    digits = re.sub(r"[^\d]", '', price_txt)
                    if digits:
                        price = int(digits)

                # Address: "Rue X, 2034 Peseux, Suisse"
                addr_el = card.select_one('div.es-address')
                address = ''
                postal = None
                location_city = ''
                if addr_el:
                    address = addr_el.get_text(strip=True)
                    pc_match = re.search(r'\b(\d{4})\b', address)
                    if pc_match:
                        postal = pc_match.group(1)
                        # City after postal: "2034 Peseux, Suisse"
                        after = address.split(postal, 1)[-1].strip()
                        location_city = after.split(',')[0].strip()

                # Rooms (Chambres à coucher)
                rooms = None
                rooms_el = card.select_one('li.es-listing__meta-bedrooms b')
                if rooms_el:
                    try:
                        rooms = float(rooms_el.get_text(strip=True))
                    except ValueError:
                        pass

                # Property type from CSS class on outer wrapper
                outer = card.parent if card.parent else card
                outer_classes = ' '.join(outer.get('class', []) if hasattr(outer, 'get') else [])
                prop_type = 'appartement'
                if 'es_type-place-de-parc' in outer_classes:
                    prop_type = 'parking'
                elif 'es_type-maison' in outer_classes or 'es_type-villa' in outer_classes:
                    prop_type = 'maison'
                elif 'es_type-commerce' in outer_classes or 'es_type-locaux' in outer_classes:
                    prop_type = 'commerce'

                # Image from data-bg-image
                img_url = ''
                img_el = card.select_one('div.es-listing__image__background[data-bg-image]')
                if img_el:
                    bg = img_el.get('data-bg-image', '')
                    m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", bg)
                    if m:
                        img_url = m.group(1).replace('&#039;', '').replace("'", '')

                results.append(_make_property(
                    external_id=ext_id, source='jouval', source_url=source_url,
                    title=title, description='',
                    property_type=prop_type, transaction=transaction,
                    price=price, rooms=rooms, surface=None,
                    floor=None, address=address, city=location_city or 'Neuchâtel',
                    canton='NE', postal_code=postal,
                    latitude=None, longitude=None,
                    features=[], images=[img_url] if img_url else [],
                    published_at=None,
                                    search_city=city,
))
                page_count += 1
            except Exception as e:
                log.debug(f"[Jouval] Card parse error: {e}")

        log.info(f"[Jouval] Page {page}: {page_count} new listings")
        if page_count == 0:
            break  # End of pagination

        time.sleep(0.5)  # Polite throttling

    # Filter out None entries (language filter rejects)
    results = [r for r in results if r is not None]
    log.info(f"[Jouval] Total: {len(results)} listings")
    return results


# ============================================================
# MULLER & CHRISTE — Régie Neuchâtel (custom CMS, /biens/{louer|acheter}/)
# ============================================================

def scrape_muller_christe(city=None, transaction="location", max_pages=15):
    """Scrape mulleretchriste.ch. URL pattern:
      /biens/louer/page/N/ + /biens/acheter/page/N/
    ~9 listings/page, ~10 pages total. Direct requests, no ScrapingBee.
    Listing pages don't expose rooms+surface for all biens — fields may be None.
    """
    log.info(f"[Muller&Christe] Fetching all listings (transaction={transaction})")
    results = []
    seen_ids = set()
    base = "https://www.mulleretchriste.ch"
    tx_path = 'louer' if transaction == 'location' else 'acheter'

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{base}/biens/{tx_path}/"
        else:
            url = f"{base}/biens/{tx_path}/page/{page}/"

        status, html = _direct_get(url)
        if status != 200 or len(html) < 5000:
            log.warning(f"[Muller&Christe] Page {page}: HTTP {status} — stop")
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('div.real-estate-item')
        if not cards:
            log.info(f"[Muller&Christe] Page {page}: no cards — stop")
            break

        page_count = 0
        for card in cards:
            try:
                # The card is wrapped by an <a> — find the closest ancestor link
                link = card.find_parent('a')
                if not link or not link.get('href'):
                    continue
                href = link.get('href', '').strip()
                source_url = href if href.startswith('http') else f"{base}{href}"

                # URL pattern: /biens/{louer|acheter}/{type}/{city}/{slug}/
                parts = [p for p in href.strip('/').split('/') if p]
                if len(parts) < 5:
                    continue
                # parts: [biens, louer, type, city, slug]
                tx_url = parts[1]
                prop_type_slug = parts[2]
                city_slug = parts[3]
                title_slug = parts[4]

                # Skip mismatched transaction (defensive — pagination should already filter)
                expected_tx = 'louer' if transaction == 'location' else 'acheter'
                if tx_url != expected_tx:
                    continue

                # Use full URL as external_id (stable, unique)
                ext_id = title_slug
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                # Title from slug (capitalize, replace dashes)
                title = title_slug.replace('-', ' ').strip().capitalize()

                # Property type from URL slug
                pt_map = {
                    'appartement': 'appartement', 'maison': 'maison', 'villa': 'villa',
                    'place-de-parc': 'parking', 'parking': 'parking', 'garage': 'parking',
                    'local-commercial': 'commerce', 'commerce': 'commerce', 'bureau': 'commerce',
                    'terrain': 'terrain', 'immeuble': 'immeuble',
                }
                prop_type = pt_map.get(prop_type_slug, 'appartement')

                # City: deslugify
                location_city = city_slug.replace('-', ' ').strip().title()

                # Price: "1'120.- CHF" or "40.- CHF" or "750'000.- CHF"
                price = None
                price_el = card.select_one('span.price')
                if price_el:
                    digits = re.sub(r"[^\d]", '', price_el.get_text())
                    if digits:
                        price = int(digits)

                # Rooms: "4 pièce(s)" — only when present
                rooms = None
                rooms_el = card.select_one('span.rooms')
                if rooms_el:
                    m = re.search(r'(\d+(?:[.,]\d+)?)', rooms_el.get_text())
                    if m:
                        try:
                            rooms = float(m.group(1).replace(',', '.'))
                        except ValueError:
                            pass

                # First image
                img_url = ''
                img_el = card.select_one('div.item-images img')
                if img_el:
                    src = img_el.get('src', '') or img_el.get('data-src', '')
                    if src:
                        img_url = src if src.startswith('http') else f"{base}{src}"

                # Skip placeholder images
                if 'placholder' in img_url or 'placeholder' in img_url:
                    img_url = ''

                # Address: not in listing — derived from city only
                address = location_city

                results.append(_make_property(
                    external_id=ext_id, source='muller-christe', source_url=source_url,
                    title=title, description='',
                    property_type=prop_type, transaction=transaction,
                    price=price, rooms=rooms, surface=None,
                    floor=None, address=address, city=location_city,
                    canton=CITY_CANTONS.get(location_city.lower(), 'NE'),
                    postal_code=None,
                    latitude=None, longitude=None,
                    features=[], images=[img_url] if img_url else [],
                    published_at=None,
                                    search_city=city,
))
                page_count += 1
            except Exception as e:
                log.debug(f"[Muller&Christe] Card parse error: {e}")

        log.info(f"[Muller&Christe] Page {page}: {page_count} new listings")
        if page_count == 0:
            break

        time.sleep(0.5)

    results = [r for r in results if r is not None]
    log.info(f"[Muller&Christe] Total: {len(results)} listings")
    return results


# ============================================================
# FIDIMMOBIL — Agence Neuchâtel (PHP SSR, /fr/louer.php)
# ============================================================

def scrape_fidimmobil(city=None, transaction="location", max_pages=1):
    """Scrape fidimmobil.ch (Fidimmobil SA, Neuchâtel & La Chaux-de-Fonds).
    All listings are NE — city param is ignored (returns all NE biens).
    Direct requests, no ScrapingBee. Single-page listing (~23 biens on /fr/louer.php).

    LOUER only for now: ACHAT is on vente.fidimmobil.ch (WordPress Nectar with ~11
    promotional projects, not individual units — different data model).
    """
    # Only scrape LOUER — ACHAT subdomain is a different tech stack
    if transaction != 'location':
        log.info(f"[Fidimmobil] transaction={transaction} not supported (LOUER only) — skip")
        return []

    log.info(f"[Fidimmobil] Fetching all listings (transaction={transaction})")
    results = []
    seen_ids = set()
    base = "https://www.fidimmobil.ch"
    url = f"{base}/fr/louer.php"

    status, html = _direct_get(url)
    if status != 200 or len(html) < 5000:
        log.warning(f"[Fidimmobil] HTTP {status} (len={len(html)}) — abort")
        return []

    soup = BeautifulSoup(html, 'html.parser')
    # Cards: <a href="/fr/location.php?id=XXX"><div class="immeuble-bloc">...</div></a>
    links = soup.select('a[href^="/fr/location.php?id="]')

    for link in links:
        try:
            href = link.get('href', '').strip()
            # Extract ID from ?id=XXX
            m = re.search(r'id=([A-Za-z0-9]+)', href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)

            source_url = href if href.startswith('http') else f"{base}{href}"

            # NOTE: the site uses invalid self-closing <div ... /> which makes
            # BeautifulSoup close div.immeuble-bloc too early. Query the <a>
            # directly — h3 and p.titre end up as siblings of the card div.
            if not link.select_one('div.immeuble-bloc'):
                continue

            # City: <h3>Neuchâtel</h3>
            city_el = link.select_one('h3')
            location_city = (city_el.get_text(strip=True) if city_el else '') or 'Neuchâtel'

            # Title block: <p class="titre">5.5 pièces<br>CHF 2'950.-/mois</p>
            # or                          "Local<br>CHF 1'900.-/mois"
            titre_el = link.select_one('p.titre')
            raw_titre = ''
            if titre_el:
                # Replace <br> with newlines before extracting text
                for br in titre_el.find_all('br'):
                    br.replace_with('\n')
                raw_titre = titre_el.get_text('\n', strip=True)

            lines = [l.strip() for l in raw_titre.split('\n') if l.strip()]
            type_line = lines[0] if lines else ''
            price_line = lines[1] if len(lines) > 1 else ''

            # Rooms from type_line ("5.5 pièces", "2 pièces", "Local", "Studio", etc.)
            rooms = None
            rm = re.search(r'(\d+(?:[.,]\d+)?)\s*pi[eè]ce', type_line, re.IGNORECASE)
            if rm:
                try:
                    rooms = float(rm.group(1).replace(',', '.'))
                except ValueError:
                    pass

            # Property type
            prop_type = 'appartement'
            t_lower = type_line.lower()
            if 'local' in t_lower or 'commerc' in t_lower or 'bureau' in t_lower or 'arcade' in t_lower:
                prop_type = 'commerce'
            elif 'parking' in t_lower or 'garage' in t_lower or 'place' in t_lower:
                prop_type = 'parking'
            elif 'maison' in t_lower or 'villa' in t_lower:
                prop_type = 'maison'
            elif 'studio' in t_lower:
                prop_type = 'appartement'

            # Price: "CHF 2'950.-/mois"
            price = None
            digits = re.sub(r"[^\d]", '', price_line)
            if digits:
                price = int(digits)

            # Image: <div class="img-list" style="background-image:url('/files/.../003525_1.jpg');">
            img_url = ''
            img_el = link.select_one('div.img-list')
            if img_el:
                style = img_el.get('style', '')
                m_bg = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
                if m_bg:
                    src = m_bg.group(1).strip()
                    img_url = src if src.startswith('http') else f"{base}{src.lstrip('/')}"
                    # Fidimmobil uses relative paths starting with /
                    if not img_url.startswith('http'):
                        img_url = f"{base}/{img_url.lstrip('/')}"

            # Title: composite. Don't start with a digit — _make_property
            # strips leading-digit prefixes as a price-cleanup heuristic.
            if type_line:
                title = f"À louer à {location_city} — {type_line}"
            else:
                title = f"À louer à {location_city}"

            results.append(_make_property(
                external_id=ext_id, source='fidimmobil', source_url=source_url,
                title=title, description='',
                property_type=prop_type, transaction=transaction,
                price=price, rooms=rooms, surface=None,
                floor=None, address=location_city, city=location_city,
                canton=CITY_CANTONS.get(location_city.lower(), 'NE'),
                postal_code=None,
                latitude=None, longitude=None,
                features=[], images=[img_url] if img_url else [],
                published_at=None,
                            search_city=city,
))
        except Exception as e:
            log.debug(f"[Fidimmobil] Card parse error: {e}")

    results = [r for r in results if r is not None]
    log.info(f"[Fidimmobil] Total: {len(results)} listings")
    return results


# ============================================================
# MAIN
# ============================================================

# Process-level cache: prevents NE agency scrapers from running 16× per cron
# (cron calls scrape_all once per NE city). Cleared at process exit.
_NE_AGENCY_CACHE = set()

# Per-run scraper stats: tracks how many listings each scraper produced across
# all cities of the current run. Used by cron_job.py to detect scrapers returning
# 0 results everywhere (→ portal layout changed, credits exhausted, etc.).
_SCRAPER_STATS = {}
# v6.3.3 (SC4) : stats granulaires par (scraper, ville). Permet à
# cron_job.py de détecter un trou ciblé (ex : Homegate KO sur Lausanne
# mais OK sur Neuchâtel) plutôt que de n'alerter que sur 0 global.
_SCRAPER_STATS_CITY = {}


def reset_scraper_stats():
    """Reset per-scraper counters. Call once at the start of a cron run."""
    global _SCRAPER_STATS, _SCRAPER_STATS_CITY, _NE_AGENCY_CACHE
    _SCRAPER_STATS = {}
    _SCRAPER_STATS_CITY = {}
    _NE_AGENCY_CACHE = set()


def get_scraper_stats():
    """Return a snapshot of {scraper_name: total_listings_across_cities}."""
    return dict(_SCRAPER_STATS)


def get_scraper_stats_city():
    """Return a snapshot of {(scraper_name, city): count} for granular alerting."""
    return dict(_SCRAPER_STATS_CITY)


# For small towns, also scrape the nearest large city to find listings
# that portals list under the main city name (e.g., Peseux listings under "Neuchâtel")
NEARBY_MAIN_CITY = {
    'peseux': 'Neuchâtel', 'colombier': 'Neuchâtel', 'boudry': 'Neuchâtel',
    'cortaillod': 'Neuchâtel', 'hauterive': 'Neuchâtel', 'saint-blaise': 'Neuchâtel',
    'marin-epagnier': 'Neuchâtel', 'bevaix': 'Neuchâtel', 'corcelles-cormondrèche': 'Neuchâtel',
    'la tène': 'Neuchâtel', 'le landeron': 'Neuchâtel', 'val-de-ruz': 'Neuchâtel',
    'milvignes': 'Neuchâtel', 'le locle': 'La Chaux-de-Fonds',
    'fleurier': 'La Chaux-de-Fonds', 'val-de-travers': 'La Chaux-de-Fonds',
    'prilly': 'Lausanne', 'pully': 'Lausanne', 'renens': 'Lausanne',
    'ecublens': 'Lausanne', 'lutry': 'Lausanne', 'savigny': 'Lausanne',
    'carouge': 'Genève', 'meyrin': 'Genève', 'lancy': 'Genève',
    'vernier': 'Genève', 'onex': 'Genève', 'thônex': 'Genève',
    'vevey': 'Montreux', 'montreux': 'Vevey',
    'sierre': 'Sion', 'martigny': 'Sion',
    'bienne': 'Bienne', 'biel': 'Biel',
}


def scrape_all(city="Lausanne", transaction="location", skip_nearby=False,
               disabled_sources=None):
    """Scrape all portals for a given city and transaction type.
    For small towns, also scrapes the nearest large city to catch listings
    that portals list under the main city name.

    skip_nearby=True disables the NEARBY_MAIN_CITY expansion. Use it from the
    cron job when all relevant main cities are already in the scrape_targets
    list (avoids scraping Neuchâtel 12× — once per small NE town).

    disabled_sources : iterable de noms de portails à skipper (matchés sur
    le 1er élément du tuple `scrapers` ci-dessous, ex: 'Homegate',
    'ImmoScout24'). Set vide / None = aucun skip. Utilisé par cron_job.py
    pour respecter les flags env ENABLE_HOMEGATE / ENABLE_IMMOSCOUT24
    (drop produit 30/04 — code des scrapers conservé pour réactivation
    future si la stratégie change).
    """
    all_results = []
    disabled = set(disabled_sources or ())

    # Determine cities to scrape
    cities_to_scrape = [city]
    if not skip_nearby:
        nearby = NEARBY_MAIN_CITY.get(city.lower())
        if nearby and nearby.lower() != city.lower():
            cities_to_scrape.append(nearby)
            log.info(f"[scrape_all] Also scraping nearby city: {nearby}")

    all_scrapers = [
        ('Flatfox', scrape_flatfox),
        ('Homegate', scrape_homegate),
        ('ImmoScout24', scrape_immoscout),
        ('Immobilier.ch', scrape_immobilier_ch),
        # ('Anibis', scrape_anibis),  # DISABLED: __NEXT_DATA__ returns 0 items — wastes credits. To re-enable, fix the parser.
        ('Acheter-Louer', scrape_acheter_louer),
        ('Comparis', scrape_comparis),
        ('Properstar', scrape_properstar),
    ]
    scrapers = [(name, fn) for name, fn in all_scrapers if name not in disabled]
    if disabled:
        log.info(f"[scrape_all] Skipping disabled sources: {sorted(disabled)}")

    # NE-only agency scrapers (single-fetch, ignores city loop) — only run for Neuchâtel main
    ne_agency_scrapers = [
        ('Jouval', scrape_jouval),
        ('Muller&Christe', scrape_muller_christe),
        ('Fidimmobil', scrape_fidimmobil),
    ]

    # Parallelize BY PORTAL (not by city) to respect per-site rate limits.
    # Each scraper keeps its own time.sleep(1) between pages of the same portal.
    # max_workers=4 → at most 4 portals hit simultaneously for a given city.
    for scrape_city in cities_to_scrape:
        log.info(f"[scrape_all] Scraping city: {scrape_city}")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(scraper, city=scrape_city, transaction=transaction): name
                for name, scraper in scrapers
            }
            for future in as_completed(futures):
                name = futures[future]
                _SCRAPER_STATS.setdefault(name, 0)
                _SCRAPER_STATS_CITY.setdefault((name, scrape_city), 0)
                try:
                    results = [r for r in future.result() if r is not None]
                    all_results.extend(results)
                    _SCRAPER_STATS[name] += len(results)
                    _SCRAPER_STATS_CITY[(name, scrape_city)] += len(results)
                    log.info(f"[{name}][{scrape_city}] {len(results)} results")
                except Exception as e:
                    log.error(f"[{name}][{scrape_city}] FAILED: {e}")

    # NE agencies: only run when scraping a NE city (avoid duplication for VD/GE cities)
    # AND only once per transaction per process (cron loops 16 NE cities — don't scrape Jouval 16x)
    is_ne_scrape = any(
        (sc.lower() == 'neuchâtel') or (sc.lower() == 'neuchatel') or
        CITY_CANTONS.get(sc.lower(), '') == 'NE'
        for sc in cities_to_scrape
    )
    if is_ne_scrape:
        pending_ne = [
            (name, scraper) for name, scraper in ne_agency_scrapers
            if f"{name}:{transaction}" not in _NE_AGENCY_CACHE
        ]
        if pending_ne:
            with ThreadPoolExecutor(max_workers=min(4, len(pending_ne))) as executor:
                futures = {
                    executor.submit(scraper, transaction=transaction): name
                    for name, scraper in pending_ne
                }
                for future in as_completed(futures):
                    name = futures[future]
                    _SCRAPER_STATS.setdefault(name, 0)
                    try:
                        results = [r for r in future.result() if r is not None]
                        all_results.extend(results)
                        _SCRAPER_STATS[name] += len(results)
                        _NE_AGENCY_CACHE.add(f"{name}:{transaction}")
                        log.info(f"[{name}][NE-all] {len(results)} results")
                    except Exception as e:
                        log.error(f"[{name}][NE-all] FAILED: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for p in all_results:
        key = p['external_id']
        if key not in seen:
            seen.add(key)
            unique.append(p)

    log.info(f"Total: {len(unique)} unique listings from {len(scrapers)} portals")
    return unique


def _ensure_property_sources_table(db):
    """Create the property_sources table if missing. Idempotent."""
    cur = db.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS property_sources (
                id SERIAL PRIMARY KEY,
                property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                source VARCHAR(50) NOT NULL,
                source_url VARCHAR(500),
                external_id VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(property_id, source)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_property_sources_property ON property_sources(property_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_property_sources_source ON property_sources(source, external_id)")
        db.commit()
    except Exception as e:
        log.warning(f"property_sources table ensure failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        cur.close()


def _find_cross_portal_duplicate(cur, p):
    """Look for an existing active property (from a different source) that
    matches this listing on city + price (±5%) + rooms + surface (±10%).
    Returns the existing property_id or None."""
    city = (p.get('city') or '').strip()
    price = p.get('price')
    rooms = p.get('rooms')
    surface = p.get('surface')
    if not (city and price and rooms):
        return None
    price_min = price * 0.95
    price_max = price * 1.05
    try:
        if surface:
            cur.execute("""
                SELECT id FROM properties
                WHERE LOWER(city) = LOWER(%s)
                  AND source != %s
                  AND is_active = TRUE
                  AND price BETWEEN %s AND %s
                  AND rooms = %s
                  AND surface IS NOT NULL
                  AND surface BETWEEN %s AND %s
                ORDER BY id ASC
                LIMIT 1
            """, (city, p['source'], price_min, price_max, rooms, surface * 0.9, surface * 1.1))
        else:
            cur.execute("""
                SELECT id FROM properties
                WHERE LOWER(city) = LOWER(%s)
                  AND source != %s
                  AND is_active = TRUE
                  AND price BETWEEN %s AND %s
                  AND rooms = %s
                ORDER BY id ASC
                LIMIT 1
            """, (city, p['source'], price_min, price_max, rooms))
        row = cur.fetchone()
        if not row:
            return None
        # RealDictRow supports both integer and key access
        try:
            return row[0]
        except (KeyError, TypeError):
            return row.get('id')
    except Exception as e:
        log.debug(f"cross-portal dedup query error: {e}")
        return None


def save_to_db(db, listings):
    """Save scraped listings to the properties table.
    Detects cross-portal duplicates (same bien listed on multiple sites) and
    records the extra sources in property_sources instead of duplicating the row."""
    _ensure_property_sources_table(db)

    cur = db.cursor()
    saved = 0
    deduped = 0
    price_changes = 0
    skipped_invalid_url = 0
    for p in listings:
        # C2.1 — reject listings with empty / garbage / wrong-domain URLs so
        # we never store a row the frontend can't link to.
        if not _is_valid_source_url(p.get('source_url'), p.get('source', '')):
            log.debug(
                "[save_to_db] skip %s/%s — invalid source_url: %r",
                p.get('source'), p.get('external_id'), p.get('source_url')
            )
            skipped_invalid_url += 1
            continue
        # Isolate each listing in a SAVEPOINT so one bad row doesn't abort
        # the whole batch (otherwise any error poisons the transaction).
        try:
            cur.execute("SAVEPOINT sp_listing")
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            cur = db.cursor()
            try:
                cur.execute("SAVEPOINT sp_listing")
            except Exception:
                pass
        try:
            # Check if property exists with different price (for price history tracking)
            cur.execute("""
                SELECT id, price FROM properties
                WHERE external_id = %s AND source = %s
            """, (p['external_id'], p['source']))
            existing = cur.fetchone()

            if existing and existing[1] and p['price'] and existing[1] != p['price']:
                old_price = existing[1]
                new_price = p['price']
                change_pct = round(((new_price - old_price) / old_price) * 100, 2) if old_price > 0 else 0
                cur.execute("""
                    INSERT INTO price_history (property_id, old_price, new_price, change_pct)
                    VALUES (%s, %s, %s, %s)
                """, (existing[0], old_price, new_price, change_pct))
                price_changes += 1

            prop_id = None

            if not existing:
                # First time we see this (source, external_id). Check if another
                # portal already lists the same bien → link sources, skip insert.
                dup_id = _find_cross_portal_duplicate(cur, p)
                if dup_id:
                    cur.execute("""
                        INSERT INTO property_sources (property_id, source, source_url, external_id)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (property_id, source) DO UPDATE SET
                            source_url = EXCLUDED.source_url,
                            external_id = EXCLUDED.external_id
                    """, (dup_id, p['source'], p.get('source_url'), p['external_id']))
                    # Keep the existing property fresh so the dedup/scoring pipeline
                    # continues to consider it.
                    cur.execute("""
                        UPDATE properties
                           SET is_active = TRUE, scraped_at = NOW()
                         WHERE id = %s
                    """, (dup_id,))
                    deduped += 1
                    cur.execute("RELEASE SAVEPOINT sp_listing")
                    continue

            cur.execute("""
                INSERT INTO properties (
                    external_id, source, source_url, title, description,
                    property_type, transaction, price, currency, price_unit,
                    rooms, surface, floor, address, city, canton, postal_code,
                    latitude, longitude, features, images,
                    contact_name, contact_phone, contact_email,
                    published_at, scraped_at, first_seen_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, NOW(), NOW()
                )
                ON CONFLICT (external_id, source) DO UPDATE SET
                    price = EXCLUDED.price,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    images = CASE WHEN EXCLUDED.images IS NOT NULL AND array_length(EXCLUDED.images, 1) > 0
                                  THEN EXCLUDED.images ELSE properties.images END,
                    rooms = COALESCE(EXCLUDED.rooms, properties.rooms),
                    surface = COALESCE(EXCLUDED.surface, properties.surface),
                    features = CASE WHEN EXCLUDED.features IS NOT NULL AND array_length(EXCLUDED.features, 1) > 0
                                    THEN EXCLUDED.features ELSE properties.features END,
                    source_url = EXCLUDED.source_url,
                    is_active = TRUE,
                    scraped_at = NOW()
                RETURNING id
            """, (
                p['external_id'], p['source'], p['source_url'],
                p['title'], p.get('description', ''),
                p['property_type'], p['transaction'],
                p['price'], p['currency'], p['price_unit'],
                p['rooms'], p['surface'], p.get('floor'),
                p['address'], p['city'], p['canton'], p.get('postal_code'),
                p.get('latitude'), p.get('longitude'),
                p.get('features', []), p.get('images', []),
                p.get('contact_name'), p.get('contact_phone'), p.get('contact_email'),
                p.get('published_at')
            ))
            row = cur.fetchone()
            if row:
                try:
                    prop_id = row[0]
                except (KeyError, TypeError):
                    prop_id = row.get('id')

            # Record this source in property_sources (even for primary source) so
            # we have a uniform view of "where is this bien listed".
            if prop_id:
                cur.execute("""
                    INSERT INTO property_sources (property_id, source, source_url, external_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (property_id, source) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        external_id = EXCLUDED.external_id
                """, (prop_id, p['source'], p.get('source_url'), p['external_id']))

            cur.execute("RELEASE SAVEPOINT sp_listing")
            saved += 1
        except Exception as e:
            log.debug(f"Save error for {p.get('external_id')}: {e}")
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_listing")
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

    try:
        db.commit()
    except Exception as e:
        log.error(f"save_to_db commit failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    cur.close()
    log.info(
        f"Saved {saved}/{len(listings)} to database "
        f"({deduped} cross-portal duplicates linked, {price_changes} price changes detected, "
        f"{skipped_invalid_url} rejected for invalid source_url)"
    )
    return saved
