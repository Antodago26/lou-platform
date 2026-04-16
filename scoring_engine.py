"""
Bon Home — Scoring Engine
Score chaque annonce par rapport aux critères de recherche d'un utilisateur.

Usage:
    from scoring_engine import score_property, score_all_for_profile, GRADE_MAP

Intégrer dans le cron job après chaque scrape:
    score_all_for_profile(db, profile_id)
"""

import math
import re
import unicodedata
from datetime import datetime


def haversine(lat1, lon1, lat2, lon2):
    """Distance en km entre deux points GPS."""
    R = 6371  # rayon Terre en km
    lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Coordonnées centrales des villes suisses — fallback pour le scoring quand
# le portail (Properstar, Immobilier.ch, etc.) n'extrait pas lat/lng.
CITY_COORDS = {
    'lausanne':             (46.520, 6.632),
    'geneve':               (46.204, 6.143),
    'genève':               (46.204, 6.143),
    'neuchatel':            (46.992, 6.931),
    'neuchâtel':            (46.992, 6.931),
    'fribourg':             (46.806, 7.162),
    'sion':                 (46.227, 7.359),
    'montreux':             (46.434, 6.912),
    'nyon':                 (46.383, 6.239),
    'morges':               (46.510, 6.498),
    'yverdon':              (46.778, 6.641),
    'yverdon-les-bains':    (46.778, 6.641),
    'la chaux-de-fonds':    (47.100, 6.826),
    'bienne':               (47.141, 7.247),
    'biel':                 (47.141, 7.247),
    'delemont':             (47.366, 7.343),
    'delémont':             (47.366, 7.343),
    'berne':                (46.948, 7.447),
    'bern':                 (46.948, 7.447),
    'vevey':                (46.462, 6.843),
    'renens':               (46.538, 6.588),
    'zurich':               (47.377, 8.541),
    'zürich':               (47.377, 8.541),
    'basel':                (47.559, 7.588),
    'bâle':                 (47.559, 7.588),
    'lugano':               (46.004, 8.951),
    'lucerne':              (47.050, 8.308),
    'luzern':               (47.050, 8.308),
    'winterthur':           (47.500, 8.724),
    'st. gallen':           (47.424, 9.376),
    'carouge':              (46.180, 6.141),
    'meyrin':               (46.231, 6.080),
    'prilly':               (46.535, 6.603),
    'pully':                (46.510, 6.662),
    'ecublens':             (46.528, 6.561),
    'sierre':               (46.292, 7.535),
    'martigny':             (46.102, 7.074),
    # Canton NE — villages
    'auvernier':            (46.974, 6.881),
    'colombier':            (46.968, 6.869),
    'peseux':               (46.988, 6.859),
    'boudry':               (46.950, 6.838),
    'cortaillod':           (46.941, 6.845),
    'marin-epagnier':       (47.006, 6.984),
    'marin':                (47.006, 6.984),
    'hauterive':            (46.997, 6.941),
    'saint-blaise':         (47.011, 6.983),
    'le locle':             (47.056, 6.748),
    'val-de-travers':       (46.916, 6.606),
    'fleurier':             (46.902, 6.585),
    'milvignes':            (46.997, 6.916),
    'la tène':              (47.007, 6.988),
    'le landeron':          (47.056, 7.072),
    'bevaix':               (46.935, 6.820),
    'val-de-ruz':           (47.048, 6.908),
    'corcelles-cormondrèche': (46.982, 6.872),
    'corcelles':            (46.982, 6.872),
    'cernier':              (47.060, 6.903),
    'corcelles ne':         (46.982, 6.872),
}

# NPA (Swiss postal code) → (lat, lng, canonical_city_name)
# Used when the user types a postal code instead of a city name in their zone.
NPA_COORDS = {
    # Canton NE
    '2000': (46.992, 6.931, 'Neuchâtel'),
    '2002': (46.992, 6.931, 'Neuchâtel'),
    '2012': (46.974, 6.881, 'Auvernier'),
    '2013': (46.968, 6.869, 'Colombier'),
    '2014': (46.950, 6.838, 'Boudry'),
    '2016': (46.941, 6.845, 'Cortaillod'),
    '2017': (46.950, 6.838, 'Boudry'),
    '2034': (46.988, 6.859, 'Peseux'),
    '2035': (46.982, 6.872, 'Corcelles-Cormondrèche'),
    '2036': (46.982, 6.872, 'Cormondrèche'),
    '2037': (46.997, 6.916, 'Milvignes'),
    '2074': (47.006, 6.984, 'Marin-Epagnier'),
    '2075': (47.007, 6.988, 'La Tène'),
    '2088': (47.011, 6.983, 'Saint-Blaise'),
    '2068': (46.997, 6.941, 'Hauterive'),
    '2063': (46.920, 6.793, 'Engollon'),
    '2022': (46.935, 6.820, 'Bevaix'),
    '2023': (46.906, 6.782, 'Gorgier'),
    '2024': (47.011, 6.983, 'Saint-Aubin-Sauges'),
    '2025': (46.945, 6.694, 'Chez-le-Bart'),
    '2042': (47.048, 6.908, 'Val-de-Ruz'),
    '2046': (47.070, 6.937, 'Fontaines'),
    '2052': (47.010, 6.943, 'Fontainemelon'),
    '2056': (47.075, 6.862, 'Dombresson'),
    '2057': (47.100, 6.826, 'Villiers'),
    '2300': (47.100, 6.826, 'La Chaux-de-Fonds'),
    '2301': (47.100, 6.826, 'La Chaux-de-Fonds'),
    '2400': (47.056, 6.748, 'Le Locle'),
    '2105': (46.916, 6.606, 'Travers'),
    '2114': (46.902, 6.585, 'Fleurier'),
    '2056': (47.075, 6.862, 'Dombresson'),
    # Canton VD
    '1000': (46.520, 6.632, 'Lausanne'),
    '1003': (46.520, 6.632, 'Lausanne'),
    '1004': (46.520, 6.632, 'Lausanne'),
    '1005': (46.520, 6.632, 'Lausanne'),
    '1006': (46.520, 6.632, 'Lausanne'),
    '1007': (46.520, 6.632, 'Lausanne'),
    '1008': (46.535, 6.603, 'Prilly'),
    '1009': (46.510, 6.662, 'Pully'),
    '1010': (46.538, 6.588, 'Renens'),
    '1020': (46.538, 6.588, 'Renens'),
    '1024': (46.528, 6.561, 'Ecublens'),
    '1110': (46.510, 6.498, 'Morges'),
    '1260': (46.383, 6.239, 'Nyon'),
    '1400': (46.778, 6.641, 'Yverdon-les-Bains'),
    '1800': (46.462, 6.843, 'Vevey'),
    '1820': (46.434, 6.912, 'Montreux'),
    # Canton GE
    '1200': (46.204, 6.143, 'Genève'),
    '1201': (46.204, 6.143, 'Genève'),
    '1202': (46.204, 6.143, 'Genève'),
    '1203': (46.204, 6.143, 'Genève'),
    '1204': (46.204, 6.143, 'Genève'),
    '1205': (46.204, 6.143, 'Genève'),
    '1206': (46.204, 6.143, 'Genève'),
    '1207': (46.204, 6.143, 'Genève'),
    '1208': (46.204, 6.143, 'Genève'),
    '1209': (46.204, 6.143, 'Genève'),
    '1227': (46.180, 6.141, 'Carouge'),
    '1217': (46.231, 6.080, 'Meyrin'),
    # Canton FR
    '1700': (46.806, 7.162, 'Fribourg'),
    # Canton VS
    '1950': (46.227, 7.359, 'Sion'),
    '1920': (46.102, 7.074, 'Martigny'),
    '3960': (46.292, 7.535, 'Sierre'),
    # Canton BE
    '3000': (46.948, 7.447, 'Berne'),
    # Canton JU
    '2800': (47.366, 7.343, 'Delémont'),
    # Canton BS
    '4000': (47.559, 7.588, 'Bâle'),
    # Canton ZH
    '8000': (47.377, 8.541, 'Zürich'),
    # Canton LU
    '6000': (47.050, 8.308, 'Luzern'),
    # Canton TI
    '6900': (46.004, 8.951, 'Lugano'),
    # Canton SG
    '9000': (47.424, 9.376, 'St. Gallen'),
    # Canton BI
    '2500': (47.141, 7.247, 'Bienne'),
}


def _norm_city_name(name):
    """Normalise un nom de ville : sans accent, minuscules."""
    if not name:
        return ''
    s = unicodedata.normalize('NFD', str(name).lower().strip())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def _is_npa(value):
    """True if the value looks like a Swiss NPA (4-digit postal code)."""
    if not value:
        return False
    return bool(re.match(r'^\d{4}$', str(value).strip()))


def _lookup_city_coords(city):
    """Retourne (lat, lng) pour une ville ou un NPA connu, ou None."""
    if not city:
        return None
    c = str(city).strip()
    # Check NPA first
    if _is_npa(c) and c in NPA_COORDS:
        lat, lng, _name = NPA_COORDS[c]
        return (lat, lng)
    c = c.lower()
    if c in CITY_COORDS:
        return CITY_COORDS[c]
    cn = _norm_city_name(city)
    for k, v in CITY_COORDS.items():
        if _norm_city_name(k) == cn:
            return v
    return None


def _npa_to_city_name(npa):
    """Retourne le nom de ville canonique pour un NPA, ou None."""
    npa = str(npa).strip()
    if npa in NPA_COORDS:
        return NPA_COORDS[npa][2]
    return None


def resolve_zone_coords(zone):
    """
    Remplit latitude/longitude d'une zone si absentes.
    Utilise NPA_COORDS (si city est un NPA) puis CITY_COORDS (nom de ville).
    Ne modifie pas la zone si déjà remplie. Retourne la zone (mutée).

    Sans GPS, score_zone() ne peut pas calculer de distance Haversine et
    tombe sur le fallback canton match (score=10 ou 40), ce qui donne des
    scores erronés et laisse passer des biens hors zone.
    """
    if zone.get('latitude') and zone.get('longitude'):
        return zone
    city = zone.get('city', '') or ''
    coords = _lookup_city_coords(city)
    if coords:
        zone['latitude'] = coords[0]
        zone['longitude'] = coords[1]
    return zone


# Synonymes pour la détection d'équipements dans les descriptions
SYNONYMS = {
    'vue': ['vue', 'panoram', 'dégagé', 'dégagée', 'lac', 'montagne'],
    'calme': ['calme', 'tranquil', 'paisible', 'résidentiel'],
    'parking': ['parking', 'garage', 'place de parc', 'box'],
    'balcon': ['balcon', 'loggia'],
    'terrasse': ['terrasse', 'rooftop'],
    'jardin': ['jardin', 'verdure', 'espace vert'],
    'ascenseur': ['ascenseur', 'lift'],
    'cave': ['cave', 'cellier'],
    'animaux': ['animaux acceptés', 'animaux autorisés', 'pets allowed'],
    'transports': ['transport', 'gare', 'bus', 'métro', 'tram'],
    'commerces': ['commerces', 'shopping', 'magasin'],
    'ecoles': ['école', 'crèche', 'garderie'],
    'renove': ['rénov', 'neuf', 'refait', 'modern'],
    'minergie': ['minergie', 'basse énergie', 'énergétique'],
    'meuble': ['meublé', 'équipé'],
    'buanderie': ['buanderie', 'machine à laver', 'lave-linge'],
}


def match_feature(priority, features_list, description):
    """Vérifie si une priorité correspond aux features ou à la description."""
    p = priority.lower().strip()

    # Check direct match in features
    for f in features_list:
        if p in f.lower():
            return True

    # Check synonyms in features + description
    combined = ' '.join(features_list).lower() + ' ' + description.lower()
    synonyms = SYNONYMS.get(p, [p])
    for syn in synonyms:
        if syn in combined:
            return True

    return False


def score_zone(prop, zones):
    """
    Score la correspondance géographique (0-100).
    Retourne (score, distance_km).
    """
    if not zones:
        # C2.5 — no location signal at all → near-zero, not neutral.
        # Prevents random faraway listings from inheriting a 50 baseline.
        return 10, None

    min_distance = float('inf')
    target_radius = 3.0
    city_match = False

    # Fallback: coordonnées centrales de la ville si GPS manquant sur l'annonce
    prop_lat = prop.get('latitude')
    prop_lng = prop.get('longitude')
    if not (prop_lat and prop_lng):
        fallback = _lookup_city_coords(prop.get('city', ''))
        if fallback:
            prop_lat, prop_lng = fallback

    for zone in zones:
        zone_city = zone.get('city', '')

        # Check exact city match (accent-insensitive)
        if (_norm_city_name(prop.get('city', '')) ==
                _norm_city_name(zone_city)):
            city_match = True

        # NPA match: if zone city is a postal code, match against prop's postal_code
        # OR against the canonical city name for that NPA
        if _is_npa(zone_city):
            prop_postal = str(prop.get('postal_code') or '').strip()
            if prop_postal == zone_city.strip():
                city_match = True
            # Also match by canonical city name (e.g., NPA 2074 → Marin-Epagnier)
            canonical = _npa_to_city_name(zone_city)
            if canonical and _norm_city_name(prop.get('city', '')) == _norm_city_name(canonical):
                city_match = True

        # Fallback: coordonnées centrales de la ville de la zone si GPS manquant
        zone_lat = zone.get('latitude')
        zone_lng = zone.get('longitude')
        if not (zone_lat and zone_lng):
            fallback = _lookup_city_coords(zone.get('city', ''))
            if fallback:
                zone_lat, zone_lng = fallback

        # GPS distance
        if prop_lat and prop_lng and zone_lat and zone_lng:
            d = haversine(prop_lat, prop_lng, zone_lat, zone_lng)
            if d < min_distance:
                min_distance = d
                # radius_km comes from a NUMERIC column → psycopg2 returns Decimal,
                # which can't be multiplied with float (the * 1.5 / * 3 below).
                # Coerce to float at the source so all downstream maths works.
                r = zone.get('radius_km')
                target_radius = float(r) if r is not None else 3.0

    # If we have GPS data
    if min_distance != float('inf'):
        # Radius 0 = "commune exacte": only city_match properties score high
        if target_radius == 0:
            if city_match:
                score = 100
            elif min_distance <= 1.0:
                score = 60  # Very close but not same commune
            else:
                score = max(0, int(15 - min_distance))
        elif min_distance <= target_radius:
            # Progressive scoring within radius: closer = higher score
            # At 0 km → 100, at target_radius → 80
            score = int(100 - (min_distance / target_radius) * 20)
        elif min_distance <= target_radius * 1.5:
            # Just outside radius: 60-70
            over = (min_distance - target_radius) / (target_radius * 0.5)
            score = int(70 - over * 10)
        elif min_distance <= target_radius * 3:
            # Further out: 20-60
            over = (min_distance - target_radius * 1.5) / (target_radius * 1.5)
            score = int(60 - over * 40)
        else:
            score = max(0, int(15 - min_distance))
    elif city_match:
        score = 90  # Same city but no GPS data
        min_distance = 0
    else:
        # Check canton match
        prop_canton = prop.get('canton', '').upper()
        zone_cantons = [z.get('canton', '').upper() for z in zones]
        if prop_canton and prop_canton in zone_cantons:
            score = 40
        else:
            score = 10
        min_distance = None

    # Bonus for exact city match (stacks with GPS proximity).
    # Main purpose: salvage the no-GPS branch (score=90) up to 100.
    if city_match and score < 100:
        score = min(100, score + 10)

    # City-first ranking: when the user picked specific cities, the user's
    # cities should rank above nearby neighbours within the same radius.
    # Without this cap, a listing 1 km outside Cortaillod in a different city
    # would score 92 on zone (100 - 1/3*20), beating a Cortaillod listing
    # whose other criteria (price, surface, …) are weaker. Feedback: users
    # expect "if I picked Cortaillod, I want Cortaillod first."
    # Empirically, capping non-matching cities at 80 gives same-city listings
    # a ~12-point zone edge (≈3.6 total-score points at weight 30), enough
    # to flip typical adjacent-city cases without flattening the distance
    # curve for listings further out.
    # Cap non-matching cities so they DON'T pass the >= 80 zone filter.
    # Previously 80, which meant a city 0.5km away with radius=3km would
    # score exactly 80 and pass the filter — showing Peseux/Auvernier
    # when the user searched for Colombier (1km).
    CITY_MISMATCH_CEILING = 75
    if (not city_match
            and min_distance is not None
            and min_distance != float('inf')
            and score > CITY_MISMATCH_CEILING):
        score = CITY_MISMATCH_CEILING

    dist = round(min_distance, 1) if min_distance and min_distance != float('inf') else None
    return score, dist


def score_budget(prop, profile):
    """Score la correspondance budget (0-100)."""
    # Both come from NUMERIC columns → psycopg2 returns Decimal, which can't be
    # multiplied with a float literal (the * 0.7 below would raise TypeError).
    price = prop.get('price')
    budget_max = profile.get('budget_max')
    budget_min = profile.get('budget_min')

    if not price or not budget_max or budget_max <= 0:
        return 50  # Pas assez d'info → neutre

    price = float(price)
    budget_max = float(budget_max)
    budget_min = float(budget_min) if budget_min else None

    ratio = price / budget_max

    if ratio <= 0.8:
        score = 100    # Bien en dessous du max
    elif ratio <= 1.0:
        score = 90     # Dans le budget
    elif ratio <= 1.1:
        score = 60     # Légèrement au-dessus (+10%)
    elif ratio <= 1.2:
        score = 30     # Au-dessus (+20%)
    else:
        score = 0      # Hors budget

    # Pénalité si suspicieusement bas
    if budget_min and price < budget_min * 0.7:
        score = max(0, score - 30)

    return score


def score_type_rooms(prop, profile):
    """Score type de bien + pièces (0-100)."""
    score = 0

    # Type match
    prop_type = (prop.get('property_type') or '').lower()
    profile_types = [t.lower() for t in (profile.get('property_types') or [])]

    if prop_type and profile_types:
        if prop_type in profile_types:
            score += 50
        elif any(t in prop_type or prop_type in t for t in profile_types):
            score += 30  # Partial match (e.g., "attique" vs "appartement")
        else:
            score += 10
    else:
        score += 25  # Neutral

    # Rooms match — cast to float defensively. rooms_min / rooms_max / rooms
    # all come from NUMERIC columns (Decimal). Any `* float_literal` below
    # raises TypeError: unsupported operand type(s) for *: 'Decimal' and 'float'.
    rooms_raw = prop.get('rooms')
    rooms_min_raw = profile.get('rooms_min')
    rooms_max_raw = profile.get('rooms_max')
    rooms = float(rooms_raw) if rooms_raw is not None else None
    rooms_min = float(rooms_min_raw) if rooms_min_raw is not None else None
    rooms_max = float(rooms_max_raw) if rooms_max_raw is not None else None

    if rooms and rooms_min:
        if rooms >= rooms_min:
            score += 50
            if rooms_max and rooms > rooms_max:
                score -= 15  # Too many rooms
        else:
            diff = rooms_min - rooms
            # Bug #5 fix: additive penalty wasn't strong enough (3.5 pcs with
            # rooms_min=4 could still score 88A). Now we BOTH remove the
            # rooms bonus AND apply a multiplicative penalty on the type
            # score below. Additive here just keeps small tolerance for
            # diff < 0.25 (rounding noise between 3.5 and 3.75).
            score += max(0, int(10 - diff * 40))

            # Multiplicative penalty: 0.5 short → 0.5×, 1.0 short → 0.3×
            # Applied to the entire type_rooms score so a rooms-short property
            # can't ride on type match + other perfect factors.
            penalty_factor = max(0.2, 1.0 - diff * 1.0)
            score = int(score * penalty_factor)
    else:
        score += 25

    return min(100, score)


def score_surface(prop, profile):
    """Score surface (0-100)."""
    # Cast to float defensively — NUMERIC columns return Decimal which breaks
    # any future `* 0.x` literal (same bug class as budget/radius).
    surface = prop.get('surface')
    surface_min = profile.get('surface_min')

    if not surface or not surface_min:
        return 50

    surface = float(surface)
    surface_min = float(surface_min)
    ratio = surface / surface_min
    if ratio >= 1.0:
        return 100
    elif ratio >= 0.9:
        return 80
    elif ratio >= 0.8:
        return 50
    else:
        return max(0, int(ratio * 50))


def score_equipment(prop, profile):
    """Score équipements/priorités (0-100)."""
    priorities = profile.get('priorities') or []
    if not priorities:
        return 50

    features = prop.get('features') or []
    description = prop.get('description') or ''

    matched = sum(1 for p in priorities if match_feature(p, features, description))
    return int(matched / len(priorities) * 100) if priorities else 50


def score_freshness(prop):
    """Score fraîcheur de l'annonce (0-100)."""
    published = prop.get('published_at')
    if not published:
        scraped = prop.get('scraped_at')
        if scraped:
            published = scraped
        else:
            return 30

    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published.replace('Z', '+00:00'))
        except ValueError:
            return 30

    now = datetime.now(published.tzinfo) if published.tzinfo else datetime.now()
    days_old = (now - published).days

    # C2.6 — tightened thresholds so stale listings drop faster.
    # Aligned with the 21-day deactivation window in cron_job.py.
    if days_old <= 1:
        return 100
    elif days_old <= 3:
        return 90
    elif days_old <= 7:
        return 75
    elif days_old <= 14:
        return 50
    elif days_old <= 21:
        return 15
    else:
        return 5


def score_property(prop, profile, zones):
    """
    Score une annonce par rapport à un profil.

    Args:
        prop: dict avec les champs de la table properties
        profile: dict avec les champs de search_profiles
        zones: list de dicts avec les champs de search_zones

    Returns:
        dict {total_score, grade, score_zone, score_budget, ...}
    """
    s_zone, distance = score_zone(prop, zones)
    s_budget = score_budget(prop, profile)
    s_type = score_type_rooms(prop, profile)
    s_surface = score_surface(prop, profile)
    s_equip = score_equipment(prop, profile)
    s_fresh = score_freshness(prop)

    # Poids personnalisables
    w = {
        'zone': profile.get('w_zone', 30),
        'budget': profile.get('w_budget', 25),
        'type': profile.get('w_type', 20),
        'surface': profile.get('w_surface', 10),
        'equipment': profile.get('w_equipment', 10),
        'freshness': profile.get('w_freshness', 5),
    }
    total_weight = sum(max(0, v) for v in w.values()) or 100

    total = round(
        (s_zone * w['zone'] +
         s_budget * w['budget'] +
         s_type * w['type'] +
         s_surface * w['surface'] +
         s_equip * w['equipment'] +
         s_fresh * w['freshness'])
        / total_weight
    )

    total = max(0, min(100, total))
    grade = 'A' if total >= 85 else 'B' if total >= 70 else 'C' if total >= 55 else 'D'

    return {
        'total_score': total,
        'grade': grade,
        'score_zone': s_zone,
        'score_budget': s_budget,
        'score_type': s_type,
        'score_surface': s_surface,
        'score_equipment': s_equip,
        'score_freshness': s_fresh,
        'distance_km': distance,
    }


def score_all_for_profile(db, profile_id):
    """
    Score toutes les annonces actives pour un profil donné.
    Appeler après chaque scrape.

    Args:
        db: psycopg2 database connection
        profile_id: ID du search_profile
    """
    cur = db.cursor()
    try:
        # Load profile
        cur.execute(
            "SELECT * FROM search_profiles WHERE id = %s AND is_active = TRUE",
            (profile_id,)
        )
        profile = cur.fetchone()

        if not profile:
            return 0
        profile = dict(profile)

        # Load zones
        cur.execute(
            "SELECT * FROM search_zones WHERE profile_id = %s",
            (profile_id,)
        )
        zones = [dict(z) for z in cur.fetchall()]

        # Pre-filter properties by transaction type
        query = """
            SELECT * FROM properties
            WHERE is_active = TRUE
            AND transaction = %s
        """
        params = [profile['transaction']]

        # Budget filter with 30% margin (cast: budget_max is Decimal from NUMERIC,
        # Decimal * float literal raises TypeError)
        if profile.get('budget_max'):
            query += " AND (price IS NULL OR price <= %s)"
            params.append(int(float(profile['budget_max']) * 1.3))

        # Zone pre-filter: if we have zones, limit to matching cantons to reduce scope.
        # When zone city is an NPA, also include properties in nearby postal codes.
        zone_cantons = [z.get('canton', '').upper() for z in zones if z.get('canton')]
        # For NPA zones with no canton, infer canton from NPA_COORDS → CITY_COORDS
        for z in zones:
            zc = z.get('city', '')
            if _is_npa(zc) and not z.get('canton'):
                # NPA zones without canton: skip canton pre-filter entirely
                # (we rely on GPS distance scoring to filter)
                zone_cantons = []
                break
        if zone_cantons:
            placeholders = ','.join(['%s'] * len(zone_cantons))
            query += f" AND (canton IN ({placeholders}) OR canton IS NULL OR canton = '')"
            params.extend(zone_cantons)

        cur.execute(query, params)
        properties = cur.fetchall()

        # Score each property
        scored = 0
        for prop in properties:
            prop = dict(prop)
            result = score_property(prop, profile, zones)

            cur.execute("""
                INSERT INTO scored_properties
                    (property_id, profile_id, user_id, total_score, grade,
                     score_zone, score_budget, score_type, score_surface,
                     score_equipment, score_freshness, distance_km)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (property_id, profile_id)
                DO UPDATE SET
                    total_score = EXCLUDED.total_score,
                    grade = EXCLUDED.grade,
                    score_zone = EXCLUDED.score_zone,
                    score_budget = EXCLUDED.score_budget,
                    score_type = EXCLUDED.score_type,
                    score_surface = EXCLUDED.score_surface,
                    score_equipment = EXCLUDED.score_equipment,
                    score_freshness = EXCLUDED.score_freshness,
                    distance_km = EXCLUDED.distance_km,
                    scored_at = NOW()
            """, (
                prop['id'], profile_id, profile['user_id'],
                result['total_score'], result['grade'],
                result['score_zone'], result['score_budget'],
                result['score_type'], result['score_surface'],
                result['score_equipment'], result['score_freshness'],
                result['distance_km']
            ))
            scored += 1

        db.commit()
        return scored
    finally:
        cur.close()
