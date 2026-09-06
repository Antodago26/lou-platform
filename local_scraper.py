#!/usr/bin/env python3
"""
Bon Home — Script de scraping LOCAL
Tourne sur ton Mac (IP résidentielle = pas de blocage Cloudflare).
Envoie les annonces au backend via /api/import.

Usage:
    python3 local_scraper.py
    python3 local_scraper.py --city Genève --transaction achat
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False
    print("⚠️  undetected-chromedriver non installé.")
    print("   Installez-le avec: pip3 install undetected-chromedriver")
    print("   Sans ça, Homegate et ImmoScout24 seront bloqués.\n")

# ============================================================
# CONFIG
# ============================================================

BACKEND_URL = os.environ.get("LOU_BACKEND_URL", "https://lou-platform.onrender.com")
LOGIN_EMAIL = os.environ.get("LOU_LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("LOU_LOGIN_PASSWORD", "")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

CITY_CANTONS = {
    'lausanne': 'VD', 'geneve': 'GE', 'genève': 'GE', 'neuchatel': 'NE',
    'neuchâtel': 'NE', 'fribourg': 'FR', 'sion': 'VS', 'montreux': 'VD',
    'nyon': 'VD', 'morges': 'VD', 'yverdon': 'VD', 'vevey': 'VD',
    'renens': 'VD', 'prilly': 'VD', 'pully': 'VD', 'ecublens': 'VD',
    'carouge': 'GE', 'meyrin': 'GE', 'sierre': 'VS', 'martigny': 'VS',
    'la chaux-de-fonds': 'NE', 'bienne': 'BE', 'delemont': 'JU',
    'zurich': 'ZH', 'bern': 'BE', 'basel': 'BS', 'lugano': 'TI',
}


# ============================================================
# HELPERS
# ============================================================

def log(source, msg):
    print(f"  [{source}] {msg}")


def clean_price(val):
    if val is None:
        return None
    text = str(val).replace('\u2019', "").replace("'", "").replace(',', '').replace('.–', '').replace('.-', '')
    nums = re.findall(r'\d+', text)
    if nums:
        n = int(nums[0])
        return n if n > 0 else None
    return None


def clean_rooms(text):
    if not text:
        return None
    text = str(text).replace('½', '.5')
    nums = re.findall(r'[\d.]+', text)
    return float(nums[0]) if nums else None


def clean_surface(text):
    if not text:
        return None
    nums = re.findall(r'(\d+)\s*m', str(text))
    return int(nums[0]) if nums else None


def guess_type(text):
    if not text:
        return 'appartement'
    t = text.lower()
    if any(w in t for w in ['maison', 'villa', 'chalet', 'house']):
        return 'maison'
    if 'studio' in t:
        return 'studio'
    if 'loft' in t:
        return 'loft'
    if any(w in t for w in ['attique', 'penthouse']):
        return 'attique'
    if 'duplex' in t:
        return 'duplex'
    return 'appartement'


def extract_postal(address):
    if not address:
        return None
    m = re.search(r'\b(\d{4})\b', address)
    return m.group(1) if m else None


def make_property(external_id, source, source_url, title, description,
                  property_type, transaction, price, rooms, surface,
                  floor, address, city, canton, postal_code,
                  latitude, longitude, features, images, published_at):
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


_browser = None

def get_browser():
    """Get or create an undetected Chrome browser instance."""
    global _browser
    if _browser is None and HAS_UC:
        log("Chrome", "Lancement de Chrome (invisible)...")
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--lang=fr-CH')
        options.add_argument('--window-size=1200,800')
        _browser = uc.Chrome(options=options, version_main=146)
        _browser.implicitly_wait(5)
    return _browser


def close_browser():
    """Close the Chrome browser if open."""
    global _browser
    if _browser:
        try:
            _browser.quit()
        except Exception:
            pass
        _browser = None


def fetch(url, session=None, use_browser=False):
    """Fetch a URL. Returns (status, html)."""
    if use_browser and HAS_UC:
        try:
            driver = get_browser()
            driver.get(url)
            time.sleep(5)  # Wait for page load
            html = driver.page_source
            # Check if Cloudflare challenge
            if 'Just a moment' in html or 'challenge-platform' in html or 'Vérification' in html:
                print("\n  ⚠️  CAPTCHA détecté! Résolvez-le dans la fenêtre Chrome...")
                print("     (faites glisser le slider ou cochez la case)")
                # Wait for user to solve captcha (check every 3s, max 60s)
                for i in range(20):
                    time.sleep(3)
                    html = driver.page_source
                    if 'Just a moment' not in html and 'challenge-platform' not in html and 'Vérification' not in html:
                        print("  ✅ CAPTCHA résolu!")
                        break
                else:
                    print("  ⏰ Timeout CAPTCHA (60s)")
            return 200, html
        except Exception as e:
            log("Chrome", f"Error: {e}")
            return 0, ''
    s = session or requests
    try:
        r = s.get(url, headers=HEADERS, timeout=30)
        return r.status_code, r.text
    except Exception as e:
        log("HTTP", f"Error: {e}")
        return 0, ''


# ============================================================
# SCRAPERS
# ============================================================

def scrape_homegate(city="Lausanne", transaction="location", max_pages=3):
    log("Homegate", f"Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e')
    session = requests.Session()
    session.headers.update(HEADERS)

    for page in range(1, max_pages + 1):
        url = f"https://www.homegate.ch/{tx}/real-estate/city-{slug}/matching-list?ep={page}"
        status, html = fetch(url, use_browser=True)

        if status != 200:
            log("Homegate", f"Page {page}: HTTP {status}")
            break

        # Debug: save HTML
        if '--debug' in sys.argv:
            with open(f'debug_homegate_p{page}.html', 'w') as f:
                f.write(html)
            log("Homegate", f"Page {page}: HTML saved ({len(html)} bytes)")
            # Show key indicators
            has_cf = 'Just a moment' in html or 'challenge-platform' in html
            has_cards = 'result-list-item' in html
            log("Homegate", f"  Cloudflare: {has_cf}, Cards marker: {has_cards}")

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('[data-test="result-list-item"]')
        log("Homegate", f"Page {page}: {len(cards)} annonces")

        if not cards:
            break

        for card in cards:
            try:
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.homegate.ch' + href
                eid = re.search(r'/(\d+)', href)
                lid = eid.group(1) if eid else ''
                if not lid:
                    continue

                card_text = card.get_text(' ', strip=True)

                # Price
                price = None
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'\u2019]+)", card_text)
                if not price_match:
                    price_match = re.search(r"([\d'\u2019]+)\s*(?:CHF|Fr|\.–|/mois|/m)", card_text)
                if price_match:
                    price = clean_price(price_match.group(1))

                # Rooms
                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pièce|piece|room|Zimmer|pi\.)', card_text)
                if not rooms_match:
                    rooms_match = re.search(r'(\d+[.,]5)\b', card_text)
                if rooms_match:
                    rooms = clean_rooms(rooms_match.group(1))

                # Surface
                surface = None
                surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surface_match:
                    surface = int(surface_match.group(1))

                # Address
                address = ''
                addr_match = re.search(r'(\d{4}\s+\w[\w\s-]+)', card_text)
                if addr_match:
                    address = addr_match.group(1).strip()[:80]

                # Title
                title = ''
                for el in card.select('h2, h3, [class*="title"], [class*="Title"]'):
                    t = el.get_text(strip=True)
                    if t and len(t) > 3:
                        title = t
                        break
                if not title:
                    parts = []
                    if rooms:
                        parts.append(f"{rooms} pièces")
                    if surface:
                        parts.append(f"{surface} m²")
                    title = ', '.join(parts) if parts else 'Bien immobilier'

                results.append(make_property(
                    external_id=f"hg-{lid}", source='Homegate',
                    source_url=href, title=title, description='',
                    property_type=guess_type(card_text), transaction=transaction,
                    price=price, rooms=rooms, surface=surface, floor=None,
                    address=address, city=city,
                    canton=CITY_CANTONS.get(city.lower(), ''),
                    postal_code=extract_postal(address),
                    latitude=None, longitude=None,
                    features=[], images=[], published_at=None,
                ))
            except Exception as e:
                log("Homegate", f"  Card error: {e}")

        time.sleep(2)

    log("Homegate", f"Total: {len(results)} annonces")
    return results


def scrape_immoscout(city="Lausanne", transaction="location", max_pages=3):
    log("ImmoScout24", f"Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e').replace('è', 'e')
    session = requests.Session()
    session.headers.update(HEADERS)

    for page in range(1, max_pages + 1):
        url = f"https://www.immoscout24.ch/fr/immobilier/{tx}/lieu-{slug}?pn={page}"
        status, html = fetch(url, use_browser=True)

        if status != 200:
            log("ImmoScout24", f"Page {page}: HTTP {status}")
            break

        # Debug: save HTML
        if '--debug' in sys.argv:
            with open(f'debug_immoscout_p{page}.html', 'w') as f:
                f.write(html)
            has_cf = 'Just a moment' in html or 'challenge-platform' in html
            log("ImmoScout24", f"Page {page}: HTML saved ({len(html)} bytes), Cloudflare: {has_cf}")

        soup = BeautifulSoup(html, 'html.parser')
        # Try multiple selectors
        cards = soup.select('[data-test="result-list-item"], article, [class*="ResultList"] > div > div')
        log("ImmoScout24", f"Page {page}: {len(cards)} éléments")

        for card in cards:
            try:
                link_el = card.select_one('a[href*="/fr/d/"], a[href*="/fr/flat/"], a[href*="immoscout24"]')
                if not link_el:
                    link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if not href or href == '#':
                    continue
                if href.startswith('/'):
                    href = 'https://www.immoscout24.ch' + href

                eid = re.search(r'/d/(\d+)', href) or re.search(r'/(\d+)', href)
                lid = eid.group(1) if eid else ''
                if not lid:
                    continue

                card_text = card.get_text(' ', strip=True)

                price = None
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'\u2019]+)", card_text)
                if price_match:
                    price = clean_price(price_match.group(1))

                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:pièce|piece|room|Zimmer)', card_text)
                if rooms_match:
                    rooms = clean_rooms(rooms_match.group(1))

                surface = None
                surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surface_match:
                    surface = int(surface_match.group(1))

                address = ''
                addr_match = re.search(r'(\d{4}\s+\w[\w\s-]+)', card_text)
                if addr_match:
                    address = addr_match.group(1).strip()[:80]

                title = ''
                for el in card.select('h2, h3, [class*="title"], [class*="Title"]'):
                    t = el.get_text(strip=True)
                    if t and len(t) > 3:
                        title = t
                        break

                results.append(make_property(
                    external_id=f"is24-{lid}", source='ImmoScout24',
                    source_url=href, title=title or 'Bien immobilier',
                    description='', property_type=guess_type(card_text),
                    transaction=transaction, price=price, rooms=rooms,
                    surface=surface, floor=None, address=address, city=city,
                    canton=CITY_CANTONS.get(city.lower(), ''),
                    postal_code=extract_postal(address),
                    latitude=None, longitude=None,
                    features=[], images=[], published_at=None,
                ))
            except Exception:
                pass

        time.sleep(2)

    log("ImmoScout24", f"Total: {len(results)} annonces")
    return results


def scrape_immobilier_ch(city="Lausanne", transaction="location", max_pages=2):
    log("Immobilier.ch", f"Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    # Map city to canton for URL
    canton_map = {'lausanne': 'vaud', 'geneve': 'geneve', 'genève': 'geneve',
                  'nyon': 'vaud', 'montreux': 'vaud', 'morges': 'vaud',
                  'fribourg': 'fribourg', 'neuchatel': 'neuchatel', 'sion': 'valais'}
    canton_slug = canton_map.get(city.lower(), 'vaud')
    city_slug = city.lower().replace(' ', '-').replace('é', 'e').replace('è', 'e').replace('â', 'a')

    for page in range(1, max_pages + 1):
        url = f"https://www.immobilier.ch/en/{tx}/apartment-house/{canton_slug}/{city_slug}/page-{page}"
        status, html = fetch(url, use_browser=True)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        # Find all property links
        cards = soup.select('a[href*="/en/{}/"]'.format(tx))
        # Also try generic selectors
        if len(cards) < 3:
            cards = soup.select('.filter-item, .item-listing, article, [class*="property"], [class*="listing"], [class*="result"]')
        log("Immobilier.ch", f"Page {page}: {len(cards)} éléments")

        for card in cards:
            try:
                card_text = card.get_text(' ', strip=True)
                if len(card_text) < 20:
                    continue

                href = card.get('href', '')
                if not href:
                    link_el = card.select_one('a[href]')
                    href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.immobilier.ch' + href
                eid = re.search(r'/(\d+)', href)

                price = None
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'\u2019]+)", card_text)
                if price_match:
                    price = clean_price(price_match.group(1))

                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:room|pièce|piece|Zimmer)', card_text)
                if rooms_match:
                    rooms = clean_rooms(rooms_match.group(1))

                surface = None
                surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surface_match:
                    surface = int(surface_match.group(1))

                title = card_text[:80].strip()

                if price or rooms:
                    results.append(make_property(
                        external_id=f"imch-{eid.group(1) if eid else hash(card_text[:50])}",
                        source='Immobilier.ch', source_url=href,
                        title=title, description='',
                        property_type=guess_type(card_text), transaction=transaction,
                        price=price, rooms=rooms, surface=surface,
                        floor=None, address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(2)

    log("Immobilier.ch", f"Total: {len(results)} annonces")
    return results


def scrape_anibis(city="Lausanne", transaction="location", max_pages=2):
    log("Anibis", f"Searching {city} ({transaction})")
    results = []

    # Anibis URLs with encoded filters per city
    anibis_urls = {
        'lausanne': 'https://www.anibis.ch/fr/q/immobilier-lausanne-appartements-louer/Ak8CqcmVhbEVzdGF0ZZSSkqtsaXN0aW5nVHlwZalhcGFydG1lbnSSqXByaWNlVHlwZaRSRU5UwMCRk6hsb2NhdGlvbrFnZW8tY2l0eS1sYXVzYW5uZcA',
        'geneve': 'https://www.anibis.ch/fr/q/immobilier-geneve-appartements-louer/Ak8CqcmVhbEVzdGF0ZZSSkqtsaXN0aW5nVHlwZalhcGFydG1lbnSSqXByaWNlVHlwZaRSRU5UwMCRk6hsb2NhdGlvbq9nZW8tY2l0eS1nZW5ldmXA',
    }
    base_url = anibis_urls.get(city.lower(),
        f"https://www.anibis.ch/fr/c/immobilier-cherche-a-louer/localite-{city.lower()}")

    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?sdc={page * 10}"
        status, html = fetch(url, use_browser=True)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.listing-card, .ItemCard, article, [class*="listing"], [class*="Listing"]')
        log("Anibis", f"Page {page}: {len(cards)} éléments")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, [class*="title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('[class*="price"]')
                price = clean_price(price_el.get_text(strip=True)) if price_el else None
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.anibis.ch' + href
                eid = re.search(r'/(\d+)', href)

                if title or price:
                    results.append(make_property(
                        external_id=f"anibis-{eid.group(1) if eid else hash(title)}",
                        source='Anibis', source_url=href,
                        title=title, description='',
                        property_type=guess_type(title), transaction=transaction,
                        price=price, rooms=clean_rooms(title),
                        surface=None, floor=None, address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(2)

    log("Anibis", f"Total: {len(results)} annonces")
    return results


def scrape_acheter_louer(city="Lausanne", transaction="location", max_pages=2):
    log("Acheter-Louer", f"Searching {city} ({transaction})")
    results = []
    tx = "louer" if transaction == "location" else "acheter"

    for page in range(1, max_pages + 1):
        url = f"https://www.acheter-louer.ch/{tx}/{city.lower()}?page={page}"
        status, html = fetch(url)

        if status != 200:
            break

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.property-item, .listing-card, article, [class*="result"]')
        log("Acheter-Louer", f"Page {page}: {len(cards)} éléments")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, [class*="title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('[class*="price"]')
                price = clean_price(price_el.get_text(strip=True)) if price_el else None
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.acheter-louer.ch' + href

                if title or price:
                    results.append(make_property(
                        external_id=f"al-{hash(title+href)}",
                        source='Acheter-Louer', source_url=href,
                        title=title, description='',
                        property_type=guess_type(title), transaction=transaction,
                        price=price, rooms=clean_rooms(title),
                        surface=None, floor=None, address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

        time.sleep(2)

    log("Acheter-Louer", f"Total: {len(results)} annonces")
    return results


def scrape_flatfox(city="Lausanne", transaction="location", limit=30):
    log("Flatfox", f"Searching {city} ({transaction})")
    results = []
    offer_type = 'RENT' if transaction == 'location' else 'SALE'
    city_slug = city.lower().replace(' ', '-')

    # Scrape search page with geo bounds for Switzerland
    url = f"https://flatfox.ch/en/search/?city={city}&east=10.492340&north=48.590424&offer_type={offer_type}&ordering=-created&south=44.994258&take=48&west=5.956080"
    status, html = fetch(url, use_browser=True)

    if status == 200 and html:
        soup = BeautifulSoup(html, 'html.parser')
        # Find listing links
        links = soup.select('a[href*="/flat/"]')
        log("Flatfox", f"{len(links)} liens trouvés")

        seen_hrefs = set()
        for link in links:
            try:
                href = link.get('href', '')
                if href.startswith('/'):
                    href = 'https://flatfox.ch' + href
                if '/flat/' not in href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                card = link
                # Walk up to find the card container
                for _ in range(5):
                    parent = card.parent
                    if parent and len(parent.get_text(strip=True)) > len(card.get_text(strip=True)):
                        card = parent
                    else:
                        break

                card_text = card.get_text(' ', strip=True)
                if len(card_text) < 15:
                    continue

                slug = re.search(r'/flat/([^/]+)', href)
                eid = slug.group(1) if slug else str(hash(href))

                price = None
                price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'\u2019]+)", card_text)
                if price_match:
                    price = clean_price(price_match.group(1))

                rooms = None
                rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:room|pièce|Zimmer)', card_text)
                if rooms_match:
                    rooms = clean_rooms(rooms_match.group(1))

                surface = None
                surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                if surface_match:
                    surface = int(surface_match.group(1))

                title = ''
                for el in card.select('h2, h3, [class*="title"]'):
                    t = el.get_text(strip=True)
                    if t and len(t) > 3:
                        title = t
                        break
                if not title:
                    title = card_text[:80]

                if price or rooms:
                    results.append(make_property(
                        external_id=f"ff-{eid}", source='Flatfox',
                        source_url=href, title=title, description='',
                        property_type=guess_type(card_text), transaction=transaction,
                        price=price, rooms=rooms, surface=surface, floor=None,
                        address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

    log("Flatfox", f"Total: {len(results)} annonces")
    return results


def scrape_comparis(city="Lausanne", transaction="location"):
    log("Comparis", f"Searching {city} ({transaction})")
    results = []

    try:
        deal = "mieten" if transaction == "location" else "kaufen"
        url = f"https://en.comparis.ch/immobilien/marktplatz/wohnung/{deal}?location={city}"
        status, html = fetch(url, use_browser=True)
        log("Comparis", f"HTTP {status} ({len(html)} bytes)")

        if status == 200 and html:
            soup = BeautifulSoup(html, 'html.parser')
            # Find property cards
            cards = soup.select('a[href*="/immobilien/"], [class*="ListItem"], [class*="result"], article')
            log("Comparis", f"{len(cards)} éléments")

            seen = set()
            for card in cards:
                try:
                    card_text = card.get_text(' ', strip=True)
                    if len(card_text) < 20:
                        continue

                    href = card.get('href', '')
                    if not href:
                        link_el = card.select_one('a[href]')
                        href = link_el.get('href', '') if link_el else ''
                    if href.startswith('/'):
                        href = 'https://www.comparis.ch' + href

                    eid = re.search(r'/(\d+)', href)
                    lid = eid.group(1) if eid else str(hash(card_text[:50]))
                    if lid in seen:
                        continue
                    seen.add(lid)

                    price = None
                    price_match = re.search(r"(?:CHF|Fr\.?)\s*([\d'\u2019]+)", card_text)
                    if price_match:
                        price = clean_price(price_match.group(1))

                    rooms = None
                    rooms_match = re.search(r'(\d+[.,]?5?)\s*(?:room|pièce|Zimmer|Zi)', card_text)
                    if rooms_match:
                        rooms = clean_rooms(rooms_match.group(1))

                    surface = None
                    surface_match = re.search(r'(\d+)\s*m[²2]', card_text)
                    if surface_match:
                        surface = int(surface_match.group(1))

                    if price or rooms:
                        results.append(make_property(
                            external_id=f"comp-{lid}",
                            source='Comparis', source_url=href,
                            title=card_text[:80], description='',
                            property_type=guess_type(card_text), transaction=transaction,
                            price=price, rooms=rooms, surface=surface, floor=None,
                            address=city, city=city,
                            canton=CITY_CANTONS.get(city.lower(), ''),
                            postal_code=None, latitude=None, longitude=None,
                            features=[], images=[], published_at=None,
                        ))
                except Exception:
                    pass
    except Exception as e:
        log("Comparis", f"Error: {e}")

    log("Comparis", f"Total: {len(results)} annonces")
    return results


def scrape_properstar(city="Lausanne", transaction="location"):
    log("Properstar", f"Searching {city} ({transaction})")
    results = []
    tx = "rent" if transaction == "location" else "buy"
    slug = city.lower().replace(' ', '-')

    url = f"https://www.properstar.ch/switzerland/{slug}/{tx}/apartment"
    status, html = fetch(url)

    if status == 200:
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('.listing-card, .property-card, article, [class*="listing"]')
        log("Properstar", f"{len(cards)} éléments")

        for card in cards:
            try:
                title_el = card.select_one('h3, h2, [class*="title"]')
                title = title_el.get_text(strip=True) if title_el else ''
                price_el = card.select_one('[class*="price"]')
                price = clean_price(price_el.get_text(strip=True)) if price_el else None
                link_el = card.select_one('a[href]')
                href = link_el.get('href', '') if link_el else ''
                if href.startswith('/'):
                    href = 'https://www.properstar.ch' + href

                if title or price:
                    results.append(make_property(
                        external_id=f"ps-{hash(title+href)}",
                        source='Properstar', source_url=href,
                        title=title, description='',
                        property_type=guess_type(title), transaction=transaction,
                        price=price, rooms=clean_rooms(title),
                        surface=None, floor=None, address=city, city=city,
                        canton=CITY_CANTONS.get(city.lower(), ''),
                        postal_code=None,
                        latitude=None, longitude=None,
                        features=[], images=[], published_at=None,
                    ))
            except Exception:
                pass

    log("Properstar", f"Total: {len(results)} annonces")
    return results


# ============================================================
# MAIN
# ============================================================

def _source_disabled(name):
    """Homegate + ImmoScout24 sont sortis du scope (drop produit 30/04). On
    respecte les memes flags que le cron : ENABLE_HOMEGATE / ENABLE_IMMOSCOUT24
    a 'false'/'0'/'no'/'off' desactivent le scraper. Defaut : desactives, comme
    en prod — mettre le flag a 'true' pour les reactiver ponctuellement."""
    flag = {'Homegate': 'ENABLE_HOMEGATE', 'ImmoScout24': 'ENABLE_IMMOSCOUT24'}.get(name)
    if not flag:
        return False
    raw = os.environ.get(flag)
    if raw is None:
        return True  # defaut : desactive (aligne sur la prod)
    return raw.strip().lower() in ('false', '0', 'no', 'off')


def run_all(city="Lausanne", transaction="location"):
    """Run all scrapers and return deduplicated results."""
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
    scrapers = [(n, fn) for n, fn in scrapers if not _source_disabled(n)]

    for name, scraper in scrapers:
        try:
            items = scraper(city=city, transaction=transaction)
            all_results.extend(items)
        except Exception as e:
            log(name, f"FAILED: {e}")
        time.sleep(1)

    # Deduplicate
    seen = set()
    unique = []
    for p in all_results:
        key = p['external_id']
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


def send_to_backend(listings, token):
    """Send listings to the backend via /api/import."""
    url = f"{BACKEND_URL}/api/import"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    # Send in batches of 50
    batch_size = 50
    total_saved = 0
    for i in range(0, len(listings), batch_size):
        batch = listings[i:i+batch_size]
        try:
            r = requests.post(url, json={'listings': batch}, headers=headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                total_saved += data.get('saved', 0)
                log("Backend", f"Batch {i//batch_size+1}: {data.get('saved', 0)} saved")
            else:
                log("Backend", f"Batch {i//batch_size+1}: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            log("Backend", f"Batch {i//batch_size+1}: Error - {e}")

    return total_saved


def login():
    """Login to backend and get JWT token."""
    try:
        r = requests.post(f"{BACKEND_URL}/api/login", json={
            'email': LOGIN_EMAIL,
            'password': LOGIN_PASSWORD,
        }, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get('token')
        else:
            log("Auth", f"Login failed: {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        log("Auth", f"Login error: {e}")
        return None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bon Home - Local Scraper')
    parser.add_argument('--city', default='Lausanne', help='Ville à scraper (default: Lausanne)')
    parser.add_argument('--transaction', default='location', choices=['location', 'achat'],
                        help='Type de transaction (default: location)')
    parser.add_argument('--debug', action='store_true', help='Sauvegarde le HTML pour debug')
    args = parser.parse_args()

    print(f"\n🔍 Bon Home — Scraping local")
    print(f"   Ville: {args.city}")
    print(f"   Transaction: {args.transaction}")
    print(f"   Backend: {BACKEND_URL}")
    print()

    # Step 1: Login
    print("📡 Connexion au backend...")
    token = login()
    if not token:
        print("❌ Impossible de se connecter au backend. Vérifiez les identifiants.")
        exit(1)
    print("✅ Connecté!\n")

    # Step 2: Scrape all portals
    print("🔍 Scraping en cours...\n")
    listings = run_all(city=args.city, transaction=args.transaction)
    print(f"\n📊 Total: {len(listings)} annonces uniques trouvées\n")

    if not listings:
        print("⚠️  Aucune annonce trouvée. Vérifiez votre connexion internet.")
        exit(0)

    # Step 3: Send to backend
    print("📤 Envoi au backend...\n")
    saved = send_to_backend(listings, token)
    print(f"\n📊 {saved} annonces enregistrées en base de données.")

    # Step 4: Trigger scoring
    print("\n⚡ Scoring des annonces par rapport à votre profil...")
    try:
        r = requests.post(f"{BACKEND_URL}/api/score", headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }, timeout=60)
        if r.status_code == 200:
            data = r.json()
            print(f"✅ {data.get('scored', 0)} annonces scorées!")
        else:
            print(f"⚠️  Scoring: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"⚠️  Scoring error: {e}")

    print(f"\n✅ Terminé! Connectez-vous sur {BACKEND_URL} pour voir vos résultats.\n")

    # Cleanup
    close_browser()
