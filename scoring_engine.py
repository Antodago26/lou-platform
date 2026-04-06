"""
Lou Garou — Scoring Engine
Score chaque annonce par rapport aux critères de recherche d'un utilisateur.

Usage:
    from scoring_engine import score_property, score_all_for_profile, GRADE_MAP

Intégrer dans le cron job après chaque scrape:
    score_all_for_profile(db, profile_id)
"""

import math
from datetime import datetime


def haversine(lat1, lon1, lat2, lon2):
    """Distance en km entre deux points GPS."""
    R = 6371  # rayon Terre en km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Synonymes pour la détection d'équipements dans les descriptions
SYNONYMS = {
    'vue': ['vue', 'panoram', 'dégagé', 'dégagée', 'lac', 'montagne'],
    'calme': ['calme', 'tranquil', 'paisible', 'résidentiel'],
    'parking': ['parking', 'garage', 'place de parc', 'box'],
    'balcon': ['balcon', 'terrasse', 'loggia'],
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
        return 50, None

    min_distance = float('inf')
    target_radius = 3.0
    city_match = False

    for zone in zones:
        # Check exact city match
        if (prop.get('city', '').lower().strip() ==
                zone.get('city', '').lower().strip()):
            city_match = True

        # GPS distance
        if (prop.get('latitude') and prop.get('longitude') and
                zone.get('latitude') and zone.get('longitude')):
            d = haversine(
                prop['latitude'], prop['longitude'],
                zone['latitude'], zone['longitude']
            )
            if d < min_distance:
                min_distance = d
                target_radius = zone.get('radius_km', 3.0) or 3.0

    # If we have GPS data
    if min_distance != float('inf'):
        if min_distance <= target_radius:
            score = 100
        elif min_distance <= target_radius * 1.5:
            score = 70
        elif min_distance <= target_radius * 2:
            score = 40
        else:
            score = max(0, int(20 - min_distance))
    elif city_match:
        score = 85  # Same city but no GPS data
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

    # Bonus for exact city match
    if city_match and score < 100:
        score = min(100, score + 15)

    dist = round(min_distance, 1) if min_distance and min_distance != float('inf') else None
    return score, dist


def score_budget(prop, profile):
    """Score la correspondance budget (0-100)."""
    price = prop.get('price')
    budget_max = profile.get('budget_max')
    budget_min = profile.get('budget_min')

    if not price or not budget_max:
        return 50  # Pas assez d'info → neutre

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

    # Rooms match
    rooms = prop.get('rooms')
    rooms_min = profile.get('rooms_min')
    rooms_max = profile.get('rooms_max')

    if rooms and rooms_min:
        if rooms >= rooms_min:
            score += 50
            if rooms_max and rooms > rooms_max:
                score -= 15  # Too many rooms
        else:
            diff = rooms_min - rooms
            score += max(0, int(35 - diff * 15))
    else:
        score += 25

    return min(100, score)


def score_surface(prop, profile):
    """Score surface (0-100)."""
    surface = prop.get('surface')
    surface_min = profile.get('surface_min')

    if not surface or not surface_min:
        return 50

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
    return int(matched / len(priorities) * 100)


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

    if days_old <= 1:
        return 100
    elif days_old <= 3:
        return 90
    elif days_old <= 7:
        return 75
    elif days_old <= 14:
        return 50
    elif days_old <= 30:
        return 25
    else:
        return 10


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
    total_weight = sum(w.values()) or 100

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
        db: database connection/session
        profile_id: ID du search_profile
    """
    # Load profile + zones
    profile = db.execute(
        "SELECT * FROM search_profiles WHERE id = %s AND is_active = TRUE",
        (profile_id,)
    ).fetchone()

    if not profile:
        return 0

    zones = db.execute(
        "SELECT * FROM search_zones WHERE profile_id = %s",
        (profile_id,)
    ).fetchall()

    # Clean old scores for this profile
    db.execute(
        "DELETE FROM scored_properties WHERE profile_id = %s",
        (profile_id,)
    )

    # Pre-filter properties — prefer matching transaction, but include all if none match
    query = """
        SELECT * FROM properties
        WHERE is_active = TRUE
        AND transaction = %s
    """
    params = [profile['transaction']]

    # Budget filter with 30% margin
    if profile['budget_max']:
        query += " AND (price IS NULL OR price <= %s)"
        params.append(int(profile['budget_max'] * 1.3))

    properties = db.execute(query, params).fetchall()

    # Fallback: if no properties match the transaction type, score ALL properties
    if not properties:
        query = "SELECT * FROM properties WHERE is_active = TRUE"
        params = []
        if profile['budget_max']:
            query += " AND (price IS NULL OR price <= %s)"
            params.append(int(profile['budget_max'] * 1.3))
        properties = db.execute(query, params).fetchall()

    properties = db.execute(query, params).fetchall()

    # Score each property
    scored = 0
    for prop in properties:
        result = score_property(dict(prop), dict(profile), [dict(z) for z in zones])

        db.execute("""
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
