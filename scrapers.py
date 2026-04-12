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
    tx = "louer" if transaction == "location" else "acheter"
    slug = _normalize_city(city).replace(' ', '-')
    canton = CITY_CANTONS.get(city.lower(), '')
    # Add canton suffix for disambiguation (e.g., colombier-ne)
    if canton and slug in ('colombier', 'hauterive', 'saint-blaise'):
        slug = f"{slug}-{canton.lower()}"

    for page in range(1, max_pages + 1):
        url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{slug}?pn={page}"
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
                                source_url=f"https://www.immoscout24.ch/fr/d/{lid}",
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
                     soup.select('a[href*="/fr/d/"]'))
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

        # Method 1: Try JSON-LD structured data (schema.org)
        found_jsonld = False
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                ld_data = json.loads(script.string or '{}')
                if isinstance(ld_data, list):
                    ld_data = ld_data[0] if ld_data else {}
                items = ld_data.get('itemListElement', []) or ld_data.get('about', [])
                if not items and ld_data.get('@type') in ('Residence', 'Apartment', 'House'):
                    items = [ld_data]
                for item in items:
                    obj = item.get('item', item)
                    if obj.get('@type') not in ('Residence', 'Apartment', 'House', 'RealEstateListing', None):
                        continue
                    name = obj.get('name', '')
                    obj_url = obj.get('url', '')
                    if obj_url and obj_url.startswith('/'):
                        obj_url = 'https://www.immobilier.ch' + obj_url
                    price_spec = obj.get('offers', {}).get('price') or obj.get('price')
                    eid_m = re.search(r'[-/](\d{5,})', obj_url)
                    results.append(_make_property(
                        external_id=f"imch-{eid_m.group(1) if eid_m else hashlib.sha256(obj_url.encode()).hexdigest()[:12]}",
                        source='Immobilier.ch', source_url=obj_url,
                        title=name, description=obj.get('description', '')[:500],
                        property_type=_guess_type(name), transaction=transaction,
                        price=_clean_price(price_spec),
                        rooms=_clean_rooms(name),
                        surface=_clean_surface(str(obj.get('floorSize', {}).get('value', '') if isinstance(obj.get('floorSize'), dict) else '')),
                        floor=None,
                        address=str(obj.get('address', {}).get('streetAddress', '')) if isinstance(obj.get('address'), dict) else '',
                        city=str(obj.get('address', {}).get('addressLocality', city)) if isinstance(obj.get('address'), dict) else city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=str(obj.get('address', {}).get('postalCode', '')) if isinstance(obj.get('address'), dict) else None,
                        latitude=None, longitude=None,
                        features=[], images=[obj['image']] if obj.get('image') else [],
                        published_at=None,
                    ))
                    found_jsonld = True
            except Exception as e:
                log.debug(f"[Immobilier.ch] JSON-LD parse error: {e}")

        # Method 2: HTML card links (broad selectors)
        if not found_jsonld:
            tx_slug = 'acheter' if transaction == 'achat' else 'louer'
            cards = soup.select(f'a[href*="/fr/{tx_slug}/"]')
            if not cards:
                cards = (soup.select('.filter-item') or
                         soup.select('[class*="property"]') or
                         soup.select('[class*="listing"]') or
                         soup.select('article'))
            log.info(f"[Immobilier.ch] Page {page}: {len(cards)} cards via HTML")

            for card in cards:
                try:
                    # Get the link
                    if card.name == 'a':
                        href = card.get('href', '')
                        link_el = card
                    else:
                        link_el = card.select_one('a[href]')
                        href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.immobilier.ch' + href
                    # Skip navigation/pagination links
                    if not re.search(r'/\d{4,}', href) and not re.search(r'[-/]\d{5,}', href):
                        continue
                    eid_m = re.search(r'[-/](\d{5,})', href)

                    card_text = card.get_text(' ', strip=True)

                    # Extract from <strong> tags (immobilier.ch pattern)
                    strongs = card.select('strong')
                    title = ''
                    price = None
                    for s in strongs:
                        txt = s.get_text(strip=True)
                        if re.search(r'CHF|[\d\']+\.?-', txt):
                            price = _clean_price(txt)
                        elif len(txt) > 5 and not price:
                            title = txt

                    if not price:
                        price_match = re.search(r"(?:CHF)\s*([\d'',.\u2019\u00a0\s]+)", card_text)
                        if price_match:
                            price = _clean_price(price_match.group(1))
                    if not title:
                        for el in card.select('h2, h3, strong, span'):
                            t = el.get_text(strip=True)
                            if t and len(t) > 5 and not re.match(r'^(?:CHF\s*)?[\d\s\'\',.]+\.?-?$', t):
                                title = t
                                break

                    surface = None
                    surf_match = re.search(r'(\d+)\s*m[²2]', card_text)
                    if surf_match:
                        surface = int(surf_match.group(1))
                    rooms = _clean_rooms(card_text)

                    # Address
                    address = ''
                    addr_match = re.search(r'(\d{4}\s+[\w\s-]+)', card_text)
                    if addr_match:
                        address = addr_match.group(1).strip()

                    # Images
                    images = []
                    for img_el in card.select('img[src], img[data-src]'):
                        src = img_el.get('src', '') or img_el.get('data-src', '')
                        if src and src.startswith('http') and 'logo' not in src.lower():
                            images.append(src)
                    images = list(dict.fromkeys(images))[:5]

                    if title or price:
                        results.append(_make_property(
                            external_id=f"imch-{eid_m.group(1) if eid_m else hashlib.sha256(href.encode()).hexdigest()[:12]}",
                            source='Immobilier.ch', source_url=href,
                            title=title, description='',
                            property_type=_guess_type(title or card_text), transaction=transaction,
                            price=price, rooms=rooms,
                            surface=surface, floor=None,
                            address=address, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=_extract_postal(address),
                            latitude=None, longitude=None,
                            features=[], images=images, published_at=None,
                        ))
                except Exception as e:
                    log.debug(f"[Immobilier.ch] Card parse error: {e}")

        else:
            log.info(f"[Immobilier.ch] Page {page}: {len(results)} items via JSON-LD")

        time.sleep(1)

    log.info(f"[Immobilier.ch] Total: {len(results)} listings")
    return results


# ============================================================
# ANIBIS — via ScrapingBee
# ============================================================

def scrape_anibis(city="Lausanne", transaction="location", max_pages=2):
    log.info(f"[Anibis] Searching {city} ({transaction})")
    results = []
    slug = _normalize_city(city).replace(' ', '-')

    for page in range(1, max_pages + 1):
        # Try multiple URL patterns (Anibis changed their URL format)
        urls_to_try = [
            f"https://www.anibis.ch/fr/immobilier/{slug}?ot={'buy' if transaction == 'achat' else 'rent'}&page={page}",
            f"https://www.anibis.ch/fr/q/immobilier-{slug}?page={page}",
            f"https://www.anibis.ch/fr/immobilier--{'acheter' if transaction == 'achat' else 'louer'}/{slug}?page={page}",
        ]
        status = 0
        html = ''
        for url in urls_to_try:
            status, html = _sb_get(url, render_js=True)
            if status == 200 and len(html) > 5000:
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
                    url = obj.get('url', '')
                    if url and url.startswith('/'):
                        url = 'https://www.anibis.ch' + url
                    if url and '/immobilier/' in url:
                        eid_m = re.search(r'/(\d+)', url)
                        results.append(_make_property(
                            external_id=f"anibis-{eid_m.group(1) if eid_m else hashlib.sha256(url.encode()).hexdigest()[:12]}",
                            source='Anibis', source_url=url,
                            title=obj.get('name', ''), description='',
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

        # Then try HTML cards — Anibis is a React SPA with hashed MUI classes
        # Detail links have a numeric ID: /fr/vi/{canton}/immobilier/{category}/{slug}/{id}
        cards = soup.select('a[href*="/fr/vi/"][href*="/immobilier/"]')
        if not cards:
            # Broader: any link with /immobilier/ AND a numeric segment (filters out nav links)
            all_immo_links = soup.select('a[href*="/immobilier/"]')
            cards = [a for a in all_immo_links if re.search(r'/\d{5,}', a.get('href', ''))]
        if not cards:
            cards = soup.select('.listing-card, .ItemCard, article, [class*="listing"], [class*="Listing"]')
        # Deduplicate: skip cards for already-found URLs
        existing_urls = {r['source_url'] for r in results}
        log.info(f"[Anibis] Page {page}: {len(cards)} HTML cards (+ {len(results)} JSON-LD)")

        for card in cards:
            try:
                # The card itself might be an <a> tag — get href directly
                href = card.get('href', '')
                if not href:
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.anibis.ch' + href
                if href in existing_urls:
                    continue

                # Get ALL text from the card — MUI hashes classes so we parse text directly
                card_text = card.get_text(' ', strip=True)
                if not card_text or len(card_text) < 20:
                    continue  # Skip nav links like "Immobilier" or "Menu"

                # Skip if href doesn't look like a detail page (need numeric ID)
                if href and '/immobilier/' in href and not re.search(r'/\d{4,}', href):
                    continue

                # Log first card text for debugging
                if len(results) == 0:
                    log.info(f"[Anibis] Sample card text: {card_text[:200]}")

                # Try selectors first, then fall back to text parsing
                title_el = card.select_one('h3, h2, h1, [class*="title"], [class*="Title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                if not title:
                    # First meaningful line of text is often the title
                    lines = [l.strip() for l in card.stripped_strings if len(l.strip()) > 3]
                    title = lines[0] if lines else ''

                # Price: look for CHF pattern or price element
                price = None
                price_el = card.select_one('[class*="price"], [class*="Price"], .price')
                if price_el:
                    price = _clean_price(price_el.get_text(strip=True))
                if not price:
                    price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d',.]+)", card_text)
                    if price_match:
                        price = _clean_price(price_match.group(0))
                    else:
                        # Try bare number patterns (e.g. "1'200.–" or "580'000")
                        price_match2 = re.search(r"(\d[\d']{2,}(?:\.\s*–|\.–|\.-)?)", card_text)
                        if price_match2:
                            price = _clean_price(price_match2.group(1))

                # Surface
                surface = None
                surf_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surf_match:
                    surface = int(surf_match.group(1))

                rooms = _clean_rooms(card_text) or _clean_rooms(title)

                # Address/location
                addr_el = card.select_one('[class*="location"], [class*="Location"], [class*="address"]')
                address = addr_el.get_text(strip=True) if addr_el else ''
                if not address:
                    # Look for NPA pattern in text
                    npa_match = re.search(r'(\d{4})\s+(\w+)', card_text)
                    if npa_match:
                        address = npa_match.group(0)

                eid = re.search(r'/(\d+)', href)

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
                        external_id=f"anibis-{eid.group(1) if eid else hashlib.sha256((title+card_text[:50]).encode()).hexdigest()[:12]}",
                        source='Anibis', source_url=href,
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
                log.debug(f"[Anibis] Card parse error: {e}")
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
    tx = "rent" if transaction == "location" else "buy"
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
        cards = soup.select('.listing-card, .property-card, article, [class*="listing"]')
        log.info(f"[Properstar] {len(cards)} cards found")

        for card in cards:
            try:
                # Link: find the main property link (avoid nav/header links)
                link_el = card.select_one('a[href*="/listing/"], a[href*="/property/"], a[href*="/suisse/"]')
                if not link_el:
                    link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.properstar.ch' + href
                if not href or href == 'https://www.properstar.ch':
                    continue

                # Price: look for price element, avoid grabbing title text
                price_el = card.select_one('[class*="price"], [class*="Price"]')
                price_text = price_el.get_text(strip=True) if price_el else ''
                price = _clean_price(price_text) if price_text else None

                # Title: find title, but exclude price elements
                title = ''
                for el in card.select('h3, h2, h4, [class*="title"], [class*="Title"]'):
                    # Skip if this element IS the price element
                    if price_el and el == price_el:
                        continue
                    t = el.get_text(strip=True)
                    # Skip if text is just a price
                    if t and not re.match(r'^(?:CHF\s*)?[\d\s\'\',.]+\.?$', t) and len(t) > 3:
                        title = t
                        break

                # Extract address/location from card text
                card_text = card.get_text(' ', strip=True)
                address = city
                postal = None
                addr_match = re.search(r'(\d{4})\s+([\w\s-]+?)(?:\s*(?:CHF|Fr|pièce|m²|\d+\s*pcs))', card_text)
                if addr_match:
                    postal = addr_match.group(1)
                    address = f"{addr_match.group(1)} {addr_match.group(2).strip()}"

                # Rooms from card text (not just title)
                rooms = _clean_rooms(card_text)

                # Surface from card text
                surface = _clean_surface(card_text)

                # Extract images
                images = []
                for img_el in card.select('img[src], img[data-src]'):
                    src = img_el.get('src', '') or img_el.get('data-src', '')
                    if src and src.startswith('http') and 'logo' not in src.lower() and 'icon' not in src.lower() and 'pixel' not in src.lower():
                        images.append(src)
                for styled in card.select('[style*="background"]'):
                    style = styled.get('style', '')
                    bg_match = re.search(r'url\(["\']?(https?://[^"\')\s]+)', style)
                    if bg_match:
                        images.append(bg_match.group(1))
                images = list(dict.fromkeys(images))[:5]

                if price or title:
                    results.append(_make_property(
                        external_id=f"ps-{hashlib.sha256((str(price)+href).encode()).hexdigest()[:12]}",
                        source='Properstar', source_url=href,
                        title=title, description='',
                        property_type=_guess_type(card_text), transaction=transaction,
                        price=price, rooms=rooms,
                        surface=surface, floor=None,
                        address=address, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=postal,
                        latitude=None, longitude=None,
                        features=[], images=images, published_at=None,
                    ))
            except Exception:
                pass

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
