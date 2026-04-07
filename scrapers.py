"""
Lou Garou — Scrapers immobiliers suisses (v3 — ScrapingBee)
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')

SCRAPINGBEE_KEY = os.environ.get('SCRAPINGBEE_API_KEY', '')
SCRAPINGBEE_URL = 'https://app.scrapingbee.com/api/v1'

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
}


def _sb_get(url, render_js=False):
    """Fetch a URL via ScrapingBee. Returns (status_code, html_text)."""
    if not SCRAPINGBEE_KEY:
        log.warning("SCRAPINGBEE_API_KEY not set — falling back to direct request")
        try:
            r = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            }, timeout=15)
            return r.status_code, r.text
        except Exception as e:
            log.error(f"Direct request failed: {e}")
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
        r = requests.get(SCRAPINGBEE_URL, params=params, timeout=180)
        log.info(f"[ScrapingBee] {url[:60]}... → HTTP {r.status_code} ({len(r.text)} bytes)")
        return r.status_code, r.text
    except requests.exceptions.Timeout:
        log.error(f"[ScrapingBee] TIMEOUT fetching {url}")
        return 0, ''
    except Exception as e:
        log.error(f"[ScrapingBee] Error fetching {url}: {e}")
        return 0, ''


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


def _clean_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    text = str(val).replace('\u2019', "'").replace("'", "").replace(',', '').replace('.–', '').replace('.-', '')
    nums = re.findall(r'\d+', text)
    if nums:
        n = int(nums[0])
        return n if n > 0 else None
    return None


def _clean_rooms(text):
    if not text:
        return None
    text = str(text).replace('½', '.5')
    nums = re.findall(r'[\d.]+', text)
    return float(nums[0]) if nums else None


def _clean_surface(text):
    if not text:
        return None
    nums = re.findall(r'(\d+)\s*m', str(text))
    return int(nums[0]) if nums else None


# ============================================================
# HOMEGATE — via ScrapingBee + __NEXT_DATA__
# ============================================================

def scrape_homegate(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Homegate] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e')

    for page in range(1, max_pages + 1):
        url = f"https://www.homegate.ch/{tx}/real-estate/city-{slug}/matching-list?ep={page}"
        status, html = _sb_get(url, render_js=False)

        if status != 200:
            log.warning(f"[Homegate] Page {page}: HTTP {status}")
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('[data-test="result-list-item"]')
        log.info(f"[Homegate] Page {page}: {len(cards)} cards")

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
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'']+)", card_text)
                if not price_match:
                    price_match = re.search(r"([\d'']+)\s*(?:CHF|Fr|\.–|/mois|/m)", card_text)
                if price_match:
                    price = _clean_price(price_match.group(1))

                # Rooms: look for X.5 pièces or X½ or similar
                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pièce|piece|room|Zimmer|pi\.)', card_text)
                if not rooms_match:
                    rooms_match = re.search(r'(\d+[.,]5)\b', card_text)
                if rooms_match:
                    rooms = _clean_rooms(rooms_match.group(1))

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

                # Title: first meaningful text
                title = ''
                for el in card.select('h2, h3, [class*="title"], [class*="Title"]'):
                    t = el.get_text(strip=True)
                    if t and len(t) > 3:
                        title = t
                        break
                if not title:
                    title = f"{rooms or '?'} pièces" + (f", {surface} m²" if surface else '')

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
                    features=[], images=[], published_at=None,
                ))
            except Exception as e:
                log.debug(f"[Homegate] Card parse error: {e}")

        time.sleep(1)

    log.info(f"[Homegate] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOSCOUT24 — via ScrapingBee
# ============================================================

def scrape_immoscout(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[ImmoScout24] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e')

    for page in range(1, max_pages + 1):
        url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{slug}?pn={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            log.warning(f"[ImmoScout24] Page {page}: HTTP {status}")
            break

        # Try __NEXT_DATA__
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if match:
            try:
                data = json.loads(match.group(1))
                page_props = data.get('props', {}).get('pageProps', {})
                # ImmoScout24 nests data differently
                items = (page_props.get('listings', []) or
                         page_props.get('resultList', {}).get('items', []) or [])
                log.info(f"[ImmoScout24] Page {page}: {len(items)} items via __NEXT_DATA__")

                for item in items:
                    listing = item.get('listing', item)
                    lid = listing.get('id', item.get('id', ''))
                    addr = listing.get('address', {}) or {}
                    chars = listing.get('characteristics', {}) or {}
                    prices = listing.get('prices', {}) or {}

                    if isinstance(prices, dict):
                        price_val = (prices.get('rent', {}).get('gross') or
                                    prices.get('buy', {}).get('price') or
                                    prices.get('value'))
                    else:
                        price_val = prices

                    results.append(_make_property(
                        external_id=f"is24-{lid}", source='ImmoScout24',
                        source_url=f"https://www.immoscout24.ch/fr/d/{lid}",
                        title=listing.get('title', ''),
                        description='',
                        property_type=_guess_type(listing.get('propertyType', listing.get('title', ''))),
                        transaction=transaction,
                        price=_clean_price(price_val),
                        rooms=chars.get('numberOfRooms'),
                        surface=chars.get('livingSpace') or chars.get('surfaceLiving'),
                        floor=chars.get('floor'),
                        address=f"{addr.get('street', '')} {addr.get('postalCode', '')} {addr.get('locality', '')}".strip(),
                        city=addr.get('locality', city),
                        canton=addr.get('region', CITY_CANTONS.get(city.lower(), '')),
                        postal_code=str(addr.get('postalCode', '')) or None,
                        latitude=None, longitude=None,
                        features=[], images=[],
                        published_at=listing.get('publishDate'),
                    ))
            except Exception as e:
                log.error(f"[ImmoScout24] Parse error: {e}")
        else:
            # Fallback: try __INITIAL_STATE__ or HTML parsing
            match2 = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html)
            if match2:
                try:
                    state = json.loads(match2.group(1))
                    items = state.get('resultList', {}).get('search', {}).get('items', [])
                    log.info(f"[ImmoScout24] Page {page}: {len(items)} items via __INITIAL_STATE__")
                    for item in items:
                        lid = item.get('id', '')
                        results.append(_make_property(
                            external_id=f"is24-{lid}", source='ImmoScout24',
                            source_url=f"https://www.immoscout24.ch/fr/d/{lid}",
                            title=item.get('title', ''), description='',
                            property_type=_guess_type(item.get('title', '')),
                            transaction=transaction,
                            price=_clean_price(item.get('price')),
                            rooms=_clean_rooms(item.get('rooms')),
                            surface=_clean_surface(item.get('surface')),
                            floor=None,
                            address=item.get('address', ''), city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=_extract_postal(item.get('address', '')),
                            latitude=None, longitude=None,
                            features=[], images=[], published_at=None,
                        ))
                except Exception as e:
                    log.error(f"[ImmoScout24] __INITIAL_STATE__ parse error: {e}")
            else:
                log.info(f"[ImmoScout24] Page {page}: No structured data found, HTML size: {len(html)}")

        time.sleep(1)

    log.info(f"[ImmoScout24] Total: {len(results)} listings")
    return results


# ============================================================
# IMMOBILIER.CH — via ScrapingBee
# ============================================================

def scrape_immobilier_ch(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Immobilier.ch] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e')

    for page in range(1, max_pages + 1):
        url = f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{slug}?page={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.filter-item, .item-listing, article, .property-item')
        log.info(f"[Immobilier.ch] Page {page}: {len(cards)} cards")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, .title, .item-title')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('.price, .item-price, [class*="price"]')
                price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                addr_el = card.select_one('.address, .location, [class*="location"]')
                address = addr_el.get_text(strip=True) if addr_el else ''
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.immobilier.ch' + href
                eid = re.search(r'/(\d+)', href)

                if title or price:
                    results.append(_make_property(
                        external_id=f"imch-{eid.group(1) if eid else hashlib.sha256((title+address).encode()).hexdigest()[:12]}",
                        source='Immobilier.ch', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=_clean_rooms(title),
                        surface=None, floor=None,
                        address=address, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(1)

    log.info(f"[Immobilier.ch] Total: {len(results)} listings")
    return results


# ============================================================
# ANIBIS — via ScrapingBee
# ============================================================

def scrape_anibis(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Anibis] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"

    for page in range(1, max_pages + 1):
        url = f"https://www.anibis.ch/fr/immobilier--{tx}/{city.lower()}?page={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.listing-card, .ItemCard, article, [class*="listing"]')
        log.info(f"[Anibis] Page {page}: {len(cards)} cards")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, .title, [class*="title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('[class*="price"], .price')
                price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                addr_el = card.select_one('[class*="location"], .location')
                address = addr_el.get_text(strip=True) if addr_el else city
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.anibis.ch' + href
                eid = re.search(r'/(\d+)', href)

                if title or price:
                    results.append(_make_property(
                        external_id=f"anibis-{eid.group(1) if eid else hashlib.sha256(title.encode()).hexdigest()[:12]}",
                        source='Anibis', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=_clean_rooms(title),
                        surface=None, floor=None,
                        address=address, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(1)

    log.info(f"[Anibis] Total: {len(results)} listings")
    return results


# ============================================================
# ACHETER-LOUER — via ScrapingBee
# ============================================================

def scrape_acheter_louer(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Acheter-Louer] Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"

    for page in range(1, max_pages + 1):
        url = f"https://www.acheter-louer.ch/{tx}/{city.lower()}?page={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.property-item, .listing-card, article, [class*="result"]')
        log.info(f"[Acheter-Louer] Page {page}: {len(cards)} cards")

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
                    results.append(_make_property(
                        external_id=f"al-{hashlib.sha256((title+href).encode()).hexdigest()[:12]}",
                        source='Acheter-Louer', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=_clean_rooms(title),
                        surface=None, floor=None,
                        address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(1)

    log.info(f"[Acheter-Louer] Total: {len(results)} listings")
    return results


# ============================================================
# FLATFOX — Direct API (no ScrapingBee needed)
# ============================================================

def scrape_flatfox(city="Lausanne", transaction="location", limit=30):
    log.info(f"[Flatfox] Searching {city} ({transaction})")
    results = []
    offer_type = 'RENT' if transaction == 'location' else 'SALE'

    endpoints = [
        "https://flatfox.ch/api/v1/flat/",
        "https://flatfox.ch/api/v1/public/listings/",
    ]

    for api_url in endpoints:
        try:
            r = requests.get(api_url, params={
                'city': city, 'offer_type': offer_type,
                'ordering': '-created', 'limit': limit,
            }, timeout=20)
            log.info(f"[Flatfox] {api_url} → HTTP {r.status_code}")

            if r.status_code == 200 and 'application/json' in r.headers.get('content-type', ''):
                data = r.json()
                items = data.get('results', data) if isinstance(data, dict) else data
                if not isinstance(items, list):
                    continue

                for item in items:
                    pk = item.get('pk', item.get('id', ''))
                    slug = item.get('slug', pk)
                    if transaction == 'location':
                        price = item.get('rent_gross') or item.get('rent_net') or item.get('price_display')
                    else:
                        price = item.get('price') or item.get('selling_price') or item.get('price_display') or item.get('rent_gross')

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
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=str(item.get('zipcode', '')) or None,
                        latitude=item.get('latitude'), longitude=item.get('longitude'),
                        features=item.get('attributes', []) or [],
                        images=[img.get('url', '') for img in (item.get('images', []) or [])[:5]],
                        published_at=item.get('created'),
                    ))

                if results:
                    break  # Found working endpoint
        except Exception as e:
            log.error(f"[Flatfox] {api_url} error: {e}")

    log.info(f"[Flatfox] Total: {len(results)} listings")
    return results


# ============================================================
# COMPARIS — via ScrapingBee
# ============================================================

def scrape_comparis(city="Lausanne", transaction="location", max_pages=1):
    log.info(f"[Comparis] Searching {city} ({transaction})")
    results = []
    tx = "mieten" if transaction == "location" else "kaufen"

    url = f"https://www.comparis.ch/immobilien/result/list?requestobject=%7B%22DealType%22%3A{10 if transaction == 'location' else 20}%2C%22Keyword%22%3A%22{city}%22%2C%22Sort%22%3A4%2C%22Page%22%3A1%7D"
    status, html = _sb_get(url, render_js=True)

    if status == 200:
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('[class*="ListItem"], [class*="result-item"], article, .property-item')
        log.info(f"[Comparis] {len(cards)} cards found")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, [class*="title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('[class*="price"]')
                price = _clean_price(price_el.get_text(strip=True)) if price_el else None
                addr_el = card.select_one('[class*="address"], [class*="location"]')
                address = addr_el.get_text(strip=True) if addr_el else ''
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.comparis.ch' + href

                if title or price:
                    results.append(_make_property(
                        external_id=f"comp-{hashlib.sha256((title+address).encode()).hexdigest()[:12]}",
                        source='Comparis', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=_clean_rooms(title),
                        surface=None, floor=None,
                        address=address, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address),
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

    log.info(f"[Comparis] Total: {len(results)} listings")
    return results


# ============================================================
# PROPERSTAR — via ScrapingBee
# ============================================================

def scrape_properstar(city="Lausanne", transaction="location", max_pages=1):
    log.info(f"[Properstar] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = city.lower().replace(' ', '-')

    url = f"https://www.properstar.ch/switzerland/{slug}/{tx}/apartment"
    status, html = _sb_get(url, render_js=True)

    if status == 200:
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.listing-card, .property-card, article, [class*="listing"]')
        log.info(f"[Properstar] {len(cards)} cards found")

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
                    results.append(_make_property(
                        external_id=f"ps-{hashlib.sha256((title+href).encode()).hexdigest()[:12]}",
                        source='Properstar', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=_clean_rooms(title),
                        surface=None, floor=None,
                        address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

    log.info(f"[Properstar] Total: {len(results)} listings")
    return results


# ============================================================
# MAIN
# ============================================================

def scrape_all(city="Lausanne", transaction="location"):
    """Scrape all portals for a given city and transaction type."""
    all_results = []

    scrapers = [
        ('Flatfox', scrape_flatfox),
        ('Homegate', scrape_homegate),
        ('ImmoScout24', scrape_immoscout),
        ('Immobilier.ch', scrape_immobilier_ch),
        ('Anibis', scrape_anibis),
        ('Acheter-Louer', scrape_acheter_louer),
        ('Comparis', scrape_comparis),
        ('Properstar', scrape_properstar),
    ]

    for name, scraper in scrapers:
        try:
            results = scraper(city=city, transaction=transaction)
            all_results.extend(results)
            log.info(f"[{name}] {len(results)} results")
        except Exception as e:
            log.error(f"[{name}] FAILED: {e}")
        time.sleep(1)

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
