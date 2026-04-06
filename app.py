"""
Lou Garou — Backend V2
Flask API avec PostgreSQL, Claude AI chatbot, scoring engine

Environment variables needed on Render:
  DATABASE_URL=postgresql://...
  ANTHROPIC_API_KEY=sk-ant-...
  JWT_SECRET=your-secret-key
  FLASK_ENV=production
"""

import os
import json
import hashlib
from datetime import datetime, timedelta
from functools import wraps

import jwt
import bcrypt
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from anthropic import Anthropic

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__, static_folder='static')
CORS(app, resources={r"/api/*": {"origins": "*"}})

DATABASE_URL = os.environ.get('DATABASE_URL', '')
JWT_SECRET = os.environ.get('JWT_SECRET', 'lou-garou-secret-change-me')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def get_db():
    """Get a database connection."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def token_required(f):
    """JWT authentication decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({"error": "Token manquant"}), 401
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            request.user_id = data['user_id']
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expiré"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token invalide"}), 401
        return f(*args, **kwargs)
    return decorated


def make_token(user_id):
    """Generate a JWT token."""
    return jwt.encode(
        {'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=30)},
        JWT_SECRET, algorithm='HS256'
    )


# ============================================================
# DATABASE INIT
# ============================================================

def init_db():
    """Create all tables if they don't exist."""
    sql_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    if os.path.exists(sql_path):
        conn = get_db()
        cur = conn.cursor()
        with open(sql_path, 'r') as f:
            cur.execute(f.read())
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized")


# ============================================================
# AUTH ENDPOINTS
# ============================================================

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    name = data.get('name', '')
    criteria = data.get('criteria', {})

    if not email or '@' not in email:
        return jsonify({"error": "Email invalide"}), 400
    if len(password) < 6:
        return jsonify({"error": "Mot de passe trop court (6 car. min)"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_db()
    cur = conn.cursor()
    try:
        # Create user
        cur.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING id, email, name",
            (email, pw_hash, name)
        )
        user = dict(cur.fetchone())

        # Create default search profile from chatbot criteria
        if criteria:
            cur.execute("""
                INSERT INTO search_profiles (user_id, property_types, transaction, budget_max, rooms_min, priorities)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                user['id'],
                [criteria.get('property_type', 'appartement')],
                criteria.get('transaction_type', 'location'),
                _parse_budget(criteria.get('budget', '')),
                _parse_rooms(criteria.get('rooms', '')),
                criteria.get('priorities', [])
            ))
            profile = cur.fetchone()
            profile_id = profile['id']

            # Create search zone
            city = criteria.get('city', '')
            canton = criteria.get('canton', '')
            if city:
                cur.execute("""
                    INSERT INTO search_zones (profile_id, city, canton, radius_km)
                    VALUES (%s, %s, %s, %s)
                """, (profile_id, city, canton, 3.0))

        conn.commit()
        token = make_token(user['id'])
        return jsonify({"ok": True, "token": token, "user": user})

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Email déjà utilisé"}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE email = %s AND is_active = TRUE", (email,))
        user = cur.fetchone()
        if not user or not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
            return jsonify({"error": "Identifiants incorrects"}), 401

        cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user['id'],))
        conn.commit()

        token = make_token(user['id'])
        return jsonify({
            "ok": True,
            "token": token,
            "user": {"id": user['id'], "email": user['email'], "name": user['name']}
        })
    finally:
        cur.close()
        conn.close()


# ============================================================
# PROFILE ENDPOINTS
# ============================================================

@app.route('/api/profile', methods=['GET'])
@token_required
def get_profile():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT sp.*, json_agg(json_build_object(
                'id', sz.id, 'city', sz.city, 'canton', sz.canton,
                'radius_km', sz.radius_km, 'latitude', sz.latitude, 'longitude', sz.longitude
            )) as zones
            FROM search_profiles sp
            LEFT JOIN search_zones sz ON sz.profile_id = sp.id
            WHERE sp.user_id = %s AND sp.is_active = TRUE
            GROUP BY sp.id
            ORDER BY sp.created_at DESC LIMIT 1
        """, (request.user_id,))
        profile = cur.fetchone()
        if not profile:
            return jsonify({"profile": None})
        return jsonify({"profile": dict(profile)})
    finally:
        cur.close()
        conn.close()


@app.route('/api/profile', methods=['PUT'])
@token_required
def update_profile():
    data = request.json or {}
    conn = get_db()
    cur = conn.cursor()
    try:
        # Get or create profile
        cur.execute(
            "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
            (request.user_id,)
        )
        row = cur.fetchone()

        if row:
            profile_id = row['id']
            cur.execute("""
                UPDATE search_profiles SET
                    property_types = %s, transaction = %s,
                    budget_min = %s, budget_max = %s,
                    rooms_min = %s, rooms_max = %s,
                    surface_min = %s, surface_max = %s,
                    priorities = %s,
                    w_zone = %s, w_budget = %s, w_type = %s,
                    w_surface = %s, w_equipment = %s, w_freshness = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (
                data.get('property_types', []),
                data.get('transaction'),
                data.get('budget_min'),
                data.get('budget_max'),
                data.get('rooms_min'),
                data.get('rooms_max'),
                data.get('surface_min'),
                data.get('surface_max'),
                data.get('priorities', []),
                data.get('weights', {}).get('zone', 30),
                data.get('weights', {}).get('budget', 25),
                data.get('weights', {}).get('type', 20),
                data.get('weights', {}).get('surface', 10),
                data.get('weights', {}).get('equipment', 10),
                data.get('weights', {}).get('freshness', 5),
                profile_id
            ))
        else:
            cur.execute("""
                INSERT INTO search_profiles (user_id, property_types, transaction, budget_min, budget_max,
                    rooms_min, rooms_max, surface_min, surface_max, priorities)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                request.user_id,
                data.get('property_types', []),
                data.get('transaction'),
                data.get('budget_min'),
                data.get('budget_max'),
                data.get('rooms_min'),
                data.get('rooms_max'),
                data.get('surface_min'),
                data.get('surface_max'),
                data.get('priorities', [])
            ))
            profile_id = cur.fetchone()['id']

        # Update zones
        zones = data.get('zones', [])
        if zones:
            cur.execute("DELETE FROM search_zones WHERE profile_id = %s", (profile_id,))
            for z in zones:
                cur.execute("""
                    INSERT INTO search_zones (profile_id, city, canton, postal_code, latitude, longitude, radius_km)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    profile_id, z.get('city', ''), z.get('canton', ''),
                    z.get('postal_code'), z.get('latitude'), z.get('longitude'),
                    z.get('radius_km', 3.0)
                ))

        conn.commit()
        return jsonify({"ok": True, "profile_id": profile_id})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
# PROPERTIES ENDPOINTS
# ============================================================

@app.route('/api/properties/<int:user_id>', methods=['GET'])
def get_properties(user_id):
    sort = request.args.get('sort', 'score')
    min_score = int(request.args.get('min_score', 0))
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    offset = (page - 1) * per_page

    order_map = {
        'score': 'sp.total_score DESC',
        'price_asc': 'p.price ASC',
        'price_desc': 'p.price DESC',
        'newest': 'p.published_at DESC NULLS LAST',
        'surface': 'p.surface DESC NULLS LAST'
    }
    order = order_map.get(sort, 'sp.total_score DESC')

    conn = get_db()
    cur = conn.cursor()
    try:
        # Get properties with scores
        cur.execute(f"""
            SELECT p.*, sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   EXISTS(SELECT 1 FROM favorites f WHERE f.user_id = %s AND f.property_id = p.id) as is_favorite
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND sp.total_score >= %s AND p.is_active = TRUE
            ORDER BY {order}
            LIMIT %s OFFSET %s
        """, (user_id, user_id, min_score, per_page, offset))
        properties = [dict(r) for r in cur.fetchall()]

        # Get total count
        cur.execute("""
            SELECT COUNT(*) as total FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND sp.total_score >= %s AND p.is_active = TRUE
        """, (user_id, min_score))
        total = cur.fetchone()['total']

        # Format for frontend
        results = []
        for p in properties:
            results.append({
                'id': p['id'],
                'title': p['title'] or 'Bien immobilier',
                'address': p['address'] or '',
                'price': p['price'] or 0,
                'unit': f"{p['currency'] or 'CHF'}/{p['price_unit'] or 'mois'}",
                'rooms': float(p['rooms']) if p['rooms'] else 0,
                'surface': p['surface'] or 0,
                'floor': p['floor'],
                'features': p['features'] or [],
                'source': p['source'] or '',
                'source_url': p['source_url'] or '',
                'contact_name': p['contact_name'] or '',
                'contact_phone': p['contact_phone'] or '',
                'contact_email': p['contact_email'] or '',
                'images': p['images'] or [],
                'score': p['total_score'],
                'grade': p['grade'],
                'distance_km': float(p['distance_km']) if p['distance_km'] else None,
                'score_detail': {
                    'zone': p['score_zone'],
                    'budget': p['score_budget'],
                    'type': p['score_type'],
                    'surface': p['score_surface'],
                    'equipment': p['score_equipment'],
                    'freshness': p['score_freshness']
                },
                'published_at': p['published_at'].isoformat() if p['published_at'] else None,
                'is_favorite': p['is_favorite']
            })

        return jsonify({"properties": results, "total": total, "page": page, "per_page": per_page})
    finally:
        cur.close()
        conn.close()


@app.route('/api/stats/<int:user_id>', methods=['GET'])
def get_stats(user_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE sp.scored_at > NOW() - INTERVAL '24 hours') as new_count,
                (SELECT COUNT(*) FROM favorites WHERE user_id = %s) as favorites
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND p.is_active = TRUE
        """, (user_id, user_id))
        stats = dict(cur.fetchone())
        return jsonify(stats)
    finally:
        cur.close()
        conn.close()


@app.route('/api/favorite/<int:property_id>', methods=['POST'])
@token_required
def toggle_favorite(property_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM favorites WHERE user_id = %s AND property_id = %s",
            (request.user_id, property_id)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute("DELETE FROM favorites WHERE id = %s", (existing['id'],))
            action = 'removed'
        else:
            cur.execute(
                "INSERT INTO favorites (user_id, property_id) VALUES (%s, %s)",
                (request.user_id, property_id)
            )
            action = 'added'
        conn.commit()
        return jsonify({"ok": True, "action": action})
    finally:
        cur.close()
        conn.close()


# ============================================================
# CHATBOT IA ENDPOINT
# ============================================================

LOU_SYSTEM_PROMPT = """Tu es Lou, un chasseur immobilier digital suisse. Tu es un loup sympathique, attentif et efficace.

TON ROLE: Aider les gens à définir leur recherche immobilière en Suisse romande via une conversation naturelle et fluide.

REGLES CRITIQUES:
- Parle en français, tutoie, sois chaleureux mais pro
- UNE question à la fois
- JAMAIS reposer une question dont tu connais déjà la réponse
- Si l'utilisateur donne plusieurs infos d'un coup, enregistre-les TOUTES
- Prends en compte CHAQUE message — ne l'ignore jamais
- Sois bref: 1-3 phrases max

CRITERES A COLLECTER (saute ceux déjà connus):
1. Zone: ville(s), canton, rayon km
2. Type: appartement, maison, villa, studio, loft, attique, duplex
3. Transaction: location ou achat
4. Budget: min et/ou max CHF
5. Pièces: min et/ou max
6. Surface: m² min
7. Priorités: balcon, parking, vue, calme, transports, animaux, cave, jardin, ascenseur
8. Étage (optionnel)
9. Date emménagement (optionnel)

COMPORTEMENT:
- Si un PROFIL ACTUEL est fourni ci-dessous, NE REDEMANDE AUCUN critère déjà connu. Salue et propose de modifier ou confirmer.
- Si aucun profil n'existe, commence par te présenter et demander la région
- Quand tu as zone + type + transaction + budget, propose de créer l'espace
- Ne force pas les critères optionnels
- Si le profil est déjà complet, demande si l'utilisateur veut modifier quelque chose

REPONSE: JSON uniquement:
{"message":"texte","suggestions":["btn1","btn2"],"criteria":{"zones":[{"city":"X","canton":"XX","radius_km":3}],"property_types":["appartement"],"transaction":"location","budget_min":null,"budget_max":2500,"currency":"CHF","rooms_min":3,"rooms_max":null,"surface_min":null,"floor_preference":null,"priorities":["vue"],"move_date":null},"profile_ready":false,"confirmed":false}

criteria contient UNIQUEMENT les champs qui changent dans ce message.
profile_ready = true quand zone + type + transaction + budget sont remplis.
confirmed = true quand l'utilisateur confirme explicitement.
REPONDS UNIQUEMENT DU JSON VALIDE, RIEN D'AUTRE.

Cantons romands: VD, GE, NE, FR, VS, JU, BE
Villes principales: Genève, Lausanne, Montreux, Vevey, Nyon, Morges, Yverdon, Neuchâtel, Fribourg, Sion, Bienne"""

# Conversation store (in-memory, use Redis for production)
conversations = {}


@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json or {}
    user_id_raw = data.get('user_id', 'anon')
    user_id = str(user_id_raw)
    # Keep numeric version for DB queries
    try:
        user_id_int = int(user_id_raw)
    except (ValueError, TypeError):
        user_id_int = None
    message = data.get('message', '').strip()

    if not message:
        return jsonify({"error": "Message vide"}), 400

    if not ANTHROPIC_KEY:
        return jsonify({
            "message": "Chatbot IA non configuré. Ajoutez ANTHROPIC_API_KEY.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        })

    # Get or create history
    if user_id not in conversations:
        conversations[user_id] = []
    history = conversations[user_id]

    # Load existing profile from DB to give context to Claude
    existing_profile = {}
    db_uid = user_id_int if user_id_int is not None else user_id
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT transaction, property_types, budget_min, budget_max,
                   rooms_min, rooms_max, surface_min, priorities
            FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (db_uid,))
        row = cur.fetchone()
        if row:
            if row['transaction']: existing_profile['transaction'] = row['transaction']
            if row['property_types']: existing_profile['property_types'] = row['property_types']
            if row['budget_min']: existing_profile['budget_min'] = row['budget_min']
            if row['budget_max']: existing_profile['budget_max'] = row['budget_max']
            if row['rooms_min']: existing_profile['rooms_min'] = row['rooms_min']
            if row['rooms_max']: existing_profile['rooms_max'] = row['rooms_max']
            if row['surface_min']: existing_profile['surface_min'] = row['surface_min']
            if row['priorities']: existing_profile['priorities'] = row['priorities']
            # Also load zones
            cur.execute("""
                SELECT sz.city, sz.canton, sz.radius_km
                FROM search_zones sz
                JOIN search_profiles sp ON sp.id = sz.profile_id
                WHERE sp.user_id = %s AND sp.is_active = TRUE
            """, (db_uid,))
            zones = cur.fetchall()
            if zones:
                existing_profile['zones'] = [dict(z) for z in zones]
    except Exception as e:
        print(f"Profile load error: {e}")

    print(f"[CHAT] user_id={user_id} existing_profile={existing_profile}")

    # Build system prompt with profile context
    sys_prompt = LOU_SYSTEM_PROMPT
    if existing_profile:
        known_parts = []
        if existing_profile.get('transaction'):
            known_parts.append(f"Transaction: {existing_profile['transaction']}")
        if existing_profile.get('zones'):
            zones_str = ', '.join([z.get('city', '?') for z in existing_profile['zones']])
            known_parts.append(f"Zones: {zones_str}")
        if existing_profile.get('budget_max'):
            known_parts.append(f"Budget max: {existing_profile['budget_max']} CHF")
        if existing_profile.get('property_types'):
            types = existing_profile['property_types']
            if isinstance(types, list):
                known_parts.append(f"Types: {', '.join(types)}")
            else:
                known_parts.append(f"Types: {types}")
        if existing_profile.get('rooms_min'):
            known_parts.append(f"Pièces min: {existing_profile['rooms_min']}")
        if existing_profile.get('surface_min'):
            known_parts.append(f"Surface min: {existing_profile['surface_min']} m²")
        if existing_profile.get('priorities'):
            prios = existing_profile['priorities']
            if isinstance(prios, list):
                known_parts.append(f"Priorités: {', '.join(prios)}")

        known_summary = '\n'.join(known_parts) if known_parts else 'Aucun critère'
        print(f"[CHAT] user_id={user_id} profile loaded: {known_summary}")
        sys_prompt += f"\n\n=== PROFIL ACTUEL DE L'UTILISATEUR (DEJA ENREGISTRE — NE JAMAIS REDEMANDER CES INFOS) ===\n{known_summary}\n\nJSON: {json.dumps(existing_profile, ensure_ascii=False, default=str)}\n\nIMPORTANT: L'utilisateur a DEJA défini ces critères. Ne les redemande PAS. Passe directement au PROCHAIN critère manquant, ou propose de modifier le profil existant."

    # Build messages
    messages = list(history[-10:])
    messages.append({"role": "user", "content": message})

    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=sys_prompt,
            messages=messages
        )
        raw = response.content[0].text.strip()

        # Parse JSON (handle ```json wrapping)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)
        result.setdefault("message", "...")
        result.setdefault("suggestions", [])
        result.setdefault("criteria", {})
        result.setdefault("profile_ready", False)
        result.setdefault("confirmed", False)

        # Store history — use the message text (not raw JSON) for better context
        history.append({"role": "user", "content": message})
        assistant_text = result.get("message", raw)
        if result.get("criteria"):
            assistant_text += f" [Critères mis à jour: {json.dumps(result['criteria'], ensure_ascii=False, default=str)}]"
        history.append({"role": "assistant", "content": assistant_text})
        if len(history) > 20:
            conversations[user_id] = history[-20:]

        # Save criteria to DB if any were extracted
        if result.get("criteria") and result["criteria"]:
            try:
                _save_chat_criteria(user_id, result["criteria"])
            except Exception as save_err:
                print(f"Criteria save error: {save_err}")

        return jsonify(result)

    except json.JSONDecodeError:
        return jsonify({
            "message": raw if raw else "Desole, reformule ?",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        })
    except Exception as e:
        return jsonify({
            "message": "Probleme technique, reessaie.",
            "suggestions": ["Reessayer"], "criteria": {},
            "profile_ready": False, "confirmed": False, "error": str(e)
        })


def _save_chat_criteria(user_id, criteria):
    """Save criteria extracted from chat to the user's search profile."""
    conn = get_db()
    cur = conn.cursor()
    # Get existing profile
    cur.execute(
        "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()
    if not row:
        # Create a new profile
        cur.execute(
            "INSERT INTO search_profiles (user_id, is_active) VALUES (%s, TRUE) RETURNING id",
            (user_id,)
        )
        row = cur.fetchone()
    profile_id = row['id']

    # Update fields that were provided
    updates = []
    values = []
    field_map = {
        'transaction': 'transaction',
        'property_types': 'property_types',
        'budget_min': 'budget_min',
        'budget_max': 'budget_max',
        'rooms_min': 'rooms_min',
        'rooms_max': 'rooms_max',
        'surface_min': 'surface_min',
        'priorities': 'priorities',
    }
    for chat_key, db_col in field_map.items():
        if chat_key in criteria and criteria[chat_key] is not None:
            updates.append(f"{db_col} = %s")
            values.append(criteria[chat_key])

    if updates:
        values.append(profile_id)
        cur.execute(f"UPDATE search_profiles SET {', '.join(updates)}, updated_at = NOW() WHERE id = %s", values)

    # Handle zones separately
    if 'zones' in criteria and criteria['zones']:
        for zone in criteria['zones']:
            if isinstance(zone, dict) and zone.get('city'):
                cur.execute("""
                    INSERT INTO search_zones (profile_id, city, canton, radius_km)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (profile_id, city) DO UPDATE SET
                        canton = EXCLUDED.canton, radius_km = EXCLUDED.radius_km
                """, (
                    profile_id,
                    zone.get('city'),
                    zone.get('canton'),
                    zone.get('radius_km', 5)
                ))

    conn.commit()


@app.route('/api/chat/reset', methods=['POST'])
def chat_reset():
    data = request.json or {}
    uid = str(data.get('user_id', 'anon'))
    conversations.pop(uid, None)
    return jsonify({"ok": True})


# ============================================================
# SCRAPING
# ============================================================

@app.route('/api/scrape', methods=['POST'])
@token_required
def api_scrape():
    """Trigger scraping based on user's search profile zones."""
    from scrapers import scrape_all, save_to_db

    data = request.json or {}
    city = data.get('city')
    transaction = data.get('transaction', 'location')

    # If no city specified, use the user's profile zones
    if not city:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT sz.city FROM search_zones sz
                JOIN search_profiles sp ON sp.id = sz.profile_id
                WHERE sp.user_id = %s AND sp.is_active = TRUE
            """, (request.user_id,))
            rows = cur.fetchall()
            cities = [r['city'] for r in rows if r['city']]
            if not cities:
                return jsonify({"error": "Aucune zone configuree. Ajoutez des zones dans votre profil."}), 400

            # Also get transaction from profile
            cur.execute("""
                SELECT transaction FROM search_profiles
                WHERE user_id = %s AND is_active = TRUE
                ORDER BY created_at DESC LIMIT 1
            """, (request.user_id,))
            prof = cur.fetchone()
            if prof and prof['transaction']:
                transaction = prof['transaction']
        finally:
            cur.close()
            conn.close()
    else:
        cities = [city]

    # Scrape for each city
    total_scraped = 0
    total_saved = 0
    details = []
    conn = get_db()
    try:
        for c in cities:
            listings = scrape_all(city=c, transaction=transaction)
            total_scraped += len(listings)
            saved = save_to_db(conn, listings)
            total_saved += saved
            details.append({"city": c, "scraped": len(listings), "saved": saved})
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "total_scraped": total_scraped,
        "total_saved": total_saved,
        "details": details
    })


@app.route('/api/import', methods=['POST'])
@token_required
def api_import():
    """Import scraped listings from external source (e.g. local scraper)."""
    from scrapers import save_to_db

    data = request.json or {}
    listings = data.get('listings', [])

    if not listings:
        return jsonify({"error": "No listings provided"}), 400

    conn = get_db()
    try:
        saved = save_to_db(conn, listings)
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "received": len(listings),
        "saved": saved
    })


@app.route('/api/score', methods=['POST'])
@token_required
def api_score():
    """Score all properties for the current user's profile."""
    import traceback
    try:
        from scoring_engine import score_property
        conn = get_db()
        cur = conn.cursor()

        # Get user's active profile
        cur.execute("""
            SELECT * FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (request.user_id,))
        profile = cur.fetchone()
        if not profile:
            return jsonify({"error": "Aucun profil trouvé"}), 400

        profile = dict(profile)

        # Get zones
        cur.execute("SELECT * FROM search_zones WHERE profile_id = %s", (profile['id'],))
        zones = [dict(z) for z in cur.fetchall()]

        # Get all active properties
        cur.execute("SELECT * FROM properties WHERE is_active = TRUE")
        properties = cur.fetchall()

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
                prop['id'], profile['id'], request.user_id,
                result['total_score'], result['grade'],
                result['score_zone'], result['score_budget'],
                result['score_type'], result['score_surface'],
                result['score_equipment'], result['score_freshness'],
                result['distance_km']
            ))
            scored += 1

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True, "scored": scored, "profile_id": profile['id']})

    except Exception as e:
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@app.route('/api/scrape/debug', methods=['GET'])
def api_scrape_debug():
    """Test ScrapingBee with a single Homegate request."""
    import traceback
    try:
        from scrapers import _sb_get, SCRAPINGBEE_KEY
        import os

        city = request.args.get('city', 'Lausanne')
        sb_key = os.environ.get('SCRAPINGBEE_API_KEY', 'NOT SET')

        results = {
            "scrapingbee_key_set": bool(sb_key and sb_key != 'NOT SET'),
            "scrapingbee_key_start": sb_key[:8] + '...' if sb_key and sb_key != 'NOT SET' else 'NOT SET',
            "key_from_scrapers": SCRAPINGBEE_KEY[:8] + '...' if SCRAPINGBEE_KEY else 'EMPTY',
        }

        # Test 1: Simple ScrapingBee connectivity test with httpbin
        try:
            status_test, html_test = _sb_get("https://httpbin.org/get", render_js=False)
            results["httpbin_test"] = {
                "http_status": status_test,
                "html_size": len(html_test) if html_test else 0,
                "html_start": html_test[:200] if html_test else '',
            }
        except Exception as e:
            results["httpbin_test"] = {"error": str(e)}

        # Test 2: Run actual Homegate scraper
        try:
            from scrapers import scrape_homegate
            listings = scrape_homegate(city=city, transaction="location", max_pages=1)
            results["homegate_scraper"] = {
                "total_listings": len(listings),
                "sample": [
                    {
                        "id": l["external_id"],
                        "title": l["title"][:80],
                        "price": l["price"],
                        "rooms": l["rooms"],
                        "surface": l["surface"],
                        "address": l["address"][:60],
                        "url": l["source_url"],
                    }
                    for l in listings[:5]
                ]
            }
        except Exception as e:
            results["homegate_scraper"] = {"error": str(e)}
            import traceback
            results["homegate_traceback"] = traceback.format_exc()

        return jsonify(results)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route('/api/scrape/test', methods=['GET'])
def api_scrape_test():
    """Debug endpoint: test raw HTTP responses from each portal."""
    import requests as req

    city = request.args.get('city', 'Lausanne')
    tx = request.args.get('transaction', 'location')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'fr-CH,fr;q=0.9',
    }
    results = {}

    # 1. Flatfox — test multiple endpoints
    for endpoint_name, url in [
        ('Flatfox_v1_flat', 'https://flatfox.ch/api/v1/flat/'),
        ('Flatfox_v1_public', 'https://flatfox.ch/api/v1/public/listings/'),
        ('Flatfox_search', 'https://flatfox.ch/api/v1/public/search/listings/'),
    ]:
        try:
            r = req.get(url, headers=headers,
                        params={'city': city, 'offer_type': 'RENT', 'ordering': '-created', 'limit': 3},
                        timeout=15)
            body = r.text[:500]
            is_json = r.headers.get('content-type', '').startswith('application/json')
            results[endpoint_name] = {"http": r.status_code, "is_json": is_json, "body_preview": body}
        except Exception as e:
            results[endpoint_name] = {"error": str(e)}

    # 2. Homegate API
    try:
        r = req.get(f'https://www.homegate.ch/api/search/rent',
                     headers={**headers, 'Referer': 'https://www.homegate.ch/'},
                     params={'loc': city, 'ag': 3, 'o': 'dateCreated-desc'},
                     timeout=15)
        body = r.text[:500]
        results['Homegate_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        results['Homegate_API'] = {"error": str(e)}

    # 3. Homegate __NEXT_DATA__
    try:
        slug = city.lower().replace(' ', '-')
        r = req.get(f'https://www.homegate.ch/rent/real-estate/city-{slug}/matching-list',
                     headers=headers, timeout=15)
        has_next = '__NEXT_DATA__' in r.text
        next_snippet = ''
        if has_next:
            import re
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.{0,300})', r.text)
            next_snippet = m.group(1) if m else ''
        results['Homegate_Page'] = {"http": r.status_code, "has_NEXT_DATA": has_next, "snippet": next_snippet[:300], "page_size": len(r.text)}
    except Exception as e:
        results['Homegate_Page'] = {"error": str(e)}

    # 4. ImmoScout24
    try:
        slug = city.lower().replace(' ', '-')
        r = req.get(f'https://www.immoscout24.ch/fr/immobilier/louer/lieu-{slug}',
                     headers=headers, timeout=15)
        has_next = '__NEXT_DATA__' in r.text
        results['ImmoScout24'] = {"http": r.status_code, "has_NEXT_DATA": has_next, "page_size": len(r.text)}
    except Exception as e:
        results['ImmoScout24'] = {"error": str(e)}

    # 5. Comparis API
    try:
        payload = {'DealType': 10, 'Keyword': city, 'LocationSearchString': city, 'Sort': 4, 'Page': 1, 'PageSize': 3, 'RootPropertyTypes': [1]}
        r = req.post('https://api.comparis.ch/realestate/v1/search/list',
                      headers={**headers, 'Content-Type': 'application/json'},
                      json=payload, timeout=15)
        body = r.text[:500]
        results['Comparis_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        results['Comparis_API'] = {"error": str(e)}

    return jsonify({"city": city, "transaction": tx, "results": results})


# ============================================================
# HELPERS
# ============================================================

def _parse_budget(s):
    """Extract numeric budget from string like '2000-2500 CHF' or '2500'."""
    if not s:
        return None
    import re
    nums = re.findall(r'\d+', str(s).replace("'", "").replace(",", ""))
    if nums:
        return int(nums[-1])  # Take the last (highest) number
    return None


def _parse_rooms(s):
    """Extract room count from string like '3+' or '3.5'."""
    if not s:
        return None
    import re
    nums = re.findall(r'[\d.]+', str(s))
    if nums:
        return float(nums[0])
    return None


# ============================================================
# STARTUP
# ============================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'dashboard.html')


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# Initialize database on module load (works with gunicorn)
if DATABASE_URL:
    try:
        init_db()
    except Exception as e:
        print(f"DB init error (will retry on first request): {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') != 'production')
