"""
Lou Garou — Scrapers immobiliers suisses
Scrape les annonces depuis les portails principaux.

Usage:
    from scrapers import scrape_all, scrape_homegate, scrape_immoscout
    results = scrape_all(city="Neuchâtel", transaction="location")

Chaque scraper retourne une liste de dicts compatibles avec la table properties.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-scrapers')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'fr-CH,fr;q=0.9',
}

# Swiss city → canton mapping
CITY_CANTONS = {
    'lausanne': 'VD', 'geneve': 'GE', 'neuchatel': 'NE', 'fribourg': 'FR',
    'sion': 'VS', 'montreux': 'VD', 'nyon': 'VD', 'morges': 'VD',
    'yverdon': 'VD', 'la chaux-de-fonds': 'NE', 'bienne': 'BE',
    'delemont': 'JU', 'berne': 'BE', 'peseux': 'NE', 'marin-epagnier': 'NE',
    'vevey': 'VD', 'renens': 'VD', 'boudry': 'NE', 'colombier': 'NE',
}


def clean_price(text):
    """Extract price from text like 'CHF 2'450.–/mois'."""
    if not text:
        return None
    nums = re.findall(r'[\d\']+', text.replace('\u2019', "'").replace(',', ''))
    if nums:
        return int(nums[0].replace("'", ""))
    return None


def clean_rooms(text):
    """Extract rooms from text like '4.5 pièces' or '3½'."""
    if not text:
        return None
    text = text.replace('½', '.5')
    nums = re.findall(r'[\d.]+', text)
    if nums:
        return float(nums[0])
    return None


def clean_surface(text):
    """Extract surface from text like '105 m²'."""
    if not text:
        return None
    nums = re.findall(r'(\d+)\s*m', text)
    if nums:
        return int(nums[0])
    return None


# ============================================================
# HOMEGATE
# ============================================================

def scrape_homegate(city="Neuchâtel", transaction="location", max_pages=3):
    """Scrape Homegate listings."""
    log.info(f"Scraping Homegate: {city} ({transaction})")
    results = []

    tx = "rent" if transaction == "location" else "buy"
    base_url = f"https://www.homegate.ch/{tx}/real-estate/city-{city.lower()}/matching-list"

    for page in range(1, max_pages + 1):
        try:
            params = {'ep': page}
            resp = requests.get(base_url, headers=HEADERS, params=params, timeout=15)
            if resp.status_code != 200:
                log.warning(f"Homegate page {page}: HTTP {resp.status_code}")
                break

            soup = BeautifulSoup(resp.text, 'lxml')

            # Find listing cards
            cards = soup.select('[data-test="result-list-item"], .ResultList_resultListItem__EDDng, article')
            if not cards:
                log.info(f"Homegate: no cards found on page {page}")
                break

            for card in cards:
                try:
                    # Title
                    title_el = card.select_one('h3, [data-test="result-list-item-title"], .ListItem_header__L0MRZ')
                    title = title_el.get_text(strip=True) if title_el else ''

                    # Price
                    price_el = card.select_one('[data-test="result-list-item-price"], .ListItem_price__mMMhq, .HgListingCard_price__zrIRb')
                    price_text = price_el.get_text(strip=True) if price_el else ''
                    price = clean_price(price_text)

                    # Address
                    addr_el = card.select_one('[data-test="result-list-item-address"], .ListItem_address__cN3pa, .HgListingCard_address__JjGFX')
                    address = addr_el.get_text(strip=True) if addr_el else ''

                    # Rooms & Surface
                    details = card.select('.HgListingRoomsLivingSpace_rooms__MfVuH, .HgListingRoomsLivingSpace_livingSpace__BTJBZ, [data-test="result-list-item-rooms"], [data-test="result-list-item-living-space"]')
                    rooms = None
                    surface = None
                    for d in details:
                        txt = d.get_text(strip=True)
                        if 'pièce' in txt.lower() or 'room' in txt.lower() or '½' in txt:
                            rooms = clean_rooms(txt)
                        elif 'm²' in txt or 'm2' in txt:
                            surface = clean_surface(txt)

                    # Link
                    link_el = card.select_one('a[href]')
                    source_url = ''
                    external_id = ''
                    if link_el:
                        href = link_el.get('href', '')
                        if href.startswith('/'):
                            href = 'https://www.homegate.ch' + href
                        source_url = href
                        id_match = re.search(r'/(\d+)$', href)
                        if id_match:
                            external_id = id_match.group(1)

                    if title or price:
                        results.append({
                            'external_id': external_id or f"hg-{hash(title+address)}",
                            'source': 'Homegate',
                            'source_url': source_url,
                            'title': title,
                            'description': '',
                            'property_type': _guess_type(title),
                            'transaction': transaction,
                            'price': price,
                            'currency': 'CHF',
                            'price_unit': 'mois' if transaction == 'location' else 'total',
                            'rooms': rooms,
                            'surface': surface,
                            'floor': None,
                            'address': address,
                            'city': city,
                            'canton': CITY_CANTONS.get(city.lower(), ''),
                            'postal_code': _extract_postal(address),
                            'latitude': None,
                            'longitude': None,
                            'features': [],
                            'images': [],
                            'contact_name': None,
                            'contact_phone': None,
                            'contact_email': None,
                            'published_at': None,
                            'scraped_at': datetime.now().isoformat(),
                        })
                except Exception as e:
                    log.debug(f"Homegate card parse error: {e}")

            time.sleep(1)  # Rate limiting

        except Exception as e:
            log.error(f"Homegate page {page} error: {e}")
            break

    log.info(f"Homegate: {len(results)} listings found")
    return results


# ============================================================
# IMMOSCOUT24
# ============================================================

def scrape_immoscout(city="Neuchâtel", transaction="location", max_pages=3):
    """Scrape ImmoScout24 listings."""
    log.info(f"Scraping ImmoScout24: {city} ({transaction})")
    results = []

    tx = "louer" if transaction == "location" else "acheter"
    base_url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{city.lower()}"

    for page in range(1, max_pages + 1):
        try:
            params = {'pn': page}
            resp = requests.get(base_url, headers=HEADERS, params=params, timeout=15)
            if resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.text, 'lxml')
            cards = soup.select('[data-test="result-list-item"], .ResultList article, .HitListItem')
            if not cards:
                break

            for card in cards:
                try:
                    title_el = card.select_one('h3, .HitListItem__title, [data-test="title"]')
                    title = title_el.get_text(strip=True) if title_el else ''

                    price_el = card.select_one('[data-test="price"], .HitListItem__price, .Price')
                    price_text = price_el.get_text(strip=True) if price_el else ''
                    price = clean_price(price_text)

                    addr_el = card.select_one('[data-test="address"], .HitListItem__address')
                    address = addr_el.get_text(strip=True) if addr_el else ''

                    link_el = card.select_one('a[href]')
                    source_url = ''
                    external_id = ''
                    if link_el:
                        href = link_el.get('href', '')
                        if href.startswith('/'):
                            href = 'https://www.immoscout24.ch' + href
                        source_url = href
                        id_match = re.search(r'/(\d+)', href)
                        if id_match:
                            external_id = id_match.group(1)

                    if title or price:
                        results.append({
                            'external_id': external_id or f"is24-{hash(title+address)}",
                            'source': 'ImmoScout24',
                            'source_url': source_url,
                            'title': title,
                            'description': '',
                            'property_type': _guess_type(title),
                            'transaction': transaction,
                            'price': price,
                            'currency': 'CHF',
                            'price_unit': 'mois' if transaction == 'location' else 'total',
                            'rooms': clean_rooms(title),
                            'surface': None,
                            'floor': None,
                            'address': address,
                            'city': city,
                            'canton': CITY_CANTONS.get(city.lower(), ''),
                            'postal_code': _extract_postal(address),
                            'latitude': None, 'longitude': None,
                            'features': [], 'images': [],
                            'contact_name': None, 'contact_phone': None, 'contact_email': None,
                            'published_at': None,
                            'scraped_at': datetime.now().isoformat(),
                        })
                except Exception as e:
                    log.debug(f"ImmoScout card error: {e}")

            time.sleep(1)
        except Exception as e:
            log.error(f"ImmoScout page {page} error: {e}")
            break

    log.info(f"ImmoScout24: {len(results)} listings found")
    return results


# ============================================================
# COMPARIS
# ============================================================

def scrape_comparis(city="Neuchâtel", transaction="location", max_pages=2):
    """Scrape Comparis listings via their search."""
    log.info(f"Scraping Comparis: {city} ({transaction})")
    results = []

    tx = "rent" if transaction == "location" else "buy"
    # Comparis uses a JSON API
    api_url = f"https://www.comparis.ch/immobilien/result/list"
    params = {
        'requestobject': json.dumps({
            'DealType': 10 if transaction == 'location' else 20,
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
            'AdAgeInHoursMax': None,
            'Keyword': city,
            'WithImagesOnly': None,
            'WithPointsOnly': None,
            'Radius': None,
            'MinAvailableDate': None,
            'MinChangeDate': None,
            'LocationSearchString': city,
            'Sort': 4,  # By date
            'HasNewBuildingProject': None,
            'SearchAbo': None,
            'LivingFloorRange': None,
            'Page': 1
        })
    }

    try:
        import json as json_mod
        resp = requests.get(api_url, headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 200:
            try:
                data = resp.json()
                items = data.get('Items', data.get('items', []))
                for item in items[:20]:
                    results.append({
                        'external_id': f"comp-{item.get('Id', '')}",
                        'source': 'Comparis',
                        'source_url': f"https://www.comparis.ch/immobilien/angebot/show/{item.get('Id', '')}",
                        'title': item.get('Title', ''),
                        'description': item.get('Description', ''),
                        'property_type': _guess_type(item.get('Title', '')),
                        'transaction': transaction,
                        'price': item.get('Price'),
                        'currency': 'CHF',
                        'price_unit': 'mois' if transaction == 'location' else 'total',
                        'rooms': item.get('NumberOfRooms'),
                        'surface': item.get('LivingSpace'),
                        'floor': item.get('Floor'),
                        'address': item.get('Street', '') + ', ' + item.get('CityName', city),
                        'city': item.get('CityName', city),
                        'canton': CITY_CANTONS.get(city.lower(), ''),
                        'postal_code': str(item.get('Zip', '')),
                        'latitude': item.get('Latitude'),
                        'longitude': item.get('Longitude'),
                        'features': [],
                        'images': [item.get('ImageUrl', '')] if item.get('ImageUrl') else [],
                        'contact_name': None, 'contact_phone': None, 'contact_email': None,
                        'published_at': item.get('PublishDate'),
                        'scraped_at': datetime.now().isoformat(),
                    })
            except (ValueError, KeyError) as e:
                log.debug(f"Comparis JSON parse error: {e}")
    except Exception as e:
        log.error(f"Comparis error: {e}")

    log.info(f"Comparis: {len(results)} listings found")
    return results


# ============================================================
# FLATFOX
# ============================================================

def scrape_flatfox(city="Neuchâtel", transaction="location", max_pages=2):
    """Scrape Flatfox listings via their public API."""
    log.info(f"Scraping Flatfox: {city} ({transaction})")
    results = []

    offer_type = 'RENT' if transaction == 'location' else 'SALE'
    api_url = "https://flatfox.ch/api/v1/public/listings/"
    params = {
        'city': city,
        'offer_type': offer_type,
        'ordering': '-created',
        'limit': 20,
    }

    try:
        resp = requests.get(api_url, headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get('results', data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for item in items:
                    pk = item.get('pk', item.get('id', ''))
                    results.append({
                        'external_id': f"ff-{pk}",
                        'source': 'Flatfox',
                        'source_url': f"https://flatfox.ch/fr/flat/{item.get('slug', pk)}/",
                        'title': item.get('title', ''),
                        'description': item.get('description', ''),
                        'property_type': _guess_type(item.get('object_category', '') + ' ' + item.get('title', '')),
                        'transaction': transaction,
                        'price': item.get('rent_gross') or item.get('price_display'),
                        'currency': 'CHF',
                        'price_unit': 'mois' if transaction == 'location' else 'total',
                        'rooms': item.get('number_of_rooms'),
                        'surface': item.get('surface_living'),
                        'floor': item.get('floor'),
                        'address': (item.get('street', '') + ' ' + str(item.get('street_number', ''))).strip()
                                   + ', ' + (item.get('city', city)),
                        'city': item.get('city', city),
                        'canton': CITY_CANTONS.get(city.lower(), ''),
                        'postal_code': str(item.get('zipcode', '')),
                        'latitude': item.get('latitude'),
                        'longitude': item.get('longitude'),
                        'features': item.get('attributes', []) or [],
                        'images': [img.get('url', '') for img in (item.get('images', []) or [])[:5]],
                        'contact_name': None, 'contact_phone': None, 'contact_email': None,
                        'published_at': item.get('created'),
                        'scraped_at': datetime.now().isoformat(),
                    })
    except Exception as e:
        log.error(f"Flatfox error: {e}")

    log.info(f"Flatfox: {len(results)} listings found")
    return results


# ============================================================
# HELPERS
# ============================================================

def _guess_type(text):
    """Guess property type from title text."""
    if not text:
        return 'appartement'
    t = text.lower()
    if any(w in t for w in ['maison', 'villa', 'chalet']):
        return 'maison'
    if 'studio' in t:
        return 'studio'
    if 'loft' in t:
        return 'loft'
    if 'attique' in t or 'penthouse' in t:
        return 'attique'
    if 'duplex' in t:
        return 'duplex'
    return 'appartement'


def _extract_postal(address):
    """Extract Swiss postal code from address."""
    if not address:
        return None
    m = re.search(r'\b(\d{4})\b', address)
    return m.group(1) if m else None


# ============================================================
# MAIN SCRAPER
# ============================================================

import json

def scrape_all(city="Neuchâtel", transaction="location"):
    """Scrape all portals for a given city and transaction type."""
    all_results = []

    scrapers = [
        ('Homegate', scrape_homegate),
        ('ImmoScout24', scrape_immoscout),
        ('Comparis', scrape_comparis),
        ('Flatfox', scrape_flatfox),
    ]

    for name, scraper in scrapers:
        try:
            results = scraper(city=city, transaction=transaction)
            all_results.extend(results)
            log.info(f"{name}: {len(results)} results")
        except Exception as e:
            log.error(f"{name} failed: {e}")
        time.sleep(2)  # Pause between portals

    log.info(f"Total: {len(all_results)} listings from {len(scrapers)} portals")
    return all_results


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
            log.debug(f"Save error: {e}")

    db.commit()
    cur.close()
    log.info(f"Saved {saved}/{len(listings)} to database")
    return saved
