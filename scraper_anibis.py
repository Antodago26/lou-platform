"""Bon Home : scraper anibis.ch (marketplace SMG, surtout des particuliers).

Comment anibis fonctionne (verifie le 6 septembre 2026)
------------------------------------------------------
- Site Next.js servi en HTML complet : les resultats de recherche sont dans
  <script id="__NEXT_DATA__"> (React Query dehydrate), 30 annonces par page,
  pagination par ?page=N. Pas de proxy ni de rendu JS necessaire, curl_cffi
  avec empreinte Chrome suffit.
- L'URL de recherche est /fr/q/<slug>/<token>. Le token est un msgpack
  encode en base64url, decale de 6 bits :
    [None, 'realEstate', [[['listingType','apartment'], ['priceType','RENT']],
                          None, None, [['location', 'geo-city-neuchatel', 10]]]]
  On le fabrique nous-memes : encode_search_token().
- La liste donne : listingID, titre, texte, NPA, localite, canton, date,
  prix formate, vignette. La fiche /fr/vi/<frSlug>/<listingID> ajoute :
  pieces, surface, adresse, GPS, toutes les photos.

Ce module ne touche pas a la base : il renvoie des dicts au format
_make_property, comme les autres scrapers.
"""
import base64
import logging
import re
import time
import json
from datetime import datetime

import msgpack

from scrapers import _make_property, CITY_CANTONS

log = logging.getLogger('lou-app')

SOURCE = 'Anibis'
BASE = 'https://www.anibis.ch'
_HEADERS = {'Accept-Language': 'fr-CH,fr;q=0.9'}
_DETAIL_DELAY_S = 0.6
_PAGE_DELAY_S = 0.4
_PAGE_SIZE = 30

_CANTON_SLUGS = {
    'NE': 'neuchatel', 'VD': 'vaud', 'GE': 'geneve', 'FR': 'fribourg',
    'VS': 'valais', 'JU': 'jura', 'BE': 'berne',
}


# ------------------------------------------------------------------
# Jeton de recherche
# ------------------------------------------------------------------
def encode_search_token(obj):
    """msgpack -> 6 bits de prefixe a zero -> base64url sans padding."""
    mp = msgpack.packb(obj, use_bin_type=True)
    bits = '000000' + ''.join(f'{b:08b}' for b in mp)
    bits += '0' * (-len(bits) % 8)
    by = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    return base64.urlsafe_b64encode(by).decode().rstrip('=')


def decode_search_token(tok):
    raw = base64.urlsafe_b64decode(tok + '=' * (-len(tok) % 4))
    bits = ''.join(f'{b:08b}' for b in raw)[6:]
    by = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - 7, 8))
    return msgpack.unpackb(by, raw=False, strict_map_key=False)


def build_search(transaction='location', listing_type='apartment',
                 city=None, radius_km=None, canton=None):
    """listing_type : 'apartment' | 'house' | None (tout l'immobilier).
    city + radius_km cible une localite (geo-city-<slug>), canton un canton
    entier (geo-canton-<slug>)."""
    filters = []
    if listing_type:
        filters.append(['listingType', listing_type])
    filters.append(['priceType', 'BUY' if transaction == 'achat' else 'RENT'])
    if canton:
        loc = [['location', f'geo-canton-{_CANTON_SLUGS.get(canton.upper(), canton.lower())}', None]]
    elif city:
        loc = [['location', f'geo-city-{_city_slug(city)}', radius_km]]
    else:
        loc = None
    return [None, 'realEstate', [filters, None, None, loc]]


def _city_slug(city):
    s = (city or '').strip().lower()
    s = (s.replace('â', 'a').replace('à', 'a').replace('é', 'e').replace('è', 'e')
          .replace('ê', 'e').replace('ô', 'o').replace('û', 'u').replace('ç', 'c')
          .replace('ü', 'u').replace('ö', 'o').replace('ä', 'a'))
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------
def _get(url, timeout=40):
    from curl_cffi import requests as cffi_requests
    r = cffi_requests.get(url, impersonate='chrome', timeout=timeout, headers=_HEADERS)
    return r.status_code, r.text


def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or '', re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _queries(nd):
    try:
        return nd['props']['pageProps']['dehydratedState']['queries']
    except Exception:
        return []


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------
def parse_price(text):
    """'2 280.- par mois' -> 2280 ; '1 250 000.-' -> 1250000 ; sinon None."""
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text.split('.-')[0])
    return int(digits) if digits else None


def parse_rooms_surface(text):
    """Cherche pieces et surface dans un texte libre (fallback sans fiche)."""
    t = (text or '').replace(',', '.')
    rooms = None
    m = re.search(r'(\d+(?:\.\d)?)\s*(?:p(?:i[eè]ces?|ces|\b)|pi[eè]ces?)', t, re.I)
    if m:
        try:
            rooms = float(m.group(1))
        except ValueError:
            rooms = None
    surface = None
    m = re.search(r'(\d{2,3})\s*m\s*(?:2|²)', t, re.I)
    if m:
        surface = int(m.group(1))
    return rooms, surface


_WANTED_RE = re.compile(r'^\s*(?:famille|couple|jeune|nous|je|on|personne|etudiant|étudiant)?\s*(?:s[ée]rieu\w*\s+)?(?:re)?cherch', re.I)


def is_wanted_ad(title, body=''):
    """True pour une demande (« Famille cherche maison »), pas une offre."""
    t = (title or '').strip()
    return bool(_WANTED_RE.search(t)) or bool(re.match(r'^\s*(?:re)?cherch', (body or '').strip(), re.I))


def clean_city(name):
    """'Colombier NE' -> 'Colombier', 'St-Blaise' -> 'Saint-Blaise', 'Biel/Bienne' -> 'Bienne'."""
    c = (name or '').strip()
    c = re.sub(r'\s*\(?\b(NE|VD|GE|FR|VS|JU|BE|ZH|BL|BS|SO|TI|AG|LU|SG|TG|SZ|ZG|GR|OW|NW|UR|GL|AR|AI|SH)\)?$', '', c)
    c = re.sub(r'^St[-. ]', 'Saint-', c)
    c = re.sub(r'^Ste[-. ]', 'Sainte-', c)
    if '/' in c:
        parts = [x.strip() for x in c.split('/')]
        c = parts[-1] if parts[-1] else parts[0]
    return c.strip()


def _listing_url(node):
    slug = ((node.get('seoInformation') or {}).get('frSlug') or '').strip('/')
    return f"{BASE}/fr/vi/{slug}/{node['listingID']}"


def parse_list_node(node, transaction, search_city=None):
    loc = node.get('localization') or {}
    pc = node.get('postcodeInformation') or {}
    canton = ((pc.get('canton') or {}).get('shortName') or '').upper()
    title = (loc.get('title') or '').strip()
    body = (loc.get('body') or '').strip()
    rooms, surface = parse_rooms_surface(title + ' ' + body)
    thumb = ((node.get('thumbnail') or {}).get('retinaRendition') or {}).get('src') \
        or ((node.get('thumbnail') or {}).get('normalRendition') or {}).get('src')
    ptype = 'maison' if re.search(r'\b(maison|villa|chalet)\b', title, re.I) else 'appartement'
    return {
        'external_id': str(node.get('listingID')),
        'source_url': _listing_url(node),
        'title': title,
        'description': body,
        'property_type': ptype,
        'transaction': transaction,
        'price': parse_price(node.get('formattedPrice')),
        'rooms': rooms,
        'surface': surface,
        'floor': None,
        'address': '',
        'city': clean_city(pc.get('locationName')) or search_city or '',
        'canton': canton,
        'postal_code': pc.get('postcode') or '',
        'latitude': None,
        'longitude': None,
        'features': _features_from_text(title + ' ' + body),
        'images': [thumb] if thumb else [],
        'published_at': node.get('timestamp'),
    }


_FEATURE_WORDS = [
    ('balcon', 'balcon'), ('terrasse', 'terrasse'), ('terasse', 'terrasse'), ('jardin', 'jardin'),
    ('parking', 'parking'), ('garage', 'garage'), ('ascenseur', 'ascenseur'),
    ('lave-vaisselle', 'lave-vaisselle'), ('vue', 'vue'), ('cheminée', 'cheminée'),
    ('meublé', 'meublé'), ('cave', 'cave'), ('minergie', 'minergie'),
]


def _features_from_text(text):
    t = (text or '').lower()
    out = []
    for word, label in _FEATURE_WORDS:
        if word in t and label not in out:
            out.append(label)
    return out


def parse_detail(nd):
    """Extrait pieces, surface, adresse, GPS, photos d'une fiche."""
    lst = None
    for q in _queries(nd):
        key = q.get('queryKey') or []
        if key and key[0] == 'GetListingDetails':
            data = (q.get('state') or {}).get('data') or {}
            lst = data.get('listing') if isinstance(data, dict) else None
            if lst is None and isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, dict) and 'listingID' in v:
                        lst = v
                        break
            break
    if not lst:
        return None
    out = {}
    for p in lst.get('properties') or []:
        pid = (p.get('listingPropertyID') or '').lower()
        txt = (p.get('text') or '')
        num = txt.replace(',', '.')
        if pid.endswith('realestaterooms'):
            try:
                out['rooms'] = float(re.sub(r'[^\d.]', '', num))
            except ValueError:
                pass
        elif pid.endswith('realestatesize'):
            try:
                out['surface'] = int(float(re.sub(r'[^\d.]', '', num)))
            except ValueError:
                pass
        elif pid.endswith('synthetic_address'):
            out['address'] = txt.strip()
    coords = lst.get('coordinates') or {}
    if coords.get('latitude') and coords.get('longitude'):
        out['latitude'] = float(coords['latitude'])
        out['longitude'] = float(coords['longitude'])
    imgs = []
    for im in lst.get('images') or []:
        src = ((im.get('rendition') or {}).get('src'))
        if src:
            imgs.append(src)
    if imgs:
        out['images'] = imgs
    if lst.get('address') and not out.get('address'):
        out['address'] = str(lst['address']).strip()
    seller = lst.get('sellerInfo') or {}
    if seller.get('alias'):
        out['contact_name'] = seller['alias'].strip()
    return out


# ------------------------------------------------------------------
# Scrape
# ------------------------------------------------------------------
def search_pages(search_obj, max_pages=10, on_page=None):
    """Itere les pages de resultats. Renvoie (nodes, total_count, pages_ok)."""
    tok = encode_search_token(search_obj)
    nodes, total, pages_ok = [], None, 0
    for page in range(1, max_pages + 1):
        url = f"{BASE}/fr/q/immobilier/{tok}" + (f"?page={page}" if page > 1 else '')
        status, html = _get(url)
        nd = _next_data(html) if status == 200 else None
        if not nd:
            log.warning(f"[Anibis] page {page} HTTP {status} sans __NEXT_DATA__")
            break
        listings = None
        for q in _queries(nd):
            key = q.get('queryKey') or []
            if key and key[0] == 'SearchListingsByConstraints':
                listings = ((q.get('state') or {}).get('data') or {}).get('listings')
                break
        if not listings:
            break
        total = listings.get('totalCount', total)
        edges = listings.get('edges') or []
        nodes.extend(e['node'] for e in edges if e.get('node'))
        pages_ok += 1
        if on_page:
            on_page(page, len(edges), total)
        if len(edges) < _PAGE_SIZE or (total is not None and page * _PAGE_SIZE >= total):
            break
        time.sleep(_PAGE_DELAY_S)
    return nodes, total, pages_ok


def scrape_anibis(city=None, transaction='location', canton=None, radius_km=10,
                  listing_types=('apartment', 'house'), max_pages=10,
                  fetch_details=True, known_ids=None, max_details=150,
                  return_meta=False):
    """Renvoie une liste de dicts _make_property.

    known_ids : identifiants deja en base, pour ne charger la fiche que des
    nouveautes. Les annonces connues sont renvoyees avec les donnees de la
    liste (prix, titre, date), ce qui suffit a les garder actives.
    """
    known_ids = set(known_ids or [])
    seen, results = set(), []
    meta = {'total': 0, 'read': 0, 'skipped': 0, 'complete': True}
    detail_budget = max_details if fetch_details else 0
    for lt in listing_types:
        obj = build_search(transaction=transaction, listing_type=lt, city=city,
                           radius_km=radius_km, canton=canton)
        nodes, total, pages_ok = search_pages(obj, max_pages=max_pages)
        log.info(f"[Anibis] {lt} {transaction} {canton or city}: {len(nodes)} annonces lues sur {total} ({pages_ok} pages)")
        meta['total'] += total or 0
        meta['read'] += len(nodes)
        if total is None or (total and len(nodes) < total):
            meta['complete'] = False
        for node in nodes:
            lid = str(node.get('listingID') or '')
            if not lid or lid in seen:
                continue
            seen.add(lid)
            base = parse_list_node(node, transaction, search_city=city)
            if base['price'] is None or is_wanted_ad(base['title'], base['description']):
                meta['skipped'] += 1
                continue
            if lt == 'house':
                base['property_type'] = 'maison'
            if lid not in known_ids and detail_budget > 0:
                try:
                    status, html = _get(base['source_url'])
                    det = parse_detail(_next_data(html)) if status == 200 else None
                    if det:
                        base.update({k: v for k, v in det.items() if v not in (None, '', [])})
                    detail_budget -= 1
                    time.sleep(_DETAIL_DELAY_S)
                except Exception as e:
                    log.warning(f"[Anibis] fiche {lid} en erreur: {e}")
            prop = _make_property(
                external_id=base['external_id'], source=SOURCE, source_url=base['source_url'],
                title=base['title'], description=base['description'],
                property_type=base['property_type'], transaction=transaction,
                price=base['price'], rooms=base['rooms'], surface=base['surface'],
                floor=base['floor'], address=base['address'], city=base['city'],
                canton=base['canton'], postal_code=base['postal_code'],
                latitude=base['latitude'], longitude=base['longitude'],
                features=base['features'], images=base['images'],
                published_at=base['published_at'], search_city=city,
            )
            if prop:
                if base.get('contact_name'):
                    prop['contact_name'] = base['contact_name']
                results.append(prop)
    if return_meta:
        return results, meta
    return results
