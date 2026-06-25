"""Bon Home — Publication d'un bien par un particulier (marketplace).

Extension applicative : un utilisateur connecté publie SON bien dans la même
table `properties` que les annonces scrapées, avec `source='prive'` +
`owner_user_id`. On réutilise tout l'existant (auth JWT, scoring, alertes,
dédup, dashboard).

Les deux endpoints différenciants — que seul bonhome peut offrir grâce à sa
donnée — sont :
  * /api/publish/price-suggestion : prix conseillé depuis les comparables scrapés.
  * /api/publish/demand-signal    : nb de chercheurs déjà en attente sur la zone.

Dépend du schéma v6.4.6 (owner_user_id, listing_status). NE PAS déployer sans
avoir lancé run_schema_v646 (fait au boot par app.py).
"""
import os
import uuid
import logging
import statistics

import requests
from flask import Blueprint, jsonify, request, render_template

from db import get_db, return_db
from auth import token_required

log = logging.getLogger('lou-app')
publish_bp = Blueprint('publish', __name__)

GEOADMIN_SEARCH = "https://api3.geo.admin.ch/rest/services/api/SearchServer"

# Client Anthropic local (même pattern que routes_chat). Optionnel : si la clé
# n'est pas posée, /generate-description renvoie un fallback non-IA.
_ANTHROPIC_KEY = os.environ.get('ANTHROPIC_KEY') or os.environ.get('ANTHROPIC_API_KEY')
try:
    from anthropic import Anthropic
    _anthropic = Anthropic(api_key=_ANTHROPIC_KEY) if _ANTHROPIC_KEY else None
except Exception:  # pragma: no cover - dépendance déjà présente en prod
    _anthropic = None


# ---------------------------------------------------------------- page UI
@publish_bp.route('/publier')
def publier_page():
    """Le wizard de publication guidé par Lou (auth gérée côté front)."""
    return render_template('publier.html')


# ------------------------------------------------ étape 2 : adresse + géo
@publish_bp.route('/api/publish/address', methods=['GET'])
@token_required
def address_autocomplete():
    """Autocomplétion d'adresse via geo.admin.ch (origins=address).

    Renvoie des suggestions normalisées {label, postal_code, city, lat, lon}.
    L'enrichissement bâtiment (RegBL/GWR : année, surface, pièces) se fera dans
    une 2e passe via l'EGID — marqué TODO, non bloquant pour le MVP."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 3:
        return jsonify({"ok": True, "suggestions": []})
    try:
        r = requests.get(GEOADMIN_SEARCH, params={
            "searchText": q, "type": "locations", "origins": "address",
            "limit": 8, "sr": 4326,
        }, timeout=8)
        results = (r.json() or {}).get('results', []) if r.status_code == 200 else []
    except Exception as e:
        log.warning("geo.admin address lookup failed: %s", e)
        results = []

    suggestions = []
    for item in results:
        a = item.get('attrs', {})
        label = (a.get('label') or '').replace('<b>', '').replace('</b>', '').strip()
        suggestions.append({
            "label": label,
            "postal_code": str(a.get('zip') or a.get('postalCode') or '') or None,
            "city": a.get('detail', '').split(' ')[0].title() if a.get('detail') else None,
            "lat": a.get('lat'),
            "lon": a.get('lon'),
            "egid": a.get('featureId'),  # pour enrichissement RegBL futur
        })
    return jsonify({"ok": True, "suggestions": suggestions})


# ----------------------------------- étape 4 : prix conseillé (comparables)
@publish_bp.route('/api/publish/price-suggestion', methods=['GET'])
@token_required
def price_suggestion():
    """Prix conseillé à partir des biens comparables déjà en base (scrapés).

    Comparables = même ville + même transaction + (même type si fourni) +
    pièces à ±1. On rend la médiane comme suggestion et le quartile [p25,p75]
    comme fourchette de marché, plus un échantillon pour la transparence."""
    city = (request.args.get('city') or '').strip()
    transaction = (request.args.get('transaction') or 'location').strip()
    ptype = (request.args.get('property_type') or '').strip() or None
    try:
        rooms = float(request.args.get('rooms')) if request.args.get('rooms') else None
    except ValueError:
        rooms = None
    if not city:
        return jsonify({"ok": False, "error": "city requis"}), 400

    r_lo, r_hi = (rooms - 1, rooms + 1) if rooms else (None, None)
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT price, rooms, surface, city, source
            FROM properties
            WHERE is_active = TRUE
              AND price IS NOT NULL AND price > 0
              AND transaction = %s
              AND LOWER(city) = LOWER(%s)
              AND (%s::text IS NULL OR property_type = %s)
              AND (%s::float IS NULL OR rooms IS NULL OR rooms BETWEEN %s AND %s)
            ORDER BY price
            LIMIT 200
        """, (transaction, city, ptype, ptype, rooms, r_lo, r_hi))
        rows = cur.fetchall()
    finally:
        cur.close()
        return_db(conn)

    prices = sorted(int(r['price']) for r in rows if r['price'])
    if len(prices) < 3:
        return jsonify({
            "ok": True, "enough_data": False, "sample_size": len(prices),
            "suggestion": None, "range": None, "comps": [],
        })

    def pct(p):
        k = max(0, min(len(prices) - 1, int(round((len(prices) - 1) * p))))
        return prices[k]

    suggestion = int(round(statistics.median(prices) / 10.0)) * 10
    comps = [{
        "price": int(r['price']),
        "rooms": float(r['rooms']) if r['rooms'] else None,
        "surface": int(r['surface']) if r['surface'] else None,
        "city": r['city'],
    } for r in rows[:6]]
    return jsonify({
        "ok": True, "enough_data": True, "sample_size": len(prices),
        "suggestion": suggestion,
        "range": {"low": pct(0.25), "high": pct(0.75)},
        "comps": comps,
    })


# --------------------------------- étape 7 : signal de demande (chercheurs)
@publish_bp.route('/api/publish/demand-signal', methods=['GET'])
@token_required
def demand_signal():
    """Nombre de chercheurs actifs ayant une zone sur cette ville.

    N'utilise que des colonnes garanties (search_zones.city, search_profiles
    .is_active). Affinage futur par transaction/budget marqué TODO."""
    city = (request.args.get('city') or '').strip()
    if not city:
        return jsonify({"ok": False, "error": "city requis"}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COUNT(DISTINCT sp.id) AS n
            FROM search_profiles sp
            JOIN search_zones sz ON sz.profile_id = sp.id
            WHERE sp.is_active = TRUE
              AND LOWER(sz.city) = LOWER(%s)
        """, (city,))
        n = (cur.fetchone() or {}).get('n', 0)
    finally:
        cur.close()
        return_db(conn)
    return jsonify({"ok": True, "searchers": int(n or 0)})


# -------------------------------- étape 5 : description rédigée par Lou (IA)
@publish_bp.route('/api/publish/generate-description', methods=['POST'])
@token_required
def generate_description():
    """Rédige une annonce FR à partir des caractéristiques structurées."""
    d = request.json or {}
    facts = {k: d.get(k) for k in (
        'property_type', 'transaction', 'rooms', 'surface', 'city',
        'floor', 'features', 'highlight',
    )}
    if not _anthropic:
        # Fallback non-IA : annonce minimale mais correcte.
        bits = [f"{facts.get('rooms') or ''} pièces" if facts.get('rooms') else '',
                f"{facts.get('surface')} m²" if facts.get('surface') else '',
                f"à {facts['city']}" if facts.get('city') else '']
        txt = " ".join(b for b in bits if b).strip().capitalize() or "Bien immobilier à découvrir."
        return jsonify({"ok": True, "description": txt, "ai": False})

    prompt = (
        "Rédige une annonce immobilière en français (Suisse), 2 à 4 phrases, "
        "ton chaleureux mais factuel, sans superlatifs creux ni emoji. "
        "Ne fabrique aucune information non fournie.\n"
        f"Caractéristiques : {facts}"
    )
    try:
        model = os.environ.get('CLAUDE_CHAT_MODEL', 'claude-sonnet-4-6')
        resp = _anthropic.messages.create(
            model=model, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, 'type', '') == 'text').strip()
        return jsonify({"ok": True, "description": text, "ai": True})
    except Exception as e:
        log.exception("generate-description failed: %s", e)
        return jsonify({"ok": False, "error": "génération indisponible"}), 502


# ------------------------------------------ étape 8 : créer / publier le bien
@publish_bp.route('/api/publish/listing', methods=['POST'])
@token_required
def create_listing():
    """Insère une annonce de privé dans `properties` (source='prive')."""
    d = request.json or {}
    transaction = 'achat' if d.get('transaction') == 'achat' else 'location'
    title = (d.get('title') or '').strip()[:200]
    city = (d.get('city') or '').strip()
    if not title or not city:
        return jsonify({"ok": False, "error": "title et city requis"}), 400

    try:
        price = int(d['price']) if d.get('price') not in (None, '') else None
    except (ValueError, TypeError):
        price = None
    external_id = f"prive-{uuid.uuid4().hex[:16]}"
    features = d.get('features') or []
    images = d.get('images') or []
    status = 'draft' if d.get('draft') else 'active'

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO properties (
                external_id, source, source_url, title, description,
                property_type, transaction, price, currency, price_unit,
                rooms, surface, floor, address, city, canton, postal_code,
                latitude, longitude, features, images,
                contact_name, contact_phone, contact_email,
                owner_user_id, listing_status,
                published_at, scraped_at, first_seen_at
            ) VALUES (
                %s, 'prive', '', %s, %s, %s, %s, %s, 'CHF', %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, NOW(), NOW(), NOW()
            )
            RETURNING id
        """, (
            external_id, title, (d.get('description') or '').strip()[:1500],
            d.get('property_type') or 'appartement', transaction, price,
            'mois' if transaction == 'location' else 'total',
            d.get('rooms'), d.get('surface'), d.get('floor'),
            (d.get('address') or '').strip()[:200], city,
            d.get('canton'), d.get('postal_code'),
            d.get('latitude'), d.get('longitude'), features, images,
            (d.get('contact_name') or '').strip()[:120] or None,
            (d.get('contact_phone') or '').strip()[:40] or None,
            (d.get('contact_email') or '').strip()[:120] or None,
            request.user_id, status,
        ))
        new_id = cur.fetchone()['id']
        # URL canonique interne du bien
        cur.execute("UPDATE properties SET source_url = %s WHERE id = %s",
                    (f"/bien/{new_id}", new_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.exception("create_listing failed: %s", e)
        return jsonify({"ok": False, "error": "insertion échouée"}), 500
    finally:
        cur.close()
        return_db(conn)

    return jsonify({"ok": True, "id": new_id, "status": status})


# ----------------------------------------------------- mes annonces (vendeur)
@publish_bp.route('/api/publish/my-listings', methods=['GET'])
@token_required
def my_listings():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, title, city, price, transaction, property_type,
                   listing_status, images, published_at
            FROM properties
            WHERE owner_user_id = %s
            ORDER BY published_at DESC
        """, (request.user_id,))
        rows = cur.fetchall()
    finally:
        cur.close()
        return_db(conn)
    return jsonify({"ok": True, "listings": [dict(r) for r in rows]})
