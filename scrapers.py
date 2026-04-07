"""
Lou Garou — Scrapers immobiliers suisses (v4 — Sans ScrapingBee)
Tous les scrapers fonctionnent avec des requêtes HTTP directes.
Méthodes: JSON API, __NEXT_DATA__, __INITIAL_STATE__, HTML parsing.

Portails couverts:
  - Flatfox (API JSON directe)
  - ImmoScout24 (__INITIAL_STATE__)
  - Homegate (__NEXT_DATA__ / __INITIAL_STATE__)
  - Comparis (__NEXT_DATA__)
  - Anibis (API interne SMG)
  - Immobilier.ch (HTML parsing)
  - Acheter-Louer (HTML parsing)
  - Properstar (HTML parsing)
  - Newhome.ch (__NEXT_DATA__ / HTML)
  - Tutti.ch (API SMG)
  - RealAdvisor (__NEXT_DATA__)

Usage:
    from scrapers import scrape_all
    results = scrape_all(city="Lausanne", transaction="achat")
"""

import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import quote, urlencode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

# Import both curl_cffi AND cloudscraper — use curl_cffi first, cloudscraper as fallback for 403
_curl_session = None
_cloudscraper_session = None

try:
    from curl_cffi.requests import Session as CurlSession
    _curl_session = CurlSession(impersonate="chrome")
    log.info("[Init] curl_cffi loaded — Chrome TLS impersonation active")
except ImportError:
    log.warning("[Init] curl_cffi not available")

try:
    import cloudscraper
    _cloudscraper_session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    log.info("[Init] cloudscraper loaded — Cloudflare JS challenge solver available as fallback")
except ImportError:
    log.warning("[Init] cloudscraper not available")

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
    'ecublens': 'VD', 'sierre': 'VS', 'martigny': 'VS', 'monthey': 'VS',
    'bulle': 'FR', 'aigle': 'VD', 'thun': 'BE', 'thoune': 'BE',
    'brig': 'VS', 'visp': 'VS', 'aarau': 'AG', 'olten': 'SO',
    'solothurn': 'SO', 'soleure': 'SO', 'schaffhausen': 'SH',
    'chur': 'GR', 'coire': 'GR', 'bellinzona': 'TI', 'locarno': 'TI',
}


BROWSER_HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'fr-CH,fr;q=0.9,de-CH;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate',  # No brotli — avoids garbled responses
    'Sec-Ch-Ua': '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="24"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
}

# Residential proxy (Decodo/SmartProxy) — set PROXY_URL in env
# Format: http://username:password@gate.decodo.com:10001
PROXY_URL = os.environ.get('PROXY_URL', '')

# Site Unblocker (Decodo) — for Cloudflare-protected sites
# Format: http://username:password@unblock.decodo.com:60000
# Set UNBLOCKER_URL in env, or auto-derive from PROXY_URL
UNBLOCKER_URL = os.environ.get('UNBLOCKER_URL', '')
if not UNBLOCKER_URL and PROXY_URL and 'decodo.com' in PROXY_URL:
    # Auto-derive: replace gate hostname + port with unblocker endpoint
    UNBLOCKER_URL = PROXY_URL.replace('gate.decodo.com', 'unblock.decodo.com').replace('ch.decodo.com', 'unblock.decodo.com')
    # Replace any port with 60000
    import re as _re
    UNBLOCKER_URL = _re.sub(r':(\d+)$', ':60000', UNBLOCKER_URL)
    log.info(f"[Init] Auto-derived UNBLOCKER_URL from PROXY_URL")

# ScrapingBee (legacy fallback, can be removed once proxy is active)
SCRAPINGBEE_KEY = os.environ.get('SCRAPINGBEE_API_KEY', '')
SCRAPINGBEE_URL = 'https://app.scrapingbee.com/api/v1'


def _sb_get(url, render_js=False):
    """Fetch via ScrapingBee (paid). Returns (status_code, html_text)."""
    if not SCRAPINGBEE_KEY:
        return 0, ''
    params = {
        'api_key': SCRAPINGBEE_KEY,
        'url': url,
        'stealth_proxy': 'true',
        'country_code': 'ch',
    }
    if render_js:
        params['render_js'] = 'true'
        params['wait'] = 3000
    try:
        r = requests.get(SCRAPINGBEE_URL, params=params, timeout=20)
        log.info(f"[ScrapingBee] {url[:60]} → HTTP {r.status_code} ({len(r.text)} bytes)")
        return r.status_code, r.text
    except Exception as e:
        log.error(f"[ScrapingBee] {url[:60]}: {e}")
        return 0, ''


def _get(url, headers=None, timeout=20, use_sb=False):
    """HTTP GET with fallback chain:
    1. Residential proxy (if PROXY_URL set) — best success rate
    2. curl_cffi (Chrome TLS impersonation)
    3. cloudscraper (Cloudflare JS solver)
    4. requests (plain)
    5. ScrapingBee (legacy fallback, if use_sb=True)
    """
    h = {**BROWSER_HEADERS}
    if headers:
        h.update(headers)

    proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None

    # Strategy 1: Try with residential proxy first (highest success rate)
    if proxies:
        try:
            r = requests.get(url, headers=h, timeout=timeout, proxies=proxies, allow_redirects=True)
            text = r.text if isinstance(r.text, str) else r.content.decode('utf-8', errors='replace')
            log.info(f"[proxy] {url[:70]} → {r.status_code} ({len(text)} bytes)")
            if r.status_code != 403:
                return r.status_code, text
            log.info(f"[proxy] 403 received, trying Site Unblocker...")
        except Exception as e:
            log.error(f"[proxy] GET {url[:70]}: {e}")

    # Strategy 1b: Site Unblocker for Cloudflare-protected sites (403 fallback)
    if UNBLOCKER_URL:
        try:
            ub_proxies = {'http': UNBLOCKER_URL, 'https': UNBLOCKER_URL}
            r = requests.get(url, headers=h, timeout=60, proxies=ub_proxies, allow_redirects=True, verify=False)
            text = r.text if isinstance(r.text, str) else r.content.decode('utf-8', errors='replace')
            log.info(f"[unblocker] {url[:70]} → {r.status_code} ({len(text)} bytes)")
            if r.status_code == 200:
                return r.status_code, text
            log.info(f"[unblocker] {r.status_code} received, trying direct clients...")
        except Exception as e:
            log.error(f"[unblocker] GET {url[:70]}: {e}")

    # Strategy 2: Direct clients (curl_cffi → cloudscraper → requests)
    clients = []
    if _curl_session:
        clients.append(('curl_cffi', _curl_session))
    if _cloudscraper_session:
        clients.append(('cloudscraper', _cloudscraper_session))
    clients.append(('requests', requests))

    for name, client in clients:
        try:
            r = client.get(url, headers=h, timeout=timeout, allow_redirects=True)
            text = r.text if isinstance(r.text, str) else r.content.decode('utf-8', errors='replace')
            log.info(f"[{name}] {url[:70]} → {r.status_code} ({len(text)} bytes)")

            if r.status_code == 403 and name != clients[-1][0]:
                is_cf = 'Just a moment' in text[:500] or 'cf-' in text[:2000]
                log.info(f"[{name}] 403 received{' (Cloudflare)' if is_cf else ''}, trying next client...")
                continue

            # If still 403 after all direct clients, try ScrapingBee
            if r.status_code == 403 and use_sb and SCRAPINGBEE_KEY:
                log.info(f"[{name}] 403 — falling back to ScrapingBee")
                sb_status, sb_html = _sb_get(url, render_js=True)
                if sb_status == 200:
                    return sb_status, sb_html

            return r.status_code, text
        except Exception as e:
            log.error(f"[{name}] GET {url[:70]}: {e}")
            continue

    # Last resort: ScrapingBee
    if use_sb and SCRAPINGBEE_KEY:
        sb_status, sb_html = _sb_get(url, render_js=True)
        if sb_status == 200:
            return sb_status, sb_html

    return 0, ''


def _get_json(url, headers=None, timeout=20):
    """HTTP GET expecting JSON, with automatic fallback on 403."""
    h = {
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'fr-CH,fr;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
    }
    if headers:
        h.update(headers)

    proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None

    # Try residential proxy first
    if proxies:
        try:
            r = requests.get(url, headers=h, timeout=timeout, proxies=proxies, allow_redirects=True)
            ct = r.headers.get('content-type', '')
            if r.status_code == 200 and 'json' in ct:
                log.info(f"[proxy-json] {url[:70]} → 200")
                return r.status_code, r.json()
            if r.status_code != 403:
                return r.status_code, None
            log.info(f"[proxy-json] {url[:70]} → 403, trying unblocker...")
        except Exception as e:
            log.error(f"[proxy-json] GET {url[:70]}: {e}")

    # Try Site Unblocker for 403s
    if UNBLOCKER_URL:
        try:
            ub_proxies = {'http': UNBLOCKER_URL, 'https': UNBLOCKER_URL}
            r = requests.get(url, headers=h, timeout=60, proxies=ub_proxies, allow_redirects=True, verify=False)
            ct = r.headers.get('content-type', '')
            if r.status_code == 200 and 'json' in ct:
                log.info(f"[unblocker-json] {url[:70]} → 200")
                return r.status_code, r.json()
            if r.status_code != 403:
                return r.status_code, None
        except Exception as e:
            log.error(f"[unblocker-json] GET {url[:70]}: {e}")

    # Direct clients fallback
    clients = []
    if _curl_session:
        clients.append(('curl_cffi', _curl_session))
    if _cloudscraper_session:
        clients.append(('cloudscraper', _cloudscraper_session))
    clients.append(('requests', requests))

    for name, client in clients:
        try:
            r = client.get(url, headers=h, timeout=timeout, allow_redirects=True)
            ct = r.headers.get('content-type', '')
            if r.status_code == 200 and 'json' in ct:
                return r.status_code, r.json()
            if r.status_code == 403 and name != clients[-1][0]:
                continue
            return r.status_code, None
        except Exception as e:
            log.error(f"[{name}] GET JSON {url[:70]}: {e}")
            continue

    return 0, None


def _extract_next_data(html):
    """Extract __NEXT_DATA__ JSON from HTML."""
    match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>', html, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            log.error(f"__NEXT_DATA__ JSON parse error: {e}")
    return None


def _extract_initial_state(html):
    """Extract window.__INITIAL_STATE__ JSON from HTML."""
    # Try multiple patterns
    patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|$|\n)',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});',
    ]
    for pat in patterns:
        match = re.search(pat, html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                # Try with a more conservative match (find balanced braces)
                text = match.group(1)
                # Truncate at last valid closing brace
                depth = 0
                last_valid = 0
                for i, c in enumerate(text):
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            last_valid = i + 1
                            break
                if last_valid > 0:
                    try:
                        return json.loads(text[:last_valid])
                    except json.JSONDecodeError:
                        pass
    return None


def _make_property(external_id, source, source_url, title, description,
                   property_type, transaction, price, rooms, surface,
                   floor, address, city, canton, postal_code,
                   latitude, longitude, features, images, published_at):
    """Create a standardized property dict."""
    return {
        'external_id': str(external_id),
        'source': source,
        'source_url': source_url or '',
        'title': title or '',
        'description': (description or '')[:500],
        'property_type': property_type or 'appartement',
        'transaction': transaction,
        'price': price,
        'currency': 'CHF',
        'price_unit': 'mois' if transaction == 'location' else 'total',
        'rooms': rooms,
        'surface': surface,
        'floor': floor,
        'address': address or '',
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


def _guess_type(text):
    if not text:
        return 'appartement'
    t = text.lower()
    if any(w in t for w in ['maison', 'villa', 'chalet', 'house', 'haus']):
        return 'maison'
    if 'studio' in t:
        return 'studio'
    if 'loft' in t:
        return 'loft'
    if any(w in t for w in ['attique', 'penthouse', 'attic']):
        return 'attique'
    if 'duplex' in t:
        return 'duplex'
    if any(w in t for w in ['terrain', 'land', 'grundstück']):
        return 'terrain'
    if any(w in t for w in ['commercial', 'bureau', 'office', 'gewerbe']):
        return 'commercial'
    return 'appartement'


def _extract_postal(address):
    if not address:
        return None
    m = re.search(r'\b(\d{4})\b', str(address))
    return m.group(1) if m else None


def _clean_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    text = str(val).replace('\u2019', "'").replace("'", "").replace(',', '').replace('.–', '').replace('.-', '').replace('CHF', '').strip()
    nums = re.findall(r'\d+', text)
    if nums:
        n = int(nums[0])
        return n if n > 0 else None
    return None


def _clean_rooms(text):
    if not text:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    text = str(text).replace('½', '.5')
    nums = re.findall(r'[\d.]+', text)
    return float(nums[0]) if nums else None


def _clean_surface(text):
    if not text:
        return None
    if isinstance(text, (int, float)):
        return int(text) if text > 0 else None
    nums = re.findall(r'(\d+)\s*m', str(text))
    return int(nums[0]) if nums else None


def _slugify(city):
    """Make a URL-safe slug from a city name."""
    return city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e').replace('ê', 'e').replace('ô', 'o').replace('û', 'u').replace('ü', 'u').replace('ä', 'a').replace('ö', 'o')


# ============================================================
# FLATFOX — Direct JSON API (always works)
# ============================================================

def scrape_flatfox(city="Lausanne", transaction="location", limit=50):
    log.info(f"[Flatfox] Searching {city} ({transaction})")
    results = []
    offer_type = 'RENT' if transaction == 'location' else 'SALE'

    # Try multiple API endpoints (Flatfox has changed their API over time)
    endpoints = [
        "https://flatfox.ch/api/v1/public-listing/",
        "https://flatfox.ch/api/v1/flat/",
        "https://flatfox.ch/api/v1/public/listings/",
    ]

    for api_url in endpoints:
        try:
            # Try different city parameter names
            city_lower = city.lower()
            params = urlencode({
                'city': city,
                'offer_type': offer_type,
                'ordering': '-created',
                'limit': limit,
            })
            status, data = _get_json(f"{api_url}?{params}")
            log.info(f"[Flatfox] {api_url} → HTTP {status}")

            if status == 200 and data:
                items = data.get('results', data) if isinstance(data, dict) else data
                if not isinstance(items, list):
                    continue

                # Filter client-side: only keep results matching city
                filtered = []
                city_slug = _slugify(city)
                for item in items:
                    item_city = (item.get('city') or '').lower()
                    item_slug = _slugify(item_city)
                    # Exact or substring match on city name
                    if (city_lower == item_city or city_slug == item_slug
                            or city_lower in item_city or item_city in city_lower):
                        filtered.append(item)

                log.info(f"[Flatfox] {len(items)} total, {len(filtered)} matching {city}")
                if not filtered:
                    log.warning(f"[Flatfox] No city match — API may not support city filter")
                items = filtered

                for item in items:
                    pk = item.get('pk', item.get('id', ''))
                    slug = item.get('slug', pk)
                    price = item.get('rent_gross') or item.get('rent_net') or item.get('price_display') or item.get('selling_price')

                    imgs = []
                    for img in (item.get('images', []) or [])[:5]:
                        if isinstance(img, dict):
                            imgs.append(img.get('url', ''))
                        elif isinstance(img, str):
                            imgs.append(img)
                    imgs = [u for u in imgs if u]

                    results.append(_make_property(
                        external_id=f"ff-{pk}", source='Flatfox',
                        source_url=f"https://flatfox.ch/fr/flat/{slug}/",
                        title=item.get('title', ''),
                        description=item.get('description', ''),
                        property_type=_guess_type(item.get('object_category', '') + ' ' + item.get('title', '')),
                        transaction=transaction,
                        price=_clean_price(price),
                        rooms=item.get('number_of_rooms'),
                        surface=item.get('surface_living'),
                        floor=item.get('floor'),
                        address=(item.get('street', '') + ' ' + str(item.get('street_number', ''))).strip()
                                + ', ' + item.get('city', city),
                        city=item.get('city', city),
                        canton=CITY_CANTONS.get(item.get('city', city).lower(), ''),
                        postal_code=str(item.get('zipcode', '')) or None,
                        latitude=item.get('latitude'), longitude=item.get('longitude'),
                        features=item.get('attributes', []) or [],
                        images=imgs,
                        published_at=item.get('created'),
                    ))

                if results:
                    break
        except Exception as e:
            log.error(f"[Flatfox] {api_url} error: {e}")

    # Fallback: scrape the search page
    if not results:
        try:
            slug = _slugify(city)
            tx_slug = "louer" if transaction == "location" else "acheter"
            url = f"https://flatfox.ch/fr/search/?city={quote(city)}&offer_type={offer_type}"
            status, html = _get(url)
            log.info(f"[Flatfox] Search page: HTTP {status}, len={len(html)}")
            if status == 200:
                # Try __NEXT_DATA__
                nd = _extract_next_data(html)
                if nd:
                    items = []
                    try:
                        pp = nd.get('props', {}).get('pageProps', {})
                        for key in ['listings', 'flats', 'results']:
                            if key in pp and isinstance(pp[key], list):
                                items = pp[key]
                                break
                    except (KeyError, TypeError):
                        pass
                    if items:
                        log.info(f"[Flatfox] {len(items)} via __NEXT_DATA__")
                        for item in items:
                            pk = item.get('pk', item.get('id', ''))
                            results.append(_make_property(
                                external_id=f"ff-{pk}", source='Flatfox',
                                source_url=item.get('url', f"https://flatfox.ch/fr/flat/{pk}/"),
                                title=item.get('title', ''), description='',
                                property_type=_guess_type(item.get('title', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('rent_gross') or item.get('price')),
                                rooms=item.get('number_of_rooms'),
                                surface=item.get('surface_living'),
                                floor=item.get('floor'),
                                address=item.get('city', city), city=item.get('city', city),
                                canton=CITY_CANTONS.get(item.get('city', city).lower(), ''),
                                postal_code=None, latitude=item.get('latitude'), longitude=item.get('longitude'),
                                features=[], images=[],
                                published_at=item.get('created'),
                            ))
        except Exception as e:
            log.error(f"[Flatfox] Search page error: {e}")

    log.info(f"[Flatfox] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOSCOUT24 — via __INITIAL_STATE__ (no JS needed)
# ============================================================

def scrape_immoscout24(city="Lausanne", transaction="location", max_pages=3):
    log.info(f"[ImmoScout24] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = _slugify(city)

    for page in range(1, max_pages + 1):
        try:
            url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{slug}?pn={page}"
            status, html = _get(url, use_sb=True)
            log.info(f"[ImmoScout24] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            # Try __INITIAL_STATE__
            state = _extract_initial_state(html)
            if state:
                listings = []
                try:
                    listings = state['resultList']['search']['fullSearch']['result']['listings']
                except (KeyError, TypeError):
                    try:
                        listings = state['resultList']['search']['preSearch']['result']['listings']
                    except (KeyError, TypeError):
                        pass

                if not listings:
                    log.warning(f"[ImmoScout24] Page {page}: __INITIAL_STATE__ found but no listings")
                else:
                    log.info(f"[ImmoScout24] Page {page}: {len(listings)} listings via __INITIAL_STATE__")

                for item in listings:
                    try:
                        listing = item.get('listing', item)
                        eid = str(item.get('id', listing.get('id', '')))
                        if not eid:
                            continue

                        loc = listing.get('localization', {})
                        fr = loc.get('fr', loc.get('de', loc.get('primary', {})))
                        title = ''
                        description = ''
                        if isinstance(fr, dict):
                            text = fr.get('text', fr)
                            if isinstance(text, dict):
                                title = text.get('title', '')
                                description = text.get('description', '')
                            else:
                                title = fr.get('title', '')

                        prices = listing.get('prices', {})
                        price = None
                        if transaction == 'achat':
                            bp = prices.get('buy', {})
                            price = bp.get('price') if isinstance(bp, dict) else None
                        else:
                            rp = prices.get('rent', {})
                            price = (rp.get('gross') or rp.get('net')) if isinstance(rp, dict) else None
                        if price is None:
                            for key in ['buy', 'rent']:
                                p = prices.get(key, {})
                                if isinstance(p, dict) and p.get('price'):
                                    price = p['price']
                                    break

                        chars = listing.get('characteristics', {})
                        rooms = chars.get('numberOfRooms')
                        surface = chars.get('livingSpace') or chars.get('totalFloorSpace')
                        floor = chars.get('floor')

                        addr = listing.get('address', {})
                        locality = addr.get('locality', city)
                        postal = addr.get('postalCode')
                        geo = addr.get('geoCoordinates', {})
                        lat = geo.get('latitude')
                        lon = geo.get('longitude')
                        address_str = f"{postal} {locality}" if postal else locality

                        imgs = []
                        attachments = fr.get('attachments', []) if isinstance(fr, dict) else []
                        for att in (attachments or [])[:5]:
                            if isinstance(att, dict):
                                img_url = att.get('url', att.get('file', ''))
                                if img_url:
                                    imgs.append(img_url)

                        features = []
                        if chars.get('hasParking'):
                            features.append('parking')
                        if chars.get('hasBalcony'):
                            features.append('balcon')
                        if chars.get('hasGarden'):
                            features.append('jardin')
                        if chars.get('hasElevator') or chars.get('hasLift'):
                            features.append('ascenseur')
                        if chars.get('isQuiet'):
                            features.append('calme')

                        results.append(_make_property(
                            external_id=f"is24-{eid}", source='ImmoScout24',
                            source_url=f"https://www.immoscout24.ch/fr/annonce/{tx}/{eid}",
                            title=title, description=description[:500],
                            property_type=_guess_type(title + ' ' + ' '.join(listing.get('categories', []))),
                            transaction=transaction,
                            price=price, rooms=rooms, surface=surface, floor=floor,
                            address=address_str, city=locality,
                            canton=CITY_CANTONS.get(locality.lower(), ''),
                            postal_code=postal, latitude=lat, longitude=lon,
                            features=features, images=imgs,
                            published_at=listing.get('meta', {}).get('createdAt'),
                        ))
                    except Exception as e:
                        log.debug(f"[ImmoScout24] Item parse error: {e}")
                        continue

            # Fallback: try __NEXT_DATA__
            elif not state:
                nd = _extract_next_data(html)
                if nd:
                    items = []
                    try:
                        items = nd['props']['pageProps']['listings']
                    except (KeyError, TypeError):
                        try:
                            items = nd['props']['pageProps']['resultList']['items']
                        except (KeyError, TypeError):
                            pass

                    log.info(f"[ImmoScout24] Page {page}: {len(items)} items via __NEXT_DATA__")
                    for item in items:
                        try:
                            listing = item.get('listing', item)
                            lid = listing.get('id', item.get('id', ''))
                            addr = listing.get('address', {}) or {}
                            chars = listing.get('characteristics', {}) or {}
                            prices = listing.get('prices', {}) or {}
                            price_val = None
                            if isinstance(prices, dict):
                                price_val = (prices.get('rent', {}).get('gross') or
                                             prices.get('buy', {}).get('price') or
                                             prices.get('value'))

                            imgs = []
                            for img in (listing.get('images') or listing.get('pictures') or [])[:5]:
                                if isinstance(img, str):
                                    imgs.append(img)
                                elif isinstance(img, dict):
                                    imgs.append(img.get('url') or img.get('src') or '')
                            imgs = [u for u in imgs if u]

                            results.append(_make_property(
                                external_id=f"is24-{lid}", source='ImmoScout24',
                                source_url=f"https://www.immoscout24.ch/fr/d/{lid}",
                                title=listing.get('title', ''), description='',
                                property_type=_guess_type(listing.get('propertyType', listing.get('title', ''))),
                                transaction=transaction,
                                price=_clean_price(price_val),
                                rooms=chars.get('numberOfRooms'),
                                surface=chars.get('livingSpace'),
                                floor=chars.get('floor'),
                                address=f"{addr.get('street', '')} {addr.get('postalCode', '')} {addr.get('locality', '')}".strip(),
                                city=addr.get('locality', city),
                                canton=addr.get('region', CITY_CANTONS.get(city.lower(), '')),
                                postal_code=str(addr.get('postalCode', '')) or None,
                                latitude=None, longitude=None,
                                features=[], images=imgs,
                                published_at=listing.get('publishDate'),
                            ))
                        except Exception as e:
                            log.debug(f"[ImmoScout24] __NEXT_DATA__ item error: {e}")
                else:
                    log.warning(f"[ImmoScout24] Page {page}: No structured data found (HTML len={len(html)})")

        except Exception as e:
            log.error(f"[ImmoScout24] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[ImmoScout24] Total: {len(results)} listings")
    return results


# ============================================================
# HOMEGATE — via __NEXT_DATA__ or __INITIAL_STATE__
# ============================================================

def scrape_homegate(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Homegate] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = _slugify(city)

    for page in range(1, max_pages + 1):
        try:
            url = f"https://www.homegate.ch/{tx}/real-estate/city-{slug}/matching-list?ep={page}"
            status, html = _get(url, headers={'Accept-Language': 'fr-CH,fr;q=0.9,de-CH;q=0.8'}, use_sb=True)
            log.info(f"[Homegate] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            parsed = False

            # Method 1: __NEXT_DATA__
            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    items = nd['props']['pageProps']['resultList'] or []
                except (KeyError, TypeError):
                    try:
                        items = nd['props']['pageProps']['listings'] or []
                    except (KeyError, TypeError):
                        # Try deeper nesting
                        try:
                            pp = nd['props']['pageProps']
                            for key in pp:
                                val = pp[key]
                                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                                    if 'id' in val[0] or 'listingId' in val[0]:
                                        items = val
                                        break
                        except (KeyError, TypeError):
                            pass

                if items:
                    log.info(f"[Homegate] Page {page}: {len(items)} items via __NEXT_DATA__")
                    parsed = True
                    for item in items:
                        try:
                            eid = str(item.get('id', item.get('listingId', '')))
                            if not eid:
                                continue
                            price = item.get('price', item.get('priceFormatted'))
                            rooms = item.get('numberOfRooms', item.get('rooms'))
                            surface = item.get('livingSpace', item.get('surfaceLiving', item.get('area')))
                            addr = item.get('address', {}) if isinstance(item.get('address'), dict) else {}
                            addr_str = f"{addr.get('postalCode', '')} {addr.get('locality', city)}".strip() if addr else city
                            locality = addr.get('locality', city) if addr else city

                            imgs = []
                            for img in (item.get('images') or item.get('pictures') or [])[:5]:
                                if isinstance(img, str):
                                    imgs.append(img)
                                elif isinstance(img, dict):
                                    imgs.append(img.get('url') or img.get('src') or '')
                            imgs = [u for u in imgs if u]

                            results.append(_make_property(
                                external_id=f"hg-{eid}", source='Homegate',
                                source_url=f"https://www.homegate.ch/{tx}/listing/{eid}",
                                title=item.get('title', ''),
                                description=item.get('description', ''),
                                property_type=_guess_type(item.get('title', '') + ' ' + str(item.get('propertyType', ''))),
                                transaction=transaction,
                                price=_clean_price(price), rooms=rooms, surface=surface, floor=None,
                                address=addr_str, city=locality,
                                canton=CITY_CANTONS.get(locality.lower(), ''),
                                postal_code=addr.get('postalCode') if addr else _extract_postal(addr_str),
                                latitude=None, longitude=None,
                                features=[], images=imgs, published_at=None,
                            ))
                        except Exception as e:
                            log.debug(f"[Homegate] __NEXT_DATA__ item error: {e}")

            # Method 2: __INITIAL_STATE__
            if not parsed:
                state = _extract_initial_state(html)
                if state:
                    listings = []
                    for path in [['listing', 'items'], ['results', 'items'], ['searchResults', 'listings']]:
                        try:
                            obj = state
                            for key in path:
                                obj = obj[key]
                            listings = obj
                            break
                        except (KeyError, TypeError):
                            continue

                    if listings:
                        log.info(f"[Homegate] Page {page}: {len(listings)} items via __INITIAL_STATE__")
                        parsed = True
                        for item in listings:
                            try:
                                eid = str(item.get('id', ''))
                                if not eid:
                                    continue
                                price = item.get('price')
                                rooms = item.get('numberOfRooms')
                                surface = item.get('livingSpace')
                                address = item.get('address', {})
                                addr_str = f"{address.get('postalCode', '')} {address.get('locality', city)}".strip()

                                imgs = []
                                for img in (item.get('images') or [])[:5]:
                                    if isinstance(img, str):
                                        imgs.append(img)
                                    elif isinstance(img, dict):
                                        imgs.append(img.get('url') or img.get('src') or '')
                                imgs = [u for u in imgs if u]

                                results.append(_make_property(
                                    external_id=f"hg-{eid}", source='Homegate',
                                    source_url=f"https://www.homegate.ch/{tx}/listing/{eid}",
                                    title=item.get('title', ''), description='',
                                    property_type=_guess_type(item.get('title', '') + ' ' + str(item.get('propertyType', ''))),
                                    transaction=transaction,
                                    price=price, rooms=rooms, surface=surface, floor=None,
                                    address=addr_str, city=address.get('locality', city),
                                    canton=CITY_CANTONS.get(address.get('locality', city).lower(), ''),
                                    postal_code=address.get('postalCode'),
                                    latitude=None, longitude=None,
                                    features=[], images=imgs, published_at=None,
                                ))
                            except Exception as e:
                                log.debug(f"[Homegate] __INITIAL_STATE__ item error: {e}")

            # Method 3: HTML fallback
            if not parsed:
                log.info(f"[Homegate] Page {page}: Trying HTML parsing fallback")
                soup = BeautifulSoup(html, 'html.parser')
                cards = soup.select('[data-test="result-list-item"], article, .listing-card, [class*="ResultList"] > div')
                log.info(f"[Homegate] Page {page}: {len(cards)} cards via HTML")
                for card in cards:
                    try:
                        link_el = card.select_one('a[href]')
                        href = link_el.get('href', '') if link_el else ''
                        if href.startswith('/'):
                            href = 'https://www.homegate.ch' + href
                        eid_match = re.search(r'/(\d+)', href)
                        eid = eid_match.group(1) if eid_match else ''
                        if not eid:
                            continue

                        card_text = card.get_text(' ', strip=True)
                        # Price: handle "CHF 1 290 000" or "CHF 1'290'000" or spans
                        price = None
                        price_match = re.search(r"CHF\s*([\d\s''.,\u2019]+)", card_text)
                        if not price_match:
                            price_match = re.search(r"([\d\s''.,\u2019]+)\s*CHF", card_text)
                        if price_match:
                            # Remove all whitespace/separators, keep digits
                            raw = price_match.group(1).replace(' ', '').replace('\u2019', '').replace("'", '').replace('.', '').replace(',', '')
                            digits = re.findall(r'\d+', raw)
                            if digits:
                                num = int(''.join(digits))
                                # Sanity check: real estate prices > 100
                                price = num if num > 100 else None

                        rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pi[èe]ce|Zimmer|room|½)', card_text, re.IGNORECASE)
                        rooms = _clean_rooms(rooms_match.group(1)) if rooms_match else None

                        surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                        surface = int(surface_match.group(1)) if surface_match else None

                        title_el = card.select_one('h2, h3, [class*="title"]')
                        title = title_el.get_text(strip=True) if title_el else f"{rooms or '?'} pièces"

                        imgs = []
                        for img_el in card.select('img[src], img[data-src]'):
                            src = img_el.get('data-src') or img_el.get('src') or ''
                            if src and not src.startswith('data:') and 'placeholder' not in src.lower():
                                if src.startswith('//'):
                                    src = 'https:' + src
                                imgs.append(src)
                        imgs = imgs[:5]

                        results.append(_make_property(
                            external_id=f"hg-{eid}", source='Homegate',
                            source_url=href, title=title, description='',
                            property_type=_guess_type(card_text), transaction=transaction,
                            price=price, rooms=rooms, surface=surface, floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=imgs, published_at=None,
                        ))
                    except Exception as e:
                        log.debug(f"[Homegate] HTML card error: {e}")

        except Exception as e:
            log.error(f"[Homegate] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[Homegate] Total: {len(results)} listings")
    return results


# ============================================================
# COMPARIS — via __NEXT_DATA__ (confirmed working)
# ============================================================

def scrape_comparis(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Comparis] Searching {city} ({transaction})")
    results = []
    deal_type = 10 if transaction == 'location' else 20

    for page in range(1, max_pages + 1):
        try:
            request_obj = json.dumps({
                "DealType": deal_type,
                "LocationSearchString": city,
                "Sort": 3,  # Newest first
                "Page": page,
            }, separators=(',', ':'))
            url = f"https://www.comparis.ch/immobilien/result/list?requestobject={quote(request_obj)}"
            status, html = _get(url, use_sb=True)
            log.info(f"[Comparis] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    items = nd['props']['pageProps']['initialResultData']['resultItems']
                except (KeyError, TypeError):
                    try:
                        items = nd['props']['pageProps']['searchResults']
                    except (KeyError, TypeError):
                        pass

                log.info(f"[Comparis] Page {page}: {len(items)} items via __NEXT_DATA__")

                for item in items:
                    try:
                        aid = str(item.get('AdId', item.get('id', '')))
                        if not aid:
                            continue

                        title = item.get('Title', item.get('title', ''))
                        price = _clean_price(item.get('Price', item.get('price')))
                        addr_parts = item.get('Address', [])
                        address = ', '.join(addr_parts) if isinstance(addr_parts, list) else str(addr_parts)
                        locality = addr_parts[-1] if isinstance(addr_parts, list) and addr_parts else city
                        # Parse "1000 Lausanne" pattern from locality
                        loc_match = re.match(r'(\d{4})\s+(.*)', str(locality))
                        postal = loc_match.group(1) if loc_match else _extract_postal(address)
                        loc_city = loc_match.group(2) if loc_match else city

                        area = item.get('AreaValue', item.get('area'))
                        rooms = None
                        essential = item.get('EssentialInformation', [])
                        if isinstance(essential, list):
                            for info in essential:
                                info_str = str(info)
                                r_match = re.search(r'([\d.]+)\s*(?:pièce|Zimmer|room)', info_str, re.IGNORECASE)
                                if r_match:
                                    rooms = _clean_rooms(r_match.group(1))
                                    break

                        imgs = []
                        img_url = item.get('ImageUrl', item.get('imageUrl', ''))
                        if img_url:
                            imgs.append(img_url)

                        detail_url = item.get('DetailUrl', item.get('detailUrl', item.get('url', '')))
                        if detail_url and detail_url.startswith('/'):
                            detail_url = 'https://www.comparis.ch' + detail_url
                        # Fallback: construct URL from AdId
                        if not detail_url and aid:
                            detail_url = f"https://www.comparis.ch/immobilien/marktplatz/details/show/{aid}"

                        results.append(_make_property(
                            external_id=f"comp-{aid}", source='Comparis',
                            source_url=detail_url, title=title, description='',
                            property_type=_guess_type(item.get('PropertyTypeText', title)),
                            transaction=transaction,
                            price=price, rooms=rooms, surface=_clean_surface(area),
                            floor=None, address=address, city=loc_city,
                            canton=CITY_CANTONS.get(loc_city.lower(), ''),
                            postal_code=postal, latitude=None, longitude=None,
                            features=[], images=imgs,
                            published_at=item.get('Date'),
                        ))
                    except Exception as e:
                        log.debug(f"[Comparis] Item error: {e}")
            else:
                # HTML fallback
                soup = BeautifulSoup(html, 'html.parser')
                cards = soup.select('[class*="ListItem"], [class*="result-item"], article')
                log.info(f"[Comparis] Page {page}: {len(cards)} cards via HTML")
                for card in cards:
                    try:
                        title_el = card.select_one('h3, h2, [class*="title"]')
                        title = title_el.get_text(strip=True) if title_el else ''
                        price_el = card.select_one('[class*="price"]')
                        price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                        link_el = card.select_one('a[href]')
                        href = link_el.get('href', '') if link_el else ''
                        if href.startswith('/'):
                            href = 'https://www.comparis.ch' + href
                        if title or price:
                            results.append(_make_property(
                                external_id=f"comp-{hash(title + href)}", source='Comparis',
                                source_url=href, title=title, description='',
                                property_type=_guess_type(title), transaction=transaction,
                                price=price, rooms=_clean_rooms(title), surface=None, floor=None,
                                address=city, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=None, latitude=None, longitude=None,
                                features=[], images=[], published_at=None,
                            ))
                    except Exception:
                        pass

        except Exception as e:
            log.error(f"[Comparis] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[Comparis] Total: {len(results)} listings")
    return results


# ============================================================
# ANIBIS — via SMG internal API
# ============================================================

def scrape_anibis(city="Lausanne", transaction="location", max_pages=2):
    """Scrape Anibis via internal API (SMG platform)."""
    log.info(f"[Anibis] Searching {city} ({transaction})")
    results = []

    # Anibis search API endpoints (Scout24 group)
    tx_slug = "louer" if transaction == "location" else "acheter"
    api_urls = [
        f"https://api.anibis.ch/v4/fr/search/listings?cid=12&q={quote(city)}&fcid=0&fts=0&nrs=20&pi={{page}}",
        f"https://www.anibis.ch/api/search?cid=immobilier&q={quote(city)}&page={{page}}",
    ]

    for page in range(1, max_pages + 1):
        found_api = False
        for api_template in api_urls:
            api_url = api_template.format(page=page)
            try:
                status, data = _get_json(api_url)
                if status == 200 and data:
                    items = data.get('listings', data.get('items', data.get('results', [])))
                    if isinstance(items, list) and items:
                        log.info(f"[Anibis] Page {page}: {len(items)} items via API")
                        found_api = True
                        for item in items:
                            try:
                                eid = str(item.get('id', item.get('listingId', '')))
                                if not eid:
                                    continue
                                title = item.get('title', item.get('subject', ''))
                                price = _clean_price(item.get('price', item.get('priceFormatted')))
                                location = item.get('location', item.get('address', ''))
                                if isinstance(location, dict):
                                    loc_str = location.get('name', location.get('city', city))
                                else:
                                    loc_str = str(location) if location else city

                                imgs = []
                                for img in (item.get('images', item.get('pictures', [])) or [])[:5]:
                                    if isinstance(img, str):
                                        imgs.append(img)
                                    elif isinstance(img, dict):
                                        imgs.append(img.get('url', img.get('thumbUrl', img.get('mediumUrl', ''))))
                                imgs = [u for u in imgs if u]

                                detail = item.get('url', item.get('detailUrl', ''))
                                if detail and not detail.startswith('http'):
                                    detail = 'https://www.anibis.ch' + detail

                                results.append(_make_property(
                                    external_id=f"anibis-{eid}", source='Anibis',
                                    source_url=detail, title=title, description=item.get('body', ''),
                                    property_type=_guess_type(title), transaction=transaction,
                                    price=price, rooms=_clean_rooms(title),
                                    surface=None, floor=None,
                                    address=loc_str, city=city,
                                    canton=CITY_CANTONS.get(city.lower(), ''),
                                    postal_code=_extract_postal(loc_str),
                                    latitude=None, longitude=None,
                                    features=[], images=imgs,
                                    published_at=item.get('date', item.get('createdAt')),
                                ))
                            except Exception as e:
                                log.debug(f"[Anibis] Item error: {e}")
                        break
            except Exception as e:
                log.debug(f"[Anibis] API {api_url[:60]} error: {e}")

        # Fallback: HTML parsing
        if not found_api:
            url = f"https://www.anibis.ch/fr/q/immobilier-{quote(city.lower())}-appartements-{tx_slug}/?page={page}"
            status, html = _get(url)
            log.info(f"[Anibis] Page {page}: HTTP {status}, len={len(html)}, trying HTML")

            if status == 200:
                # Try __NEXT_DATA__
                nd = _extract_next_data(html)
                if nd:
                    try:
                        items = nd['props']['pageProps']['listings'] or []
                        log.info(f"[Anibis] Page {page}: {len(items)} items via __NEXT_DATA__")
                        for item in items:
                            eid = str(item.get('id', ''))
                            if not eid:
                                continue
                            results.append(_make_property(
                                external_id=f"anibis-{eid}", source='Anibis',
                                source_url=item.get('url', ''),
                                title=item.get('title', ''), description='',
                                property_type=_guess_type(item.get('title', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('price')),
                                rooms=_clean_rooms(item.get('title', '')),
                                surface=None, floor=None,
                                address=city, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=None, latitude=None, longitude=None,
                                features=[], images=[],
                                published_at=item.get('date'),
                            ))
                    except (KeyError, TypeError):
                        pass
                else:
                    # HTML parsing
                    soup = BeautifulSoup(html, 'html.parser')
                    cards = soup.select('[class*="ListItem"], [class*="listing"], article, [data-testid*="listing"]')
                    log.info(f"[Anibis] Page {page}: {len(cards)} cards via HTML")
                    for card in cards:
                        try:
                            title_el = card.select_one('h3, h2, [class*="title"], [class*="Title"]')
                            title = title_el.get_text(strip=True) if title_el else ''
                            price_el = card.select_one('[class*="price"], [class*="Price"]')
                            price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                            link_el = card.select_one('a[href]')
                            href = link_el.get('href', '') if link_el else ''
                            if href.startswith('/'):
                                href = 'https://www.anibis.ch' + href
                            eid = re.search(r'/(\d+)', href)
                            if title or price:
                                results.append(_make_property(
                                    external_id=f"anibis-{eid.group(1) if eid else hash(title)}",
                                    source='Anibis', source_url=href,
                                    title=title, description='',
                                    property_type=_guess_type(title), transaction=transaction,
                                    price=price, rooms=_clean_rooms(title),
                                    surface=None, floor=None,
                                    address=city, city=city,
                                    canton=CITY_CANTONS.get(city.lower(), ''),
                                    postal_code=None, latitude=None, longitude=None,
                                    features=[], images=[], published_at=None,
                                ))
                        except Exception:
                            pass

        time.sleep(1)

    log.info(f"[Anibis] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOBILIER.CH — HTML parsing with multi-method
# ============================================================

def scrape_immobilier_ch(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Immobilier.ch] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = _slugify(city)

    for page in range(1, max_pages + 1):
        try:
            url = f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{slug}?page={page}"
            status, html = _get(url)
            log.info(f"[Immobilier.ch] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            # Try __NEXT_DATA__
            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    items = nd['props']['pageProps']['listings'] or []
                except (KeyError, TypeError):
                    pass
                if items:
                    log.info(f"[Immobilier.ch] Page {page}: {len(items)} via __NEXT_DATA__")
                    for item in items:
                        try:
                            eid = str(item.get('id', ''))
                            results.append(_make_property(
                                external_id=f"imch-{eid}", source='Immobilier.ch',
                                source_url=item.get('url', ''),
                                title=item.get('title', ''), description=item.get('description', ''),
                                property_type=_guess_type(item.get('title', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('price')),
                                rooms=_clean_rooms(item.get('rooms')),
                                surface=_clean_surface(item.get('surface')),
                                floor=None, address=item.get('address', city), city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=_extract_postal(item.get('address', '')),
                                latitude=None, longitude=None,
                                features=[], images=(item.get('images') or [])[:5],
                                published_at=item.get('date'),
                            ))
                        except Exception:
                            pass
                    continue

            # HTML parsing fallback
            soup = BeautifulSoup(html, 'html.parser')
            # immobilier.ch uses .filter-item for listing cards — be specific
            cards = soup.select('.filter-item, .item-listing, .object-list .object-item')
            if not cards:
                # broader fallback but filter aggressively
                cards = soup.select('article, .property-item')
            log.info(f"[Immobilier.ch] Page {page}: {len(cards)} cards via HTML")

            for card in cards:
                try:
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    # Skip non-listing links (javascript:, city-guide, etc.)
                    if not href or href.startswith('javascript:') or 'city-guide' in href or 'guide' in href:
                        continue
                    if href.startswith('/'):
                        href = 'https://www.immobilier.ch' + href
                    # Must be an actual property URL
                    if '/fr/' not in href and '/de/' not in href and 'immobilier.ch' not in href:
                        continue

                    title_el = card.select_one('h3, h2, .title, .item-title')
                    title = title_el.get_text(strip=True) if title_el else ''
                    # Skip navigation/filter elements
                    if title and any(skip in title.lower() for skip in ['critère', 'recherche', 'filtre', 'aperçu', 'city guide']):
                        continue

                    price_el = card.select_one('.price, .item-price, [class*="price"]')
                    price_text = price_el.get_text(strip=True) if price_el else ''
                    price = _clean_price(price_text)

                    # Fallback: extract price from title or card text
                    card_text = card.get_text(' ', strip=True)
                    if not price:
                        # Look for CHF patterns in card text
                        pm = re.search(r"CHF\s*([\d''.\s\u2019]+)", card_text)
                        if pm:
                            raw = pm.group(1).replace(' ', '').replace('\u2019', '').replace("'", '').replace('.', '').replace('-', '')
                            digits = re.findall(r'\d+', raw)
                            if digits:
                                num = int(''.join(digits))
                                price = num if num > 100 else None
                    if not price:
                        pm = re.search(r"CHF\s*([\d''.\s\u2019]+)", title)
                        if pm:
                            raw = pm.group(1).replace(' ', '').replace('\u2019', '').replace("'", '').replace('.', '').replace('-', '')
                            digits = re.findall(r'\d+', raw)
                            if digits:
                                num = int(''.join(digits))
                                price = num if num > 100 else None

                    # Must have either a price or a title to be a real listing
                    if not price and not title:
                        continue

                    # Extract rooms from card text if not in title
                    rooms = _clean_rooms(title)
                    if not rooms:
                        rm = re.search(r'(\d+[.,]?5?)\s*(?:pi[èe]ce|½)', card_text, re.IGNORECASE)
                        rooms = _clean_rooms(rm.group(1)) if rm else None

                    # Extract surface from card text
                    surface = _clean_surface(card_text)

                    addr_el = card.select_one('.address, .location, [class*="location"], [class*="address"]')
                    address = addr_el.get_text(strip=True) if addr_el else ''
                    eid = re.search(r'/(\d+)', href)

                    imgs = []
                    for img_el in card.select('img[src], img[data-src]'):
                        src = img_el.get('data-src') or img_el.get('src') or ''
                        if src and not src.startswith('data:') and 'placeholder' not in src.lower():
                            if src.startswith('//'):
                                src = 'https:' + src
                            imgs.append(src)

                    # Clean title: remove price if it's the whole title
                    clean_title = title
                    if clean_title.startswith('CHF'):
                        # Title is just the price — try to get a better one
                        t2 = card.select_one('.item-description, [class*="desc"], p')
                        if t2:
                            clean_title = t2.get_text(strip=True)[:100]

                    results.append(_make_property(
                        external_id=f"imch-{eid.group(1) if eid else hash(title + address)}",
                        source='Immobilier.ch', source_url=href,
                        title=clean_title, description='',
                        property_type=_guess_type(card_text), transaction=transaction,
                        price=price, rooms=rooms,
                        surface=surface, floor=None,
                        address=address or city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=None, longitude=None,
                        features=[], images=imgs[:5], published_at=None,
                    ))
                except Exception:
                    pass

        except Exception as e:
            log.error(f"[Immobilier.ch] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[Immobilier.ch] Total: {len(results)} listings")
    return results


# ============================================================
# ACHETER-LOUER — HTML parsing with multi-method
# ============================================================

def scrape_acheter_louer(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Acheter-Louer] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"

    for page in range(1, max_pages + 1):
        try:
            # Try multiple URL patterns (acheter-louer.ch changes their routing)
            urls_to_try = [
                f"https://www.acheter-louer.ch/fr/{tx}/appartement/{city.lower()}?page={page}",
                f"https://www.acheter-louer.ch/{tx}/{city.lower()}?page={page}",
                f"https://www.acheter-louer.ch/fr/{tx}/{city.lower()}?page={page}",
            ]
            status, html = 0, ''
            for try_url in urls_to_try:
                status, html = _get(try_url, use_sb=True)
                if status == 200:
                    url = try_url
                    break
            if status != 200:
                url = urls_to_try[0]
            log.info(f"[Acheter-Louer] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            # Try __NEXT_DATA__
            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    items = nd['props']['pageProps']['listings'] or []
                except (KeyError, TypeError):
                    pass
                if items:
                    log.info(f"[Acheter-Louer] Page {page}: {len(items)} via __NEXT_DATA__")
                    for item in items:
                        try:
                            eid = str(item.get('id', ''))
                            results.append(_make_property(
                                external_id=f"al-{eid}", source='Acheter-Louer',
                                source_url=item.get('url', ''),
                                title=item.get('title', ''), description='',
                                property_type=_guess_type(item.get('title', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('price')),
                                rooms=_clean_rooms(item.get('rooms')),
                                surface=_clean_surface(item.get('surface')),
                                floor=None, address=city, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=None, latitude=None, longitude=None,
                                features=[], images=(item.get('images') or [])[:5],
                                published_at=item.get('date'),
                            ))
                        except Exception:
                            pass
                    continue

            # HTML parsing
            soup = BeautifulSoup(html, 'html.parser')
            cards = soup.select('.property-item, .listing-card, article, [class*="result"], [class*="listing"]')
            log.info(f"[Acheter-Louer] Page {page}: {len(cards)} cards via HTML")

            for card in cards:
                try:
                    title_el = card.select_one('h3, h2, .title, [class*="title"]')
                    title = title_el.get_text(strip=True) if title_el else ''
                    price_el = card.select_one('[class*="price"], .price')
                    price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.acheter-louer.ch' + href

                    if title or price:
                        imgs = []
                        for img_el in card.select('img[src], img[data-src]'):
                            src = img_el.get('data-src') or img_el.get('src') or ''
                            if src and not src.startswith('data:'):
                                if src.startswith('//'):
                                    src = 'https:' + src
                                imgs.append(src)

                        results.append(_make_property(
                            external_id=f"al-{hash(title + href)}", source='Acheter-Louer',
                            source_url=href, title=title, description='',
                            property_type=_guess_type(title), transaction=transaction,
                            price=price, rooms=_clean_rooms(title),
                            surface=None, floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=imgs[:5], published_at=None,
                        ))
                except Exception:
                    pass

        except Exception as e:
            log.error(f"[Acheter-Louer] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[Acheter-Louer] Total: {len(results)} listings")
    return results


# ============================================================
# PROPERSTAR — HTML + JSON-LD parsing
# ============================================================

def scrape_properstar(city="Lausanne", transaction="location", max_pages=1):
    log.info(f"[Properstar] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = _slugify(city)

    try:
        url = f"https://www.properstar.ch/switzerland/{slug}/{tx}/apartment"
        status, html = _get(url, use_sb=True)
        log.info(f"[Properstar] HTTP {status}, len={len(html)}")

        if status != 200:
            return results

        # Try __NEXT_DATA__
        nd = _extract_next_data(html)
        if nd:
            items = []
            try:
                items = nd['props']['pageProps']['listings'] or []
            except (KeyError, TypeError):
                try:
                    items = nd['props']['pageProps']['properties'] or []
                except (KeyError, TypeError):
                    pass
            if items:
                log.info(f"[Properstar] {len(items)} via __NEXT_DATA__")
                for item in items:
                    try:
                        eid = str(item.get('id', ''))
                        results.append(_make_property(
                            external_id=f"ps-{eid}", source='Properstar',
                            source_url=item.get('url', item.get('detailUrl', '')),
                            title=item.get('title', ''), description=item.get('description', ''),
                            property_type=_guess_type(item.get('propertyType', item.get('title', ''))),
                            transaction=transaction,
                            price=_clean_price(item.get('price')),
                            rooms=_clean_rooms(item.get('bedrooms', item.get('rooms'))),
                            surface=_clean_surface(item.get('area', item.get('surface'))),
                            floor=None, address=item.get('address', city), city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=_extract_postal(item.get('address', '')),
                            latitude=item.get('latitude'), longitude=item.get('longitude'),
                            features=[], images=(item.get('images') or [])[:5],
                            published_at=item.get('publishedAt'),
                        ))
                    except Exception:
                        pass
                return results

        # Try JSON-LD
        soup = BeautifulSoup(html, 'html.parser')
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string)
                if isinstance(ld, list):
                    for item in ld:
                        if item.get('@type') in ['RealEstateListing', 'Product', 'Offer']:
                            _parse_jsonld_listing(item, results, transaction, city)
                elif isinstance(ld, dict):
                    if ld.get('@type') in ['RealEstateListing', 'ItemList']:
                        items = ld.get('itemListElement', [ld])
                        for item in items:
                            _parse_jsonld_listing(item, results, transaction, city)
            except Exception:
                pass

        # HTML fallback
        if not results:
            cards = soup.select('.listing-card, .property-card, article, [class*="listing"], [class*="property"]')
            log.info(f"[Properstar] {len(cards)} cards via HTML")
            for card in cards:
                try:
                    title_el = card.select_one('h3, h2, [class*="title"]')
                    title = title_el.get_text(strip=True) if title_el else ''
                    price_el = card.select_one('[class*="price"]')
                    price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.properstar.ch' + href
                    if title or price:
                        imgs = []
                        for img_el in card.select('img[src], img[data-src]'):
                            src = img_el.get('data-src') or img_el.get('src') or ''
                            if src and not src.startswith('data:'):
                                imgs.append(src if src.startswith('http') else 'https:' + src if src.startswith('//') else src)

                        results.append(_make_property(
                            external_id=f"ps-{hash(title + href)}", source='Properstar',
                            source_url=href, title=title, description='',
                            property_type=_guess_type(title), transaction=transaction,
                            price=price, rooms=_clean_rooms(title),
                            surface=None, floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=imgs[:5], published_at=None,
                        ))
                except Exception:
                    pass

    except Exception as e:
        log.error(f"[Properstar] Error: {e}")

    log.info(f"[Properstar] Total: {len(results)} listings")
    return results


def _parse_jsonld_listing(item, results, transaction, city):
    """Parse a JSON-LD real estate listing."""
    name = item.get('name', item.get('title', ''))
    price = item.get('offers', {}).get('price') if isinstance(item.get('offers'), dict) else item.get('price')
    geo = item.get('geo', {})
    addr = item.get('address', {})
    imgs = []
    if item.get('image'):
        if isinstance(item['image'], str):
            imgs = [item['image']]
        elif isinstance(item['image'], list):
            imgs = item['image'][:5]

    results.append(_make_property(
        external_id=f"ps-{hash(name + str(price))}", source='Properstar',
        source_url=item.get('url', ''),
        title=name, description=item.get('description', ''),
        property_type=_guess_type(name), transaction=transaction,
        price=_clean_price(price), rooms=None, surface=None, floor=None,
        address=addr.get('streetAddress', city) if isinstance(addr, dict) else city,
        city=addr.get('addressLocality', city) if isinstance(addr, dict) else city,
        canton=CITY_CANTONS.get(city.lower(), ''),
        postal_code=addr.get('postalCode') if isinstance(addr, dict) else None,
        latitude=geo.get('latitude'), longitude=geo.get('longitude'),
        features=[], images=imgs, published_at=None,
    ))


# ============================================================
# NEWHOME.CH — __NEXT_DATA__ / HTML parsing
# ============================================================

def scrape_newhome(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Newhome] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = _slugify(city)

    for page in range(1, max_pages + 1):
        try:
            url = f"https://www.newhome.ch/fr/{tx}/immobilier/{slug}/liste?page={page}"
            status, html = _get(url, use_sb=True)
            log.info(f"[Newhome] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            # Try __NEXT_DATA__
            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    pp = nd['props']['pageProps']
                    # Try common paths
                    for key in ['listings', 'properties', 'results', 'searchResults']:
                        if key in pp and isinstance(pp[key], list):
                            items = pp[key]
                            break
                    if not items:
                        # Try nested
                        for key in pp:
                            val = pp[key]
                            if isinstance(val, dict):
                                for subkey in ['items', 'listings', 'results']:
                                    if subkey in val and isinstance(val[subkey], list):
                                        items = val[subkey]
                                        break
                            if items:
                                break
                except (KeyError, TypeError):
                    pass

                if items:
                    log.info(f"[Newhome] Page {page}: {len(items)} via __NEXT_DATA__")
                    for item in items:
                        try:
                            eid = str(item.get('id', item.get('propertyId', '')))
                            addr = item.get('address', {}) if isinstance(item.get('address'), dict) else {}
                            results.append(_make_property(
                                external_id=f"nh-{eid}", source='Newhome',
                                source_url=item.get('url', item.get('detailUrl', '')),
                                title=item.get('title', ''), description=item.get('description', ''),
                                property_type=_guess_type(item.get('propertyType', item.get('title', ''))),
                                transaction=transaction,
                                price=_clean_price(item.get('price', item.get('rentGross'))),
                                rooms=_clean_rooms(item.get('rooms', item.get('numberOfRooms'))),
                                surface=_clean_surface(item.get('livingSpace', item.get('surface'))),
                                floor=None,
                                address=addr.get('street', city) if addr else city,
                                city=addr.get('city', addr.get('locality', city)) if addr else city,
                                canton=addr.get('canton', CITY_CANTONS.get(city.lower(), '')) if addr else CITY_CANTONS.get(city.lower(), ''),
                                postal_code=addr.get('zip', addr.get('postalCode')) if addr else None,
                                latitude=item.get('latitude', item.get('lat')),
                                longitude=item.get('longitude', item.get('lng')),
                                features=[], images=(item.get('images') or [])[:5],
                                published_at=item.get('publishDate', item.get('createdAt')),
                            ))
                        except Exception as e:
                            log.debug(f"[Newhome] Item error: {e}")
                    continue

            # HTML fallback
            soup = BeautifulSoup(html, 'html.parser')
            cards = soup.select('[class*="listing"], [class*="property"], article, .result-item')
            log.info(f"[Newhome] Page {page}: {len(cards)} cards via HTML")
            for card in cards:
                try:
                    title_el = card.select_one('h2, h3, [class*="title"]')
                    title = title_el.get_text(strip=True) if title_el else ''
                    price_el = card.select_one('[class*="price"]')
                    price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.newhome.ch' + href
                    eid = re.search(r'/(\d+)', href)
                    if title or price:
                        imgs = []
                        for img_el in card.select('img[src], img[data-src]'):
                            src = img_el.get('data-src') or img_el.get('src') or ''
                            if src and not src.startswith('data:'):
                                imgs.append(src if src.startswith('http') else 'https:' + src if src.startswith('//') else src)
                        results.append(_make_property(
                            external_id=f"nh-{eid.group(1) if eid else hash(title)}",
                            source='Newhome', source_url=href,
                            title=title, description='',
                            property_type=_guess_type(title), transaction=transaction,
                            price=price, rooms=_clean_rooms(title),
                            surface=None, floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=imgs[:5], published_at=None,
                        ))
                except Exception:
                    pass

        except Exception as e:
            log.error(f"[Newhome] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[Newhome] Total: {len(results)} listings")
    return results


# ============================================================
# TUTTI.CH — SMG API / __NEXT_DATA__
# ============================================================

def scrape_tutti(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Tutti] Searching {city} ({transaction})")
    results = []

    # Try API endpoints (tutti.ch changed their API structure)
    api_urls = [
        f"https://www.tutti.ch/api/v10/list.json?q=appartement&l={quote(city)}&c=immobilien&o=0&n=30",
        f"https://www.tutti.ch/api/v10/list.json?q=&l={quote(city)}&c=immobilien-wohnungen&o=0&n=30",
        f"https://www.tutti.ch/api/v14/list.json?q={quote(city)}&c=immobilien&o=0&n=30",
    ]

    for api_url in api_urls:
        try:
            status, data = _get_json(api_url)
            if status == 200 and data:
                items = data.get('items', data.get('listings', []))
                if isinstance(items, list) and items:
                    log.info(f"[Tutti] {len(items)} items via API")
                    for item in items:
                        try:
                            eid = str(item.get('id', item.get('listingId', '')))
                            if not eid:
                                continue
                            title = item.get('subject', item.get('title', ''))
                            body = item.get('body', item.get('description', ''))
                            price = _clean_price(item.get('price'))
                            location = item.get('location', {})
                            loc_name = location.get('name', city) if isinstance(location, dict) else city

                            imgs = []
                            for img in (item.get('images', item.get('pictures', [])) or [])[:5]:
                                if isinstance(img, str):
                                    imgs.append(img)
                                elif isinstance(img, dict):
                                    imgs.append(img.get('url', img.get('uri', '')))
                            imgs = [u for u in imgs if u]

                            detail = item.get('link', item.get('url', ''))
                            if detail and not detail.startswith('http'):
                                detail = 'https://www.tutti.ch' + detail

                            results.append(_make_property(
                                external_id=f"tutti-{eid}", source='Tutti',
                                source_url=detail, title=title, description=body,
                                property_type=_guess_type(title + ' ' + body),
                                transaction=transaction,
                                price=price, rooms=_clean_rooms(title),
                                surface=_clean_surface(body),
                                floor=None, address=loc_name, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=_extract_postal(loc_name),
                                latitude=None, longitude=None,
                                features=[], images=imgs,
                                published_at=item.get('timestamp', item.get('date')),
                            ))
                        except Exception as e:
                            log.debug(f"[Tutti] Item error: {e}")
                    break
        except Exception as e:
            log.debug(f"[Tutti] API error: {e}")

    # Fallback: page scraping
    if not results:
        try:
            url = f"https://www.tutti.ch/fr/li/toute-la-suisse/immobilier/appartements?q={quote(city)}"
            status, html = _get(url)
            log.info(f"[Tutti] HTML: HTTP {status}, len={len(html)}")

            if status == 200:
                nd = _extract_next_data(html)
                if nd:
                    items = []
                    try:
                        items = nd['props']['pageProps']['listings'] or []
                    except (KeyError, TypeError):
                        try:
                            pp = nd['props']['pageProps']
                            for key in pp:
                                if isinstance(pp[key], dict) and 'items' in pp[key]:
                                    items = pp[key]['items']
                                    break
                        except (KeyError, TypeError):
                            pass

                    log.info(f"[Tutti] {len(items)} via __NEXT_DATA__")
                    for item in items:
                        try:
                            eid = str(item.get('id', ''))
                            results.append(_make_property(
                                external_id=f"tutti-{eid}", source='Tutti',
                                source_url=item.get('url', ''),
                                title=item.get('subject', item.get('title', '')),
                                description=item.get('body', ''),
                                property_type=_guess_type(item.get('title', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('price')),
                                rooms=_clean_rooms(item.get('title', '')),
                                surface=None, floor=None,
                                address=city, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=None, latitude=None, longitude=None,
                                features=[], images=[],
                                published_at=item.get('date'),
                            ))
                        except Exception:
                            pass
        except Exception as e:
            log.error(f"[Tutti] Fallback error: {e}")

    log.info(f"[Tutti] Total: {len(results)} listings")
    return results


# ============================================================
# REALADVISOR — __NEXT_DATA__
# ============================================================

def scrape_realadvisor(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[RealAdvisor] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = _slugify(city)

    for page in range(1, max_pages + 1):
        try:
            # RealAdvisor URL patterns (updated April 2026)
            urls_to_try = [
                f"https://realadvisor.ch/fr/{tx}/ville-{slug}/appartement?page={page}",
                f"https://realadvisor.ch/fr/immobilier-a-{'vendre' if tx == 'acheter' else 'louer'}?city={quote(city)}&page={page}",
                f"https://realadvisor.ch/fr/{tx}/{slug}?page={page}",
            ]
            url = urls_to_try[0]  # Start with most likely
            status, html = 0, ''
            for try_url in urls_to_try:
                status, html = _get(try_url)
                if status == 200:
                    url = try_url
                    break
                log.info(f"[RealAdvisor] {try_url} → {status}, trying next...")

            if status != 200:
                log.warning(f"[RealAdvisor] All URL patterns failed for page {page}")
                break
            log.info(f"[RealAdvisor] Page {page}: HTTP {status}, len={len(html)}")

            if status != 200:
                break

            # Try __NEXT_DATA__
            nd = _extract_next_data(html)
            if nd:
                items = []
                try:
                    pp = nd['props']['pageProps']
                    for key in ['listings', 'properties', 'results', 'searchResults', 'data']:
                        if key in pp:
                            val = pp[key]
                            if isinstance(val, list):
                                items = val
                                break
                            elif isinstance(val, dict):
                                for subkey in ['items', 'listings', 'results', 'edges', 'nodes']:
                                    if subkey in val and isinstance(val[subkey], list):
                                        items = val[subkey]
                                        break
                        if items:
                            break
                except (KeyError, TypeError):
                    pass

                if items:
                    log.info(f"[RealAdvisor] Page {page}: {len(items)} via __NEXT_DATA__")
                    for item in items:
                        try:
                            # Handle GraphQL edge/node pattern
                            node = item.get('node', item)
                            eid = str(node.get('id', node.get('propertyId', '')))
                            if not eid:
                                continue

                            addr = node.get('address', {}) if isinstance(node.get('address'), dict) else {}
                            results.append(_make_property(
                                external_id=f"ra-{eid}", source='RealAdvisor',
                                source_url=node.get('url', node.get('slug', '')),
                                title=node.get('title', ''), description=node.get('description', ''),
                                property_type=_guess_type(node.get('propertyType', node.get('title', ''))),
                                transaction=transaction,
                                price=_clean_price(node.get('price', node.get('rentGross'))),
                                rooms=_clean_rooms(node.get('rooms', node.get('numberOfRooms'))),
                                surface=_clean_surface(node.get('livingSpace', node.get('surface'))),
                                floor=None,
                                address=addr.get('street', city) if addr else city,
                                city=addr.get('city', city) if addr else city,
                                canton=addr.get('canton', CITY_CANTONS.get(city.lower(), '')) if addr else CITY_CANTONS.get(city.lower(), ''),
                                postal_code=addr.get('postalCode') if addr else None,
                                latitude=node.get('latitude'), longitude=node.get('longitude'),
                                features=[], images=(node.get('images') or [])[:5],
                                published_at=node.get('publishDate'),
                            ))
                        except Exception as e:
                            log.debug(f"[RealAdvisor] Item error: {e}")
                    continue

            # JSON-LD fallback
            soup = BeautifulSoup(html, 'html.parser')
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    ld = json.loads(script.string)
                    if isinstance(ld, dict) and ld.get('@type') == 'ItemList':
                        for li in ld.get('itemListElement', []):
                            item = li.get('item', li)
                            results.append(_make_property(
                                external_id=f"ra-{hash(item.get('name', '') + str(item.get('url', '')))}",
                                source='RealAdvisor',
                                source_url=item.get('url', ''),
                                title=item.get('name', ''), description='',
                                property_type=_guess_type(item.get('name', '')),
                                transaction=transaction,
                                price=_clean_price(item.get('offers', {}).get('price')),
                                rooms=None, surface=None, floor=None,
                                address=city, city=city,
                                canton=CITY_CANTONS.get(city.lower(), ''),
                                postal_code=None, latitude=None, longitude=None,
                                features=[], images=[], published_at=None,
                            ))
                except Exception:
                    pass

            if not results:
                log.info(f"[RealAdvisor] Page {page}: No structured data found")

        except Exception as e:
            log.error(f"[RealAdvisor] Page {page} error: {e}")
            break

        time.sleep(1)

    log.info(f"[RealAdvisor] Total: {len(results)} listings")
    return results


# ============================================================
# MAIN — Scrape all portals
# ============================================================

def scrape_all(city="Lausanne", transaction="location"):
    """Scrape all portals for a given city and transaction type."""
    all_results = []

    # With residential proxy (PROXY_URL), all scrapers should work
    # Without proxy, only Comparis (via SB), Immobilier.ch, Flatfox work
    if PROXY_URL:
        scrapers = [
            ('Flatfox', scrape_flatfox),
            ('ImmoScout24', scrape_immoscout24),
            ('Homegate', scrape_homegate),
            ('Comparis', scrape_comparis),
            ('Anibis', scrape_anibis),
            ('Immobilier.ch', scrape_immobilier_ch),
            ('Acheter-Louer', scrape_acheter_louer),
            ('Newhome', scrape_newhome),
            ('Tutti', scrape_tutti),
            ('RealAdvisor', scrape_realadvisor),
        ]
    else:
        # No proxy — only run scrapers that work without it
        scrapers = [
            ('Comparis', scrape_comparis),
            ('Immobilier.ch', scrape_immobilier_ch),
            ('Flatfox', scrape_flatfox),
        ]

    for name, scraper in scrapers:
        try:
            results = scraper(city=city, transaction=transaction)
            all_results.extend(results)
            log.info(f"[{name}] ✓ {len(results)} results")
        except Exception as e:
            log.error(f"[{name}] FAILED: {e}")
        time.sleep(1)

    # Deduplicate by external_id
    seen = set()
    unique = []
    for p in all_results:
        key = p['external_id']
        if key not in seen:
            seen.add(key)
            unique.append(p)

    log.info(f"Total: {len(unique)} unique listings from {len(scrapers)} portals")
    return unique


def save_to_db(db, listings):
    """Save scraped listings to the properties table."""
    cur = db.cursor()
    saved = 0
    for p in listings:
        try:
            cur.execute("""
                INSERT INTO properties (
                    external_id, source, source_url, title, description,
                    property_type, transaction, price, currency, price_unit,
                    rooms, surface, floor, address, city, canton, postal_code,
                    latitude, longitude, features, images,
                    contact_name, contact_phone, contact_email,
                    published_at, scraped_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (external_id, source) DO UPDATE SET
                    price = EXCLUDED.price,
                    title = EXCLUDED.title,
                    is_active = TRUE,
                    scraped_at = NOW()
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
            saved += 1
        except Exception as e:
            log.debug(f"Save error for {p.get('external_id')}: {e}")

    db.commit()
    cur.close()
    log.info(f"Saved {saved}/{len(listings)} to database")
    return saved
