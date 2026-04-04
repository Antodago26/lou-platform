"""
Lou Garou — Scrapers immobiliers suisses (v2 — API-based)
Utilise les APIs JSON des portails au lieu du scraping HTML.

Usage:
    from scrapers import scrape_all
    results = scrape_all(city="Lausanne", transaction="location")
"""

import re
import json
import time
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'fr-CH,fr;q=0.9',
    'Accept': 'application/json, text/plain, */*',
}

# Swiss city → canton mapping
CITY_CANTONS = {
    'lausanne': 'VD', 'geneve': 'GE', 'genève': 'GE', 'neuchatel': 'NE',
    'neuchâtel': 'NE', 'fribourg': 'FR', 'sion': 'VS', 'montreux': 'VD',
    'nyon': 'VD', 'morges': 'VD', 'yverdon': 'VD', 'yverdon-les-bains': 'VD',
    'la chaux-de-fonds': 'NE', 'bienne': 'BE', 'biel': 'BE',
    'delemont': 'JU', 'delémont': 'JU', 'berne': 'BE', 'bern': 'BE',
    'peseux': 'NE', 'marin-epagnier': 'NE', 'vevey': 'VD',
    'renens': 'VD', 'boudry': 'NE', 'colombier': 'NE', 'zurich': 'ZH',
    'zürich': 'ZH', 'basel': 'BS', 'bâle': 'BS', 'lugano': 'TI',
    'lucerne': 'LU', 'luzern': 'LU', 'winterthur': 'ZH', 'st. gallen': 'SG',
    'thun': 'BE', 'biel/bienne': 'BE', 'köniz': 'BE', 'la chaux de fonds': 'NE',
    'schaffhausen': 'SH', 'chur': 'GR', 'uster': 'ZH', 'vernier': 'GE',
    'lancy': 'GE', 'emmen': 'LU', 'kriens': 'LU', 'rapperswil-jona': 'SG',
    'carouge': 'GE', 'meyrin': 'GE', 'prilly': 'VD', 'pully': 'VD',
    'ecublens': 'VD', 'sierre': 'VS', 'martigny': 'VS',
}

# City name → Homegate slug mapping (for URL construction)
CITY_SLUGS_HG = {
    'lausanne': 'lausanne', 'genève': 'geneve', 'geneve': 'geneve',
    'neuchâtel': 'neuchatel', 'neuchatel': 'neuchatel',
    'fribourg': 'fribourg', 'berne': 'bern', 'zürich': 'zurich',
    'zurich': 'zurich', 'bâle': 'basel', 'basel': 'basel',
}


def _make_property(external_id, source, source_url, title, description,
                   property_type, transaction, price, rooms, surface,
                   floor, address, city, canton, postal_code,
                   latitude, longitude, features, images,
                   published_at):
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
        'canton': canton or CITY_CANTONS.get(city.lower(), ''),
        'postal_code': postal_code,
        'latitude': latitude,
        'longitude': longitude,
        'features': features or [],
        'images': images or [],
        'contact_name': None,
        'contact_phone': None,
        'contact_email': None,
        'published_at': published_at,
        'scraped_at': datetime.now().isoformat(),
    }


def _guess_type(text):
    """Guess property type from title/category text."""
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
    """Extract Swiss postal code (4 digits) from address."""
    if not address:
        return None
    m = re.search(r'\b(\d{4})\b', address)
    return m.group(1) if m else None


def _clean_price(val):
    """Parse price from various formats."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    text = str(val).replace('\u2019', "'").replace("'", "").replace(',', '').replace('.–', '')
    nums = re.findall(r'\d+', text)
    if nums:
        return int(nums[0])
    return None


# ============================================================
# FLATFOX — Public REST API (most reliable)
# ============================================================

def scrape_flatfox(city="Lausanne", transaction="location", limit=30):
    """Scrape Flatfox via their public API."""
    log.info(f"[Flatfox] Searching {city} ({transaction})")
    results = []

    offer_type = 'RENT' if transaction == 'location' else 'SALE'

    # Try multiple API endpoints (Flatfox has changed their API over time)
    endpoints = [
        "https://flatfox.ch/api/v1/flat/",
        "https://flatfox.ch/api/v1/public/listings/",
        "https://flatfox.ch/api/v1/public/search/listings/",
    ]

    resp = None
    for api_url in endpoints:
        try:
            resp = requests.get(api_url, headers=HEADERS, params={
                'city': city,
                'offer_type': offer_type,
                'ordering': '-created',
                'limit': limit,
            }, timeout=20)
            log.info(f"[Flatfox] {api_url} → HTTP {resp.status_code}")
            if resp.status_code == 200 and resp.headers.get('content-type', '').startswith('application/json'):
                break  # Found working endpoint
        except Exception as e:
            log.warning(f"[Flatfox] {api_url} failed: {e}")
            resp = None

    try:
        if not resp or resp.status_code != 200:
            log.warning(f"[Flatfox] No working endpoint found")
            return results

        if resp.status_code == 200:
            data = resp.json()
            items = data.get('results', data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []

            for item in items:
                pk = item.get('pk', item.get('id', ''))
                slug = item.get('slug', pk)
                price = item.get('rent_gross') or item.get('rent_net') or item.get('price_display')

                results.append(_make_property(
                    external_id=f"ff-{pk}",
                    source='Flatfox',
                    source_url=f"https://flatfox.ch/fr/flat/{slug}/",
                    title=item.get('title', ''),
                    description=item.get('description', ''),
                    property_type=_guess_type(
                        (item.get('object_category', '') + ' ' + item.get('title', ''))
                    ),
                    transaction=transaction,
                    price=_clean_price(price),
                    rooms=item.get('number_of_rooms'),
                    surface=item.get('surface_living'),
                    floor=item.get('floor'),
                    address=(
                        (item.get('street', '') + ' ' + str(item.get('street_number', ''))).strip()
                        + ', ' + item.get('city', city)
                    ),
                    city=item.get('city', city),
                    canton=CITY_CANTONS.get(city.lower(), ''),
                    postal_code=str(item.get('zipcode', '')) or None,
                    latitude=item.get('latitude'),
                    longitude=item.get('longitude'),
                    features=item.get('attributes', []) or [],
                    images=[img.get('url', '') for img in (item.get('images', []) or [])[:5]],
                    published_at=item.get('created'),
                ))

    except Exception as e:
        log.error(f"[Flatfox] Error: {e}")

    log.info(f"[Flatfox] {len(results)} listings found")
    return results


# ============================================================
# HOMEGATE — Search API (JSON)
# ============================================================

def scrape_homegate(city="Lausanne", transaction="location", limit=20):
    """Scrape Homegate via their internal search API."""
    log.info(f"[Homegate] Searching {city} ({transaction})")
    results = []

    tx = "rent" if transaction == "location" else "buy"
    city_slug = CITY_SLUGS_HG.get(city.lower(), city.lower().replace(' ', '-'))

    # Homegate uses a ResultList API endpoint
    search_url = f"https://www.homegate.ch/api/search/{tx}"

    try:
        params = {
            'loc': city,
            'ag': limit,  # amount
            'o': 'dateCreated-desc',
        }
        resp = requests.get(search_url, headers={
            **HEADERS,
            'Referer': f'https://www.homegate.ch/{tx}/real-estate/city-{city_slug}/matching-list',
        }, params=params, timeout=15)

        log.info(f"[Homegate] API HTTP {resp.status_code}")

        if resp.status_code == 200:
            try:
                data = resp.json()
                items = data.get('results', data.get('items', []))
                if isinstance(items, list):
                    for item in items[:limit]:
                        listing = item.get('listing', item)
                        lid = listing.get('id', '')
                        loc = listing.get('address', {}) or {}
                        chars = listing.get('characteristics', {}) or {}
                        prices = listing.get('prices', {}) or {}

                        price_val = prices.get('rent', {}).get('gross') if transaction == 'location' else prices.get('buy', {}).get('price')

                        results.append(_make_property(
                            external_id=f"hg-{lid}",
                            source='Homegate',
                            source_url=f"https://www.homegate.ch/{tx}/{lid}",
                            title=listing.get('title', ''),
                            description=listing.get('description', ''),
                            property_type=_guess_type(listing.get('categories', [''])[0] if listing.get('categories') else ''),
                            transaction=transaction,
                            price=_clean_price(price_val),
                            rooms=chars.get('numberOfRooms'),
                            surface=chars.get('livingSpace'),
                            floor=chars.get('floor'),
                            address=f"{loc.get('street', '')} {loc.get('streetAddition', '')}".strip() + f", {loc.get('postalCode', '')} {loc.get('locality', '')}".strip(),
                            city=loc.get('locality', city),
                            canton=loc.get('region', CITY_CANTONS.get(city.lower(), '')),
                            postal_code=str(loc.get('postalCode', '')) or None,
                            latitude=loc.get('geoCoordinates', {}).get('latitude') if loc.get('geoCoordinates') else None,
                            longitude=loc.get('geoCoordinates', {}).get('longitude') if loc.get('geoCoordinates') else None,
                            features=[],
                            images=[],
                            published_at=listing.get('publishDate'),
                        ))
            except (ValueError, KeyError) as e:
                log.warning(f"[Homegate] JSON parse error: {e}")
        else:
            # Fallback: try the listing page and extract __NEXT_DATA__
            log.info("[Homegate] API failed, trying __NEXT_DATA__ fallback")
            page_url = f"https://www.homegate.ch/{tx}/real-estate/city-{city_slug}/matching-list"
            resp2 = requests.get(page_url, headers=HEADERS, timeout=15)
            if resp2.status_code == 200:
                match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp2.text)
                if match:
                    try:
                        next_data = json.loads(match.group(1))
                        page_props = next_data.get('props', {}).get('pageProps', {})
                        items = page_props.get('resultList', {}).get('items', [])
                        log.info(f"[Homegate] __NEXT_DATA__ found {len(items)} items")

                        for item in items[:limit]:
                            listing = item.get('listing', item)
                            lid = listing.get('id', '')
                            loc = listing.get('address', {}) or {}
                            chars = listing.get('characteristics', {}) or {}
                            prices = listing.get('prices', {}) or {}

                            price_val = prices.get('rent', {}).get('gross') if transaction == 'location' else prices.get('buy', {}).get('price')

                            results.append(_make_property(
                                external_id=f"hg-{lid}",
                                source='Homegate',
                                source_url=f"https://www.homegate.ch/{tx}/{lid}",
                                title=listing.get('title', ''),
                                description='',
                                property_type=_guess_type(listing.get('categories', [''])[0] if listing.get('categories') else ''),
                                transaction=transaction,
                                price=_clean_price(price_val),
                                rooms=chars.get('numberOfRooms'),
                                surface=chars.get('livingSpace'),
                                floor=chars.get('floor'),
                                address=f"{loc.get('street', '')} {loc.get('postalCode', '')} {loc.get('locality', '')}".strip(),
                                city=loc.get('locality', city),
                                canton=loc.get('region', CITY_CANTONS.get(city.lower(), '')),
                                postal_code=str(loc.get('postalCode', '')) or None,
                                latitude=loc.get('geoCoordinates', {}).get('latitude') if loc.get('geoCoordinates') else None,
                                longitude=loc.get('geoCoordinates', {}).get('longitude') if loc.get('geoCoordinates') else None,
                                features=[],
                                images=[],
                                published_at=listing.get('publishDate'),
                            ))
                    except json.JSONDecodeError as e:
                        log.warning(f"[Homegate] __NEXT_DATA__ parse error: {e}")

    except Exception as e:
        log.error(f"[Homegate] Error: {e}")

    log.info(f"[Homegate] {len(results)} listings found")
    return results


# ============================================================
# IMMOSCOUT24 — Search API (JSON via __NEXT_DATA__)
# ============================================================

def scrape_immoscout(city="Lausanne", transaction="location", limit=20):
    """Scrape ImmoScout24 via __NEXT_DATA__ or search endpoint."""
    log.info(f"[ImmoScout24] Searching {city} ({transaction})")
    results = []

    tx = "louer" if transaction == "location" else "acheter"
    city_slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e').replace('ü', 'u')
    page_url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{city_slug}"

    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=15)
        log.info(f"[ImmoScout24] HTTP {resp.status_code}")

        if resp.status_code == 200:
            # Try extracting __NEXT_DATA__ or similar embedded JSON
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text)
            if match:
                try:
                    next_data = json.loads(match.group(1))
                    page_props = next_data.get('props', {}).get('pageProps', {})
                    items = (page_props.get('listings', []) or
                             page_props.get('resultList', {}).get('items', []) or
                             page_props.get('searchResults', {}).get('items', []) or [])

                    log.info(f"[ImmoScout24] __NEXT_DATA__ found {len(items)} items")

                    for item in items[:limit]:
                        listing = item.get('listing', item)
                        lid = listing.get('id', item.get('id', ''))
                        addr = listing.get('address', {}) or {}
                        chars = listing.get('characteristics', {}) or {}
                        prices = listing.get('prices', listing.get('price', {})) or {}

                        if isinstance(prices, dict):
                            price_val = (prices.get('rent', {}).get('gross') or
                                        prices.get('value') or
                                        prices.get('buy', {}).get('price'))
                        else:
                            price_val = _clean_price(prices)

                        results.append(_make_property(
                            external_id=f"is24-{lid}",
                            source='ImmoScout24',
                            source_url=f"https://www.immoscout24.ch/fr/d/{lid}",
                            title=listing.get('title', ''),
                            description='',
                            property_type=_guess_type(listing.get('propertyType', '')),
                            transaction=transaction,
                            price=_clean_price(price_val),
                            rooms=chars.get('numberOfRooms'),
                            surface=chars.get('livingSpace') or chars.get('surfaceLiving'),
                            floor=chars.get('floor'),
                            address=f"{addr.get('street', '')} {addr.get('postalCode', '')} {addr.get('locality', '')}".strip(),
                            city=addr.get('locality', city),
                            canton=addr.get('region', CITY_CANTONS.get(city.lower(), '')),
                            postal_code=str(addr.get('postalCode', '')) or None,
                            latitude=addr.get('geoCoordinates', {}).get('latitude') if addr.get('geoCoordinates') else None,
                            longitude=addr.get('geoCoordinates', {}).get('longitude') if addr.get('geoCoordinates') else None,
                            features=[],
                            images=[],
                            published_at=listing.get('publishDate'),
                        ))
                except json.JSONDecodeError as e:
                    log.warning(f"[ImmoScout24] JSON parse error: {e}")
            else:
                log.info("[ImmoScout24] No __NEXT_DATA__ found in page")

    except Exception as e:
        log.error(f"[ImmoScout24] Error: {e}")

    log.info(f"[ImmoScout24] {len(results)} listings found")
    return results


# ============================================================
# COMPARIS — Search API
# ============================================================

def scrape_comparis(city="Lausanne", transaction="location", limit=20):
    """Scrape Comparis via their search API."""
    log.info(f"[Comparis] Searching {city} ({transaction})")
    results = []

    deal_type = 10 if transaction == 'location' else 20

    api_url = "https://api.comparis.ch/realestate/v1/search/list"
    payload = {
        'DealType': deal_type,
        'SiteId': 0,
        'RootPropertyTypes': [1],  # Apartment
        'PropertyTypes': [],
        'RoomsFrom': None,
        'RoomsTo': None,
        'FloorSearchType': 0,
        'LivingSpaceFrom': None,
        'LivingSpaceTo': None,
        'PriceFrom': None,
        'PriceTo': None,
        'ComparisPointsMin': 0,
        'AdAgeMax': 0,
        'Keyword': city,
        'LocationSearchString': city,
        'Sort': 4,
        'Page': 1,
        'PageSize': limit,
    }

    try:
        # Try POST first (newer API)
        resp = requests.post(api_url, headers={
            **HEADERS,
            'Content-Type': 'application/json',
            'Referer': 'https://www.comparis.ch/immobilien/result/list',
        }, json=payload, timeout=15)

        log.info(f"[Comparis] POST API HTTP {resp.status_code}")

        if resp.status_code != 200:
            # Fallback: try GET with query params
            get_url = "https://www.comparis.ch/immobilien/result/list"
            resp = requests.get(get_url, headers={
                **HEADERS,
                'Referer': 'https://www.comparis.ch/immobilien',
            }, params={'requestobject': json.dumps(payload)}, timeout=15)
            log.info(f"[Comparis] GET fallback HTTP {resp.status_code}")

        if resp.status_code == 200:
            try:
                data = resp.json()
                items = (data.get('Items', []) or
                        data.get('items', []) or
                        data.get('SearchResults', []) or [])

                log.info(f"[Comparis] Found {len(items)} items")

                for item in items[:limit]:
                    results.append(_make_property(
                        external_id=f"comp-{item.get('Id', item.get('id', ''))}",
                        source='Comparis',
                        source_url=f"https://www.comparis.ch/immobilien/angebot/show/{item.get('Id', item.get('id', ''))}",
                        title=item.get('Title', item.get('title', '')),
                        description=item.get('Description', item.get('description', '')),
                        property_type=_guess_type(item.get('Title', item.get('title', ''))),
                        transaction=transaction,
                        price=_clean_price(item.get('Price', item.get('price'))),
                        rooms=item.get('NumberOfRooms', item.get('numberOfRooms')),
                        surface=item.get('LivingSpace', item.get('livingSpace')),
                        floor=item.get('Floor', item.get('floor')),
                        address=(item.get('Street', item.get('street', '')) + ', ' +
                                item.get('CityName', item.get('cityName', city))),
                        city=item.get('CityName', item.get('cityName', city)),
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=str(item.get('Zip', item.get('zip', ''))) or None,
                        latitude=item.get('Latitude', item.get('latitude')),
                        longitude=item.get('Longitude', item.get('longitude')),
                        features=[],
                        images=[item.get('ImageUrl', '')] if item.get('ImageUrl') else [],
                        published_at=item.get('PublishDate', item.get('publishDate')),
                    ))
            except (ValueError, KeyError) as e:
                log.warning(f"[Comparis] JSON parse error: {e}")

    except Exception as e:
        log.error(f"[Comparis] Error: {e}")

    log.info(f"[Comparis] {len(results)} listings found")
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
        ('Comparis', scrape_comparis),
    ]

    for name, scraper in scrapers:
        try:
            results = scraper(city=city, transaction=transaction)
            all_results.extend(results)
            log.info(f"[{name}] {len(results)} results")
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

    log.info(f"Total: {len(unique)} unique listings from {len(scrapers)} portals (before dedup: {len(all_results)})")
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
