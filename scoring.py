"""
Lou Score™ — Algorithme de notation des biens immobiliers
Score sur 100, 4 catégories, grade A-D
"""
import re


def compute_lou_score(property_data, profile):
    """
    Compute Lou Score for a property against a user's search profile.

    Returns: {
        total: int (0-100),
        grade: str (A/B/C/D),
        categories: [
            {name, score, max, details}
        ]
    }
    """
    correspondance = _score_correspondance(property_data, profile)
    preferences = _score_preferences(property_data, profile)
    qualite = _score_qualite(property_data)
    lifestyle = _score_lifestyle(property_data, profile)

    total = correspondance["score"] + preferences["score"] + qualite["score"] + lifestyle["score"]
    total = min(100, max(0, total))

    if total >= 85:
        grade = "A"
    elif total >= 70:
        grade = "B"
    elif total >= 55:
        grade = "C"
    else:
        grade = "D"

    return {
        "total": total,
        "grade": grade,
        "categories": [
            {"name": "Correspondance", "score": correspondance["score"], "max": 50, "details": correspondance["details"]},
            {"name": "Préférences", "score": preferences["score"], "max": 25, "details": preferences["details"]},
            {"name": "Qualité", "score": qualite["score"], "max": 10, "details": qualite["details"]},
            {"name": "Lifestyle", "score": lifestyle["score"], "max": 15, "details": lifestyle["details"]},
        ]
    }


def _score_correspondance(prop, profile):
    """Max 50 pts — How well does the property match the search criteria?"""
    score = 0
    details = []

    # Canton match (10 pts)
    if prop.get("canton") and profile.get("canton"):
        if prop["canton"].lower() == profile["canton"].lower():
            score += 10
            details.append("Canton exact +10")
        else:
            details.append("Canton différent +0")

    # City match (10 pts)
    if prop.get("city") and profile.get("city"):
        if prop["city"].lower() == profile["city"].lower():
            score += 10
            details.append("Ville exacte +10")
        elif profile["city"].lower() in prop.get("address", "").lower():
            score += 5
            details.append("Proche de la ville +5")

    # Property type (10 pts)
    if prop.get("property_type") and profile.get("property_type"):
        type_map = {
            "appartement": ["appartement", "apartment", "flat", "wohnung"],
            "maison": ["maison", "house", "haus", "villa", "chalet"],
            "villa": ["villa", "maison", "house"],
            "studio": ["studio", "1-zimmer"],
        }
        p_type = profile["property_type"].lower()
        prop_title = (prop.get("title", "") + " " + prop.get("description", "")).lower()
        matched = False
        for key, variants in type_map.items():
            if p_type in variants or key == p_type:
                if any(v in prop_title for v in variants):
                    score += 10
                    matched = True
                    details.append("Type exact +10")
                    break
        if not matched:
            details.append("Type non confirmé +0")

    # Budget match (10 pts)
    prop_price = prop.get("price", 0)
    budget_range = _parse_budget(profile.get("budget", ""), profile.get("transaction_type", ""))
    if prop_price and budget_range:
        low, high = budget_range
        if low <= prop_price <= high:
            score += 10
            details.append("Dans le budget +10")
        elif prop_price <= high * 1.1:
            score += 5
            details.append("Légèrement au-dessus du budget +5")
        else:
            details.append("Hors budget +0")

    # Rooms match (10 pts)
    prop_rooms = prop.get("rooms", 0)
    profile_rooms = _parse_rooms(profile.get("rooms", ""))
    if prop_rooms and profile_rooms:
        if prop_rooms == profile_rooms:
            score += 10
            details.append("Pièces exactes +10")
        elif abs(prop_rooms - profile_rooms) <= 1:
            score += 6
            details.append("Pièces proches +6")
        elif abs(prop_rooms - profile_rooms) <= 2:
            score += 3
            details.append("Pièces ±2 +3")

    return {"score": min(50, score), "details": details}


def _score_preferences(prop, profile):
    """Max 25 pts — How well does the property match user priorities?"""
    priorities = profile.get("priorities", [])
    if isinstance(priorities, str):
        import json
        try:
            priorities = json.loads(priorities)
        except:
            priorities = []

    if not priorities:
        return {"score": 15, "details": ["Pas de priorités définies, score moyen"]}

    score = 0
    details = []
    pts_per = min(25 // len(priorities), 8)

    text = (prop.get("title", "") + " " + prop.get("description", "") + " " + prop.get("address", "")).lower()

    priority_keywords = {
        "luminosité": ["lumineux", "luminosité", "ensoleillé", "sud", "baie vitrée", "hell", "sonnig", "lichtdurchflutet"],
        "vue": ["vue", "panorama", "dégagée", "lac", "montagne", "aussicht", "seeblick", "bergblick"],
        "calme": ["calme", "tranquille", "résidentiel", "ruhig", "quiet"],
        "transport": ["transport", "métro", "bus", "gare", "tram", "öv", "bahn", "station"],
        "terrasse": ["terrasse", "balcon", "jardin", "loggia", "balkon", "terrasse", "garten"],
        "parking": ["parking", "garage", "place de parc", "parkplatz", "tiefgarage"],
    }

    for p in priorities:
        p_lower = p.lower()
        keywords = priority_keywords.get(p_lower, [p_lower])
        if any(kw in text for kw in keywords):
            score += pts_per
            details.append(f"{p} trouvé +{pts_per}")
        else:
            details.append(f"{p} non mentionné +0")

    return {"score": min(25, score), "details": details}


def _score_qualite(prop):
    """Max 10 pts — Quality of the listing itself"""
    score = 0
    details = []

    # Has images (3 pts)
    images = prop.get("images", [])
    if isinstance(images, str):
        import json
        try:
            images = json.loads(images)
        except:
            images = []
    if len(images) >= 5:
        score += 3
        details.append("5+ photos +3")
    elif len(images) >= 1:
        score += 1
        details.append("Quelques photos +1")

    # Description length (3 pts)
    desc = prop.get("description", "")
    if len(desc) > 300:
        score += 3
        details.append("Description détaillée +3")
    elif len(desc) > 100:
        score += 1
        details.append("Description courte +1")

    # Has surface info (2 pts)
    if prop.get("surface_m2"):
        score += 2
        details.append("Surface indiquée +2")

    # Has exact address (2 pts)
    addr = prop.get("address", "")
    if addr and len(addr) > 10 and any(c.isdigit() for c in addr):
        score += 2
        details.append("Adresse précise +2")

    return {"score": min(10, score), "details": details}


def _score_lifestyle(prop, profile):
    """Max 15 pts — Lifestyle compatibility based on priorities"""
    priorities = profile.get("priorities", [])
    if isinstance(priorities, str):
        import json
        try:
            priorities = json.loads(priorities)
        except:
            priorities = []

    score = 0
    details = []

    weights = {
        "transport": 4,
        "luminosité": 3,
        "calme": 3,
        "vue": 3,
        "terrasse": 1,
        "parking": 1,
    }

    text = (prop.get("description", "") + " " + prop.get("address", "")).lower()

    for p in priorities:
        p_lower = p.lower()
        w = weights.get(p_lower, 1)
        # Check if property description strongly suggests this lifestyle factor
        strong_keywords = {
            "transport": ["5 min", "proximité", "à pied", "direct", "proche gare"],
            "luminosité": ["très lumineux", "plein sud", "double exposition", "baie"],
            "calme": ["très calme", "impasse", "zone résidentielle", "cul-de-sac"],
            "vue": ["vue imprenable", "vue dégagée", "panoramique", "vue lac"],
            "terrasse": ["grande terrasse", "jardin privatif", "balcon couvert"],
            "parking": ["2 places", "garage double", "parking couvert", "box"],
        }
        keywords = strong_keywords.get(p_lower, [])
        if any(kw in text for kw in keywords):
            score += w
            details.append(f"{p} (fort) +{w}")

    return {"score": min(15, score), "details": details}


def _parse_budget(budget_str, transaction_type=""):
    """Parse budget string to (min, max) range"""
    if not budget_str:
        return None
    b = budget_str.lower().replace(" ", "").replace("'", "").replace("chf", "")

    # Purchase budgets
    if "500k" in b and "1m" in b:
        return (500000, 1000000)
    if "<500" in b or "<500" in b:
        return (0, 500000)
    if "1m" in b and "2m" in b:
        return (1000000, 2000000)
    if "2m+" in b or "2m" in b:
        return (2000000, 50000000)

    # Rental budgets
    if "<1500" in b:
        return (0, 1500)
    if "1500" in b and "2500" in b:
        return (1500, 2500)
    if "2500" in b and "4000" in b:
        return (2500, 4000)
    if "4000+" in b or "4000" in b:
        return (4000, 50000)

    # Try to extract numbers
    nums = re.findall(r'\d+', b)
    if len(nums) >= 2:
        return (int(nums[0]), int(nums[1]))
    if len(nums) == 1:
        n = int(nums[0])
        return (0, n)

    return None


def _parse_rooms(rooms_str):
    """Parse rooms string to number"""
    if not rooms_str:
        return None
    nums = re.findall(r'[\d.]+', rooms_str)
    if nums:
        return float(nums[0])
    if "5+" in rooms_str:
        return 5.0
    return None
