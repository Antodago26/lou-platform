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

import unicodedata

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')


def _normalize_city(name):
    """Remove accents and lowercase for city comparison.
    'Neuchâtel' → 'neuchatel', 'La Tène' → 'la tene'"""
    s = unicodedata.normalize('NFD', (name or '').lower().strip())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')

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
    'colombier': 'NE', 'peseux': 'NE', 'boudry': 'NE', 'cortaillod': 'NE',
    'marin-epagnier': 'NE', 'hauterive': 'NE', 'saint-blaise': 'NE',
    'le locle': 'NE', 'val-de-travers': 'NE', 'fleurier': 'NE',
    'milvignes': 'NE', 'la tène': 'NE', 'le landeron': 'NE',
    'bevaix': 'NE', 'val-de-ruz': 'NE', 'corcelles-cormondrèche': 'NE',
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

    # Retry up to 2 times on server errors (500, 502, 504)
    for attempt in range(2):
        try:
            r = requests.get(SCRAPINGBEE_URL, params=params, timeout=180)
            r.encoding = 'utf-8'  # Force UTF-8 to avoid Latin-1 misdetection
            log.info(f"[ScrapingBee] {url[:60]}... → HTTP {r.status_code} ({len(r.text)} bytes)")
            if r.status_code in (500, 502, 504) and attempt == 0:
                log.warning(f"[ScrapingBee] Server error {r.status_code}, retrying in 3s...")
                time.sleep(3)
                continue
            return r.status_code, r.text
        except requests.exceptions.Timeout:
            log.error(f"[ScrapingBee] TIMEOUT fetching {url} (attempt {attempt+1})")
            if attempt == 0:
                time.sleep(3)
                continue
            return 0, ''
        except Exception as e:
            log.error(f"[ScrapingBee] Error fetching {url}: {e}")
            return 0, ''
    return 0, ''


def _make_property(external_id, source, source_url, title, description,
                   property_type, transaction, price, rooms, surface,
                   floor, address, city, canton, postal_code,
                   latitude, longitude, features, images, published_at):
    """Create a standardized property dict."""
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

    desc_clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', (description or '')[:500]).strip()
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

def scrape_homegate(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Homegate] Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = _normalize_city(city).replace(' ', '-')
    # Add canton suffix for ambiguous city names (e.g., colombier-ne, hauterive-ne)
    canton = CITY_CANTONS.get(city.lower(), '')
    if canton and slug in ('colombier', 'hauterive', 'saint-blaise', 'corcelles-cormondrèche', 'corcelles-cormondr', 'corcelles-cormondrche'):
        slug = f"{slug}-{canton.lower()}"

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
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'',.\u2019\u00a0]+)", card_text)
                if not price_match:
                    price_match = re.search(r"([\d'',.\u2019\u00a0]+)\s*(?:CHF|Fr|\.–|/mois|/m)", card_text)
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
    tx = "rent" if transaction == "location" else "buy"
    slug = _normalize_city(city).replace(' ', '-')
    canton = CITY_CANTONS.get(city.lower(), '')
    # Add canton suffix for disambiguation (e.g., colombier-ne)
    if canton and slug in ('colombier', 'hauterive', 'saint-blaise'):
        slug = f"{slug}-{canton.lower()}"

    for page in range(1, max_pages + 1):
        # ImmoScout24 switched to English URL paths (mid-2025)
        url = f"https://www.immoscout24.ch/en/real-estate/{tx}/city-{slug}?pn={page}"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            log.warning(f"[ImmoScout24] Page {page}: HTTP {status}")
            break

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

                            # Images
                            img_list = []
                            for img in (listing.get('images', []) or item.get('images', []) or []):
                                if isinstance(img, dict):
                                    img_list.append(img.get('url', img.get('src', '')))
                                elif isinstance(img, str):
                                    img_list.append(img)

                            # Address
                            addr_str = ''
                            if isinstance(addr, dict):
                                addr_str = f"{addr.get('street', '')} {addr.get('postalCode', '')} {addr.get('locality', '')}".strip()
                                if not addr_str:
                                    addr_str = addr.get('formatted', '')

                            results.append(_make_property(
                                external_id=f"is24-{lid}", source='ImmoScout24',
                                source_url=f"https://www.immoscout24.ch/en/d/{lid}",
                                title=listing.get('title', item.get('title', '')),
                                description=listing.get('description', '')[:500],
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
                    ))
                except Exception as e:
                    log.debug(f"[ImmoScout24] Card parse error: {e}")

            if not cards:
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
    slug = _normalize_city(city).replace(' ', '-')
    canton = CITY_CANTONS.get(city.lower(), '')

    for page in range(1, max_pages + 1):
        # Try multiple URL patterns (immobilier.ch changed their URL structure)
        urls = [
            f"https://www.immobilier.ch/fr/{tx}/appartement-maison/{slug}?page={page}",
            f"https://www.immobilier.ch/fr/carte/{tx}/appartement-et-maison/{slug}?page={page}",
        ]
        html = ''
        status = 0
        for try_url in urls:
            status, html = _sb_get(try_url, render_js=True)
            if status == 200 and len(html) > 5000:
                break

        if status != 200:
            break

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
    """Scrape Anibis.ch using their GraphQL API (c.anibis.ch).
    The website uses opaque binary tokens in URLs, so direct URL construction is unreliable.
    Instead, we call the search API directly."""
    log.info(f"[Anibis] Searching {city} ({transaction})")
    results = []
    city_norm = _normalize_city(city)
    canton = CITY_CANTONS.get(city.lower(), '').lower()

    # Anibis GraphQL API
    api_url = "https://c.anibis.ch/graphql"
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Origin': 'https://www.anibis.ch',
        'Referer': 'https://www.anibis.ch/',
    }

    # Map transaction to Anibis listing types
    listing_type = "apartment" if transaction == "location" else "apartment"
    price_type = "RENT" if transaction == "location" else "BUY"

    for page in range(1, max_pages + 1):
        # GraphQL query for real estate search
        query = {
            "query": """
                query SearchListings($category: String!, $constraints: [ListingSearchConstraint!], $offset: Int, $limit: Int, $sorting: String) {
                    searchListingsByQuery(
                        category: $category
                        constraints: $constraints
                        offset: $offset
                        limit: $limit
                        sorting: $sorting
                    ) {
                        listings {
                            id
                            title
                            body
                            price
                            formattedPrice
                            thumbUrl
                            imageUrls
                            location { id name geoCoordinate { latitude longitude } }
                            attributes { name value }
                            detail { url }
                        }
                        totalCount
                    }
                }
            """,
            "variables": {
                "category": "realEstate",
                "constraints": [
                    {"name": "priceType", "value": price_type},
                ],
                "offset": (page - 1) * 30,
                "limit": 30,
                "sorting": "newest",
            }
        }

        # Add location constraint
        if canton:
            query["variables"]["constraints"].append({
                "name": "location",
                "value": f"geo-canton-{canton}" if len(canton) == 2 else f"geo-city-{city_norm.replace(' ', '-')}"
            })
        else:
            query["variables"]["constraints"].append({
                "name": "location",
                "value": f"geo-city-{city_norm.replace(' ', '-')}"
            })

        try:
            r = requests.post(api_url, json=query, headers=headers, timeout=20)
            if r.status_code != 200:
                log.warning(f"[Anibis] GraphQL API → HTTP {r.status_code}")
                # Fallback: try ScrapingBee HTML scraping
                break

            data = r.json()
            search_data = data.get('data', {}).get('searchListingsByQuery', {})
            items = search_data.get('listings', [])
            total = search_data.get('totalCount', 0)
            log.info(f"[Anibis] API page {page}: {len(items)} items (total={total})")

            if not items:
                break

            for item in items:
                lid = item.get('id', '')
                if not lid:
                    continue

                title = item.get('title', '')
                price = _clean_price(item.get('price') or item.get('formattedPrice'))

                # Extract rooms and surface from attributes
                rooms = None
                surface = None
                for attr in (item.get('attributes') or []):
                    attr_name = (attr.get('name') or '').lower()
                    attr_val = attr.get('value', '')
                    if 'room' in attr_name or 'piece' in attr_name or attr_name == 'numberOfRooms':
                        rooms = _clean_rooms(str(attr_val))
                    elif 'surface' in attr_name or 'area' in attr_name or attr_name == 'livingSpace':
                        surface = _clean_surface(str(attr_val))

                if not rooms:
                    rooms = _clean_rooms(title)
                if not surface:
                    surface = _clean_surface(title)

                # Location
                loc = item.get('location', {}) or {}
                loc_name = loc.get('name', '')
                geo = loc.get('geoCoordinate', {}) or {}
                lat = geo.get('latitude')
                lng = geo.get('longitude')

                # Images
                images = item.get('imageUrls', []) or []
                if not images and item.get('thumbUrl'):
                    images = [item['thumbUrl']]
                images = [u for u in images if u and u.startswith('http')][:5]

                # Detail URL
                detail = item.get('detail', {}) or {}
                detail_url = detail.get('url', '')
                if detail_url and detail_url.startswith('/'):
                    detail_url = 'https://www.anibis.ch' + detail_url
                if not detail_url:
                    detail_url = f"https://www.anibis.ch/fr/vi/{lid}"

                results.append(_make_property(
                    external_id=f"anibis-{lid}", source='Anibis',
                    source_url=detail_url,
                    title=title, description=(item.get('body') or '')[:500],
                    property_type=_guess_type(title),
                    transaction=transaction,
                    price=price, rooms=rooms, surface=surface, floor=None,
                    address=loc_name or city, city=loc_name.split(',')[0].strip() if ',' in loc_name else (loc_name or city),
                    canton=CITY_CANTONS.get(city.lower(), ''),
                    postal_code=_extract_postal(loc_name),
                    latitude=lat, longitude=lng,
                    features=[], images=images, published_at=None,
                ))

            if len(items) < 30:
                break  # No more pages

        except Exception as e:
            log.error(f"[Anibis] GraphQL API error: {e}")
            break

        time.sleep(1)

    # Fallback: if GraphQL failed, try scraping HTML via ScrapingBee
    if not results:
        log.info("[Anibis] GraphQL failed, trying HTML fallback via ScrapingBee")
        slug = city_norm.replace(' ', '-')
        # Try the landing page which has pre-built search links
        fallback_url = f"https://www.anibis.ch/fr/immobilier--{slug}"
        status, html = _sb_get(fallback_url, render_js=True)
        if status == 200 and len(html) > 5000:
            soup = BeautifulSoup(html, 'html.parser')

            # Try __NEXT_DATA__ (Anibis is Next.js)
            nd = soup.select_one('script#__NEXT_DATA__')
            if nd:
                try:
                    ndata = json.loads(nd.string or '{}')
                    # Navigate dehydratedState for listings
                    queries = ndata.get('props', {}).get('pageProps', {}).get('dehydratedState', {}).get('queries', [])
                    for q in queries:
                        state_data = q.get('state', {}).get('data', {})
                        listings = state_data.get('listings', [])
                        if listings:
                            log.info(f"[Anibis] Found {len(listings)} items in __NEXT_DATA__")
                            for item in listings:
                                lid = item.get('id', '')
                                if not lid:
                                    continue
                                title = item.get('title', '')
                                results.append(_make_property(
                                    external_id=f"anibis-{lid}", source='Anibis',
                                    source_url=f"https://www.anibis.ch/fr/vi/{lid}",
                                    title=title, description='',
                                    property_type=_guess_type(title),
                                    transaction=transaction,
                                    price=_clean_price(item.get('price') or item.get('formattedPrice')),
                                    rooms=_clean_rooms(title), surface=None, floor=None,
                                    address=city, city=city,
                                    canton=CITY_CANTONS.get(city.lower(), ''),
                                    postal_code=None, latitude=None, longitude=None,
                                    features=[], images=[item['thumbUrl']] if item.get('thumbUrl') else [],
                                    published_at=None,
                                ))
                except Exception as e:
                    log.error(f"[Anibis] __NEXT_DATA__ parse error: {e}")

            # Also try link-based scraping
            if not results:
                cards = soup.select('a[href*="/fr/vi/"][href*="/immobilier/"]')
                if not cards:
                    all_links = soup.select('a[href*="/immobilier/"]')
                    cards = [a for a in all_links if re.search(r'/\d{5,}', a.get('href', ''))]
                log.info(f"[Anibis] HTML fallback: {len(cards)} cards found")
                for card in cards[:30]:
                    try:
                        href = card.get('href', '')
                        if href.startswith('/'):
                            href = 'https://www.anibis.ch' + href
                        card_text = card.get_text(' ', strip=True)
                        if len(card_text) < 15:
                            continue
                        eid_m = re.search(r'/(\d+)', href)
                        price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d',.]+)", card_text)
                        results.append(_make_property(
                            external_id=f"anibis-{eid_m.group(1) if eid_m else hashlib.sha256(href.encode()).hexdigest()[:12]}",
                            source='Anibis', source_url=href,
                            title=card_text[:100], description='',
                            property_type=_guess_type(card_text),
                            transaction=transaction,
                            price=_clean_price(price_match.group(0)) if price_match else None,
                            rooms=_clean_rooms(card_text), surface=_clean_surface(card_text), floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=[], published_at=None,
                        ))
                    except Exception:
                        pass

    log.info(f"[Anibis] Total: {len(results)} listings")
    return results


# ============================================================
# ACHETER-LOUER — via ScrapingBee
# ============================================================

def scrape_acheter_louer(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Acheter-Louer] Searching {city} ({transaction})")
    results = []
    slug = _normalize_city(city).replace(' ', '-')

    if transaction == 'achat':
        url_patterns = [
            f"https://www.acheter-louer.ch/fr/achat-immobilier/{slug}",
            f"https://www.acheter-louer.ch/acheter/{slug}-appartements-a-vendre.html",
        ]
    else:
        url_patterns = [
            f"https://www.acheter-louer.ch/fr/location-immobilier/{slug}",
            f"https://www.acheter-louer.ch/louer/{slug}-appartements-a-louer.html",
        ]

    for page in range(1, max_pages + 1):
        # Try multiple URL patterns until one works
        status = 0
        html = ''
        for url_tmpl in url_patterns:
            url = url_tmpl + (f"?page={page}" if page > 1 else '')
            status, html = _sb_get(url, render_js=True)
            if status == 200 and len(html) > 2000:
                break

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')

        # Try JSON-LD first
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(script.string or '{}')
                items = ld.get('itemListElement', [])
                for item in items:
                    obj = item.get('item', item)
                    url_str = obj.get('url', '')
                    if url_str and url_str.startswith('/'):
                        url_str = 'https://www.acheter-louer.ch' + url_str
                    results.append(_make_property(
                        external_id=f"al-{hashlib.sha256(url_str.encode()).hexdigest()[:12]}",
                        source='Acheter-Louer', source_url=url_str,
                        title=obj.get('name', ''), description=obj.get('description', ''),
                        property_type=_guess_type(obj.get('name', '')),
                        transaction=transaction,
                        price=_clean_price(obj.get('offers', {}).get('price') if isinstance(obj.get('offers'), dict) else None),
                        rooms=_clean_rooms(obj.get('name', '')),
                        surface=None, floor=None,
                        address='', city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None, latitude=None, longitude=None,
                        features=[], images=[obj['image']] if obj.get('image') else [],
                        published_at=None,
                    ))
            except Exception:
                pass

        # HTML cards — try multiple selectors
        cards = soup.select('.property-item, .listing-card, article, [class*="result"], [class*="annonce"], [class*="listing"]')
        if not cards:
            # Try links to property detail pages
            cards = soup.select('a[href*="/acheter/"], a[href*="/louer/"], a[href*="/annonce/"]')
        log.info(f"[Acheter-Louer] Page {page}: {len(cards)} HTML cards (+ {len(results)} JSON-LD)")

        # Log a snippet of the HTML for debugging if no cards found
        if not cards and len(results) == 0:
            body = soup.select_one('body')
            if body:
                log.info(f"[Acheter-Louer] Body snippet: {str(body)[:500]}")

        existing_urls = {r['source_url'] for r in results}
        for card in cards:
            try:
                # Get link — card might be <a> itself
                href = card.get('href', '')
                if not href:
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.acheter-louer.ch' + href
                if href in existing_urls:
                    continue

                card_text = card.get_text(' ', strip=True)
                if not card_text or len(card_text) < 30:
                    continue  # Skip generic elements like "objet(s)", "Menu", etc.

                # Log first card for debugging
                if len(results) == 0 and len(existing_urls) == 0:
                    log.info(f"[Acheter-Louer] Sample card text: {card_text[:200]}")

                # Title
                title_el = card.select_one('h3, h2, h1, [class*="title"], [class*="Title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                if not title:
                    lines = [l.strip() for l in card.stripped_strings if len(l.strip()) > 3]
                    title = lines[0] if lines else ''

                # Price
                price = None
                price_el = card.select_one('[class*="price"], [class*="Price"], .price')
                if price_el:
                    price = _clean_price(price_el.get_text(strip=True))
                if not price:
                    price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d',.]+)", card_text)
                    if price_match:
                        price = _clean_price(price_match.group(0))
                    else:
                        price_match2 = re.search(r"(\d[\d']{2,}(?:\.\s*–|\.–|\.-)?)", card_text)
                        if price_match2:
                            price = _clean_price(price_match2.group(1))

                # Surface & rooms
                surface = None
                surf_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surf_match:
                    surface = int(surf_match.group(1))
                rooms = _clean_rooms(card_text) or _clean_rooms(title)

                addr_el = card.select_one('[class*="location"], [class*="address"], [class*="Location"]')
                address = addr_el.get_text(strip=True) if addr_el else ''

                # Images
                images = []
                for img_el in card.select('img[src], img[data-src]'):
                    src = img_el.get('src', '') or img_el.get('data-src', '')
                    if src and src.startswith('http') and 'logo' not in src.lower() and 'icon' not in src.lower() and 'pixel' not in src.lower():
                        images.append(src)
                for styled in card.select('[style*="background"]'):
                    bg_match = re.search(r'url\(["\']?(https?://[^"\')\s]+)', styled.get('style', ''))
                    if bg_match:
                        images.append(bg_match.group(1))
                images = list(dict.fromkeys(images))[:5]

                if title or price:
                    existing_urls.add(href)
                    results.append(_make_property(
                        external_id=f"al-{hashlib.sha256((title+href).encode()).hexdigest()[:12]}",
                        source='Acheter-Louer', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(title), transaction=transaction,
                        price=price, rooms=rooms,
                        surface=surface, floor=None,
                        address=address or city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=_extract_postal(address or card_text),
                        latitude=None, longitude=None,
                        features=[], images=images, published_at=None,
                    ))
            except Exception as e:
                log.debug(f"[Acheter-Louer] Card parse error: {e}")
                pass

        time.sleep(1)

    log.info(f"[Acheter-Louer] Total: {len(results)} listings")
    return results


# ============================================================
# FLATFOX — Direct API (no ScrapingBee needed)
# ============================================================

def scrape_flatfox(city="Lausanne", transaction="location", limit=50):
    log.info(f"[Flatfox] Searching {city} ({transaction})")
    results = []
    offer_type = 'RENT' if transaction == 'location' else 'SELL'
    city_norm = _normalize_city(city)

    # Public API — no auth needed, returns all listings, we filter client-side by city
    api_url = "https://flatfox.ch/api/v1/public-listing/"
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    }

    # Paginate through listings, filter by city + offer_type client-side
    # Cap at 20 pages (2000 listings) to avoid excessive requests
    max_pages = 20
    page_size = 100
    found = 0

    for page_idx in range(max_pages):
        try:
            r = requests.get(api_url, params={
                'limit': page_size,
                'offset': page_idx * page_size,
            }, headers=headers, timeout=20)

            if r.status_code != 200:
                log.warning(f"[Flatfox] API page {page_idx} → HTTP {r.status_code}")
                break

            data = r.json()
            items = data.get('results', [])
            if not items:
                break

            total_count = data.get('count', 0)
            log.info(f"[Flatfox] Page {page_idx}: {len(items)} items (total={total_count})")

            for item in items:
                # Filter by city (accent-insensitive)
                item_city = _normalize_city(item.get('city') or '')
                if item_city != city_norm:
                    continue
                # Filter by offer type
                if item.get('offer_type') != offer_type:
                    continue

                pk = item.get('pk', '')
                slug = item.get('slug', pk)

                if transaction == 'location':
                    price = item.get('rent_gross') or item.get('rent_net') or item.get('price_display')
                else:
                    price = item.get('price_display')

                # Build image URLs
                images = []
                cover = item.get('cover_image')
                if isinstance(cover, dict) and cover.get('url'):
                    img_url = cover['url']
                    if img_url.startswith('/'):
                        img_url = 'https://flatfox.ch' + img_url
                    images.append(img_url)

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
                )
                if prop:
                    results.append(prop)
                    found += 1

            # Stop early if we've checked enough or found enough
            if found >= limit:
                break
            # If we've gone through a lot and total is huge, stop to save time
            if page_idx >= 5 and found == 0:
                log.info(f"[Flatfox] No matches after {(page_idx+1)*page_size} listings, stopping")
                break

        except Exception as e:
            log.error(f"[Flatfox] API page {page_idx} error: {e}")
            break

        time.sleep(0.5)

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

    for page in range(1, max_pages + 1):
        url = f"https://www.comparis.ch/immobilien/result/list?requestobject=%7B%22DealType%22%3A{deal_type}%2C%22Keyword%22%3A%22{quote(city)}%22%2C%22Sort%22%3A4%2C%22Page%22%3A{page}%7D"
        status, html = _sb_get(url, render_js=True)

        if status != 200:
            break
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
        status, html = _sb_get(url, render_js=True)
        if status == 200 and len(html) > 2000:
            all_html.append(html)

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
                    ))
            except Exception as e:
                log.debug(f"[Properstar] JSON-LD error: {e}")

        # Method 2: HTML cards (article.item-adaptive) — up to 20 per page
        existing_ids = {r['external_id'] for r in results}
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
                    ))
            except Exception as e:
                log.debug(f"[Properstar] Card parse error: {e}")

    log.info(f"[Properstar] Total: {len(results)} listings")
    return results


# ============================================================
# MAIN
# ============================================================

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


def scrape_all(city="Lausanne", transaction="location"):
    """Scrape all portals for a given city and transaction type.
    For small towns, also scrapes the nearest large city to catch listings
    that portals list under the main city name."""
    all_results = []

    # Determine cities to scrape
    cities_to_scrape = [city]
    nearby = NEARBY_MAIN_CITY.get(city.lower())
    if nearby and nearby.lower() != city.lower():
        cities_to_scrape.append(nearby)
        log.info(f"[scrape_all] Also scraping nearby city: {nearby}")

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

    for scrape_city in cities_to_scrape:
        log.info(f"[scrape_all] Scraping city: {scrape_city}")
        for name, scraper in scrapers:
            try:
                results = [r for r in scraper(city=scrape_city, transaction=transaction) if r is not None]
                all_results.extend(results)
                log.info(f"[{name}][{scrape_city}] {len(results)} results")
            except Exception as e:
                log.error(f"[{name}][{scrape_city}] FAILED: {e}")
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
    price_changes = 0
    for p in listings:
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
    log.info(f"Saved {saved}/{len(listings)} to database ({price_changes} price changes detected)")
    return saved
