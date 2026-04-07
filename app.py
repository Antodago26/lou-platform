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
- Le profil est COMPLET UNIQUEMENT quand tu as les 4 critères OBLIGATOIRES: zone (ville) + type de bien + transaction (achat/location) + budget. Si un seul manque, pose la question suivante — NE PROPOSE JAMAIS de créer l'espace sans ces 4 critères.
- Ne force pas les critères optionnels (surface, pièces, priorités, étage, date)
- Si le profil est déjà complet (4 critères obligatoires remplis), dis-le et propose des modifications ou de consulter les annonces
- Quand l'utilisateur dit "Créer mon espace" ou "Créer l'espace" et que le profil est complet, confirme simplement que c'est fait. NE REDEMANDE PAS la zone.
- Quand l'utilisateur dit "Voir les annonces", confirme simplement. NE REDEMANDE RIEN.
- Si l'utilisateur te donne une zone (ex: "Neuchâtel +3km"), enregistre-la IMMEDIATEMENT dans criteria.zones.
- IMPORTANT: criteria.zones REMPLACE toutes les zones existantes. Si l'utilisateur veut CHANGER de ville (ex: "je veux Neuchâtel au lieu de Lausanne"), mets UNIQUEMENT la nouvelle ville dans criteria.zones. Si l'utilisateur veut AJOUTER une ville, mets TOUTES les villes (anciennes + nouvelle) dans criteria.zones.

FORMAT DE REPONSE: Tu dois répondre UNIQUEMENT avec un objet JSON valide, sans AUCUN texte avant ou après. Pas de markdown, pas de commentaires, JUSTE le JSON.

Exemple de réponse correcte:
{"message":"Salut ! Je vois que tu cherches à Lausanne. Tu veux modifier quelque chose ?","suggestions":["Changer le budget","Changer la zone","Tout est bon"],"criteria":{},"profile_ready":true,"confirmed":false}

Exemple quand l'utilisateur donne une zone:
{"message":"J'ajoute Neuchâtel dans un rayon de 3km à ta recherche.","suggestions":["Ajouter une autre zone","Modifier le budget","C'est parfait"],"criteria":{"zones":[{"city":"Neuchâtel","canton":"NE","radius_km":3}]},"profile_ready":true,"confirmed":false}

Règles JSON:
- "message": texte court (1-3 phrases), PAS de markdown (**gras**), PAS de bullet points, PAS de listes
- "suggestions": 2-4 boutons courts (pas de suggestions d'action comme "Voir les annonces" si le profil vient d'être modifié)
- "criteria": UNIQUEMENT les champs qui CHANGENT dans ce message (objet vide {} si rien ne change)
- "criteria.zones": TOUJOURS un tableau d'objets avec city, canton, radius_km. Exemple: [{"city":"Lausanne","canton":"VD","radius_km":5}]
- "profile_ready": true UNIQUEMENT quand les 4 critères obligatoires sont TOUS remplis (zone + type + transaction + budget). Si un seul manque, profile_ready DOIT être false.
- "confirmed": true quand l'utilisateur confirme explicitement

IMPORTANT: Le champ "message" doit être du texte simple et naturel. Ne fais JAMAIS de liste de critères dans le message. Si l'utilisateur veut modifier ses critères, demande simplement ce qu'il veut changer en une phrase.
IMPORTANT: Quand l'utilisateur clique sur un bouton de confirmation ("Créer mon espace", "C'est parfait", etc.), NE REDEMANDE RIEN. Confirme et passe à l'étape suivante.

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
            zones_list = []
            for z in existing_profile['zones']:
                city = z.get('city', '?')
                radius = z.get('radius_km', 5)
                canton = z.get('canton', '')
                zones_list.append(f"{city} ({canton}, rayon {radius}km)")
            known_parts.append(f"Zones: {', '.join(zones_list)}")
        if existing_profile.get('budget_min'):
            known_parts.append(f"Budget min: {existing_profile['budget_min']} CHF")
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
        if existing_profile.get('rooms_max'):
            known_parts.append(f"Pièces max: {existing_profile['rooms_max']}")
        if existing_profile.get('surface_min'):
            known_parts.append(f"Surface min: {existing_profile['surface_min']} m²")
        if existing_profile.get('priorities'):
            prios = existing_profile['priorities']
            if isinstance(prios, list):
                known_parts.append(f"Priorités: {', '.join(prios)}")

        known_summary = '\n'.join(known_parts) if known_parts else 'Aucun critère'
        # Check which criteria are still missing
        missing = []
        if not existing_profile.get('zones'):
            missing.append('zone (ville + rayon)')
        if not existing_profile.get('transaction'):
            missing.append('transaction (location ou achat)')
        if not existing_profile.get('property_types'):
            missing.append('type de bien')
        if not existing_profile.get('budget_max') and not existing_profile.get('budget_min'):
            missing.append('budget')

        missing_str = ', '.join(missing) if missing else 'AUCUN - profil complet'

        print(f"[CHAT] user_id={user_id} profile loaded: {known_summary} | missing: {missing_str}")
        # DB profile info is merged into all_criteria below — single unified injection

    # Build accumulated criteria from ALL sources: DB profile + conversation JSON + message analysis
    conv_criteria = {}
    # Source 1: extract from assistant JSON criteria tags
    for h in history:
        if h.get('role') == 'assistant' and '[Critères mis à jour:' in h.get('content', ''):
            try:
                crit_start = h['content'].index('[Critères mis à jour:') + len('[Critères mis à jour: ')
                crit_end = h['content'].index(']', crit_start)
                crit_json = json.loads(h['content'][crit_start:crit_end])
                conv_criteria.update(crit_json)
            except Exception:
                pass

    # Source 2: analyze user messages directly for criteria mentions
    for h in history:
        if h.get('role') == 'user':
            msg_low = h.get('content', '').lower().strip()
            # Detect transaction
            if not conv_criteria.get('transaction_type') and not existing_profile.get('transaction'):
                if any(w in msg_low for w in ['acheter', 'achat', 'achète', 'acquerir']):
                    conv_criteria['transaction_type'] = 'achat'
                elif any(w in msg_low for w in ['louer', 'location', 'loue']):
                    conv_criteria['transaction_type'] = 'location'
            # Detect property type
            if not conv_criteria.get('property_type') and not existing_profile.get('property_types'):
                if 'appartement' in msg_low:
                    conv_criteria['property_type'] = 'appartement'
                elif 'maison' in msg_low or 'villa' in msg_low:
                    conv_criteria['property_type'] = 'maison'
                elif 'studio' in msg_low:
                    conv_criteria['property_type'] = 'studio'
            # Detect city
            if not conv_criteria.get('zones') and not existing_profile.get('zones'):
                city_map = {
                    'neuchâtel': ('Neuchâtel', 'NE'), 'neuchatel': ('Neuchâtel', 'NE'), 'neuch': ('Neuchâtel', 'NE'),
                    'lausanne': ('Lausanne', 'VD'), 'genève': ('Genève', 'GE'), 'geneve': ('Genève', 'GE'),
                    'montreux': ('Montreux', 'VD'), 'fribourg': ('Fribourg', 'FR'), 'sion': ('Sion', 'VS'),
                    'nyon': ('Nyon', 'VD'), 'morges': ('Morges', 'VD'), 'yverdon': ('Yverdon', 'VD'),
                    'vevey': ('Vevey', 'VD'), 'bienne': ('Bienne', 'BE'), 'biel': ('Bienne', 'BE'),
                }
                for key, (city_name, canton) in city_map.items():
                    if key in msg_low:
                        conv_criteria['zones'] = [{'city': city_name, 'canton': canton, 'radius_km': 5}]
                        break
            # Detect budget
            if not conv_criteria.get('budget_max') and not conv_criteria.get('budget_min'):
                if not existing_profile.get('budget_max') and not existing_profile.get('budget_min'):
                    import re as _re_budget
                    # Match patterns like "800'000", "800000", "500k", "800k"
                    budget_match = _re_budget.search(r"(\d[\d'\.]*)\s*(?:000)?\s*(?:chf|CHF|francs?)?", msg_low)
                    if '800' in msg_low and any(w in msg_low for w in ['plus de', '800k', "800'000", '800000']):
                        conv_criteria['budget_min'] = 800000
                    elif '500' in msg_low and '800' in msg_low:
                        conv_criteria['budget_min'] = 500000
                        conv_criteria['budget_max'] = 800000
                    elif '500' in msg_low and any(w in msg_low for w in ['moins de', '500k', "500'000"]):
                        conv_criteria['budget_max'] = 500000

    # Source 3: also analyze the CURRENT message (not yet in history)
    msg_low = message.lower().strip()
    if not conv_criteria.get('transaction_type') and not existing_profile.get('transaction'):
        if any(w in msg_low for w in ['acheter', 'achat', 'achète', 'acquerir']):
            conv_criteria['transaction_type'] = 'achat'
        elif any(w in msg_low for w in ['louer', 'location', 'loue']):
            conv_criteria['transaction_type'] = 'location'
    if not conv_criteria.get('property_type') and not existing_profile.get('property_types'):
        if 'appartement' in msg_low:
            conv_criteria['property_type'] = 'appartement'
        elif 'maison' in msg_low or 'villa' in msg_low:
            conv_criteria['property_type'] = 'maison'
    if not conv_criteria.get('zones') and not existing_profile.get('zones'):
        city_map_cur = {
            'neuchâtel': ('Neuchâtel', 'NE'), 'neuchatel': ('Neuchâtel', 'NE'), 'neuch': ('Neuchâtel', 'NE'),
            'lausanne': ('Lausanne', 'VD'), 'genève': ('Genève', 'GE'), 'geneve': ('Genève', 'GE'),
            'montreux': ('Montreux', 'VD'), 'fribourg': ('Fribourg', 'FR'), 'sion': ('Sion', 'VS'),
            'nyon': ('Nyon', 'VD'), 'morges': ('Morges', 'VD'), 'yverdon': ('Yverdon', 'VD'),
            'vevey': ('Vevey', 'VD'), 'bienne': ('Bienne', 'BE'), 'biel': ('Bienne', 'BE'),
        }
        for key, (city_name, canton) in city_map_cur.items():
            if key in msg_low:
                conv_criteria['zones'] = [{'city': city_name, 'canton': canton, 'radius_km': 5}]
                break
    if not conv_criteria.get('budget_max') and not conv_criteria.get('budget_min'):
        if not existing_profile.get('budget_max') and not existing_profile.get('budget_min'):
            if '800' in msg_low and any(w in msg_low for w in ['plus de', '800k', "800'000", '800000']):
                conv_criteria['budget_min'] = 800000
            elif '500' in msg_low and '800' in msg_low:
                conv_criteria['budget_min'] = 500000
                conv_criteria['budget_max'] = 800000
            elif '500' in msg_low and any(w in msg_low for w in ['moins de', '500k', "500'000"]):
                conv_criteria['budget_max'] = 500000

    # Merge conv_criteria with existing_profile for the complete picture
    all_criteria = {}
    if existing_profile:
        all_criteria.update(existing_profile)
    # Map conv_criteria keys to profile keys
    if conv_criteria.get('transaction_type') and not all_criteria.get('transaction'):
        all_criteria['transaction'] = conv_criteria['transaction_type']
    if conv_criteria.get('property_type') and not all_criteria.get('property_types'):
        all_criteria['property_types'] = [conv_criteria['property_type']]
    if conv_criteria.get('zones') and not all_criteria.get('zones'):
        all_criteria['zones'] = conv_criteria['zones']
    if conv_criteria.get('budget_min') and not all_criteria.get('budget_min'):
        all_criteria['budget_min'] = conv_criteria['budget_min']
    if conv_criteria.get('budget_max') and not all_criteria.get('budget_max'):
        all_criteria['budget_max'] = conv_criteria['budget_max']

    # Log combined criteria for debugging
    print(f"[CHAT] user_id={user_id} conv_criteria={json.dumps(conv_criteria, ensure_ascii=False, default=str)} all_criteria={json.dumps({k:str(v)[:50] for k,v in all_criteria.items()}, ensure_ascii=False, default=str)}")

    # ALWAYS inject the combined criteria summary from ALL sources (DB + conversation)
    # This ensures Claude always knows what's already been collected, even if DB save failed
    all_parts = []
    if all_criteria.get('transaction'):
        all_parts.append(f"Transaction: {all_criteria['transaction']}")
    if all_criteria.get('zones'):
        zones_str = ', '.join(
            f"{z.get('city', '?')} ({z.get('canton', '?')}, rayon {z.get('radius_km', 5)}km)" if isinstance(z, dict) else str(z)
            for z in all_criteria['zones']
        )
        all_parts.append(f"Zones: {zones_str}")
    if all_criteria.get('property_types'):
        types = all_criteria['property_types']
        all_parts.append(f"Type: {', '.join(types) if isinstance(types, list) else types}")
    if all_criteria.get('budget_min') or all_criteria.get('budget_max'):
        bmin = all_criteria.get('budget_min', '')
        bmax = all_criteria.get('budget_max', '')
        all_parts.append(f"Budget: {bmin or '?'} - {bmax or '?'} CHF")
    if all_criteria.get('rooms_min') or all_criteria.get('rooms_max'):
        all_parts.append(f"Pièces: {all_criteria.get('rooms_min', '?')} - {all_criteria.get('rooms_max', '?')}")
    if all_criteria.get('surface_min'):
        all_parts.append(f"Surface min: {all_criteria['surface_min']} m²")
    if all_criteria.get('priorities'):
        prios = all_criteria['priorities']
        all_parts.append(f"Priorités: {', '.join(prios) if isinstance(prios, list) else prios}")

    if all_parts:
        all_missing = []
        if not all_criteria.get('zones'):
            all_missing.append('zone (ville)')
        if not all_criteria.get('transaction'):
            all_missing.append('transaction (location ou achat)')
        if not all_criteria.get('property_types'):
            all_missing.append('type de bien')
        if not all_criteria.get('budget_max') and not all_criteria.get('budget_min'):
            all_missing.append('budget')
        all_missing_str = ', '.join(all_missing) if all_missing else 'AUCUN - profil complet'
        all_summary = '\n'.join(all_parts)
        sys_prompt += f"""

=== CRITERES COMPLETS (BASE DE DONNEES + CONVERSATION) ===
{all_summary}

Critères MANQUANTS: {all_missing_str}

REGLES ABSOLUES — NE LES ENFREINS JAMAIS:
1. Les critères ci-dessus sont CONNUS. NE LES REDEMANDE JAMAIS, même pour confirmer.
2. Si "Zones:" apparait ci-dessus → NE DEMANDE PAS "dans quelle ville/région"
3. Si "Budget:" apparait ci-dessus → NE DEMANDE PAS le budget
4. Si "Transaction:" apparait ci-dessus → NE DEMANDE PAS achat/location
5. Si "Type:" apparait ci-dessus → NE DEMANDE PAS le type de bien
6. Demande UNIQUEMENT les critères listés comme MANQUANTS
7. Si AUCUN critère n'est manquant → propose de créer l'espace ou voir les annonces"""

    # Build messages
    messages = list(history[-10:])

    # Inject a reminder of known criteria right before user's message
    # This is more reliable than system prompt alone — Claude can't miss it
    if all_parts:
        all_missing_reminder = []
        if not all_criteria.get('zones'):
            all_missing_reminder.append('ville/zone')
        if not all_criteria.get('transaction'):
            all_missing_reminder.append('transaction')
        if not all_criteria.get('property_types'):
            all_missing_reminder.append('type de bien')
        if not all_criteria.get('budget_max') and not all_criteria.get('budget_min'):
            all_missing_reminder.append('budget')
        reminder_parts = ', '.join(all_parts)
        if all_missing_reminder:
            reminder = f"[RAPPEL SYSTEME: Critères DEJA connus: {reminder_parts}. Il manque UNIQUEMENT: {', '.join(all_missing_reminder)}. Ne redemande RIEN d'autre.]"
        else:
            reminder = f"[RAPPEL SYSTEME: TOUS les critères sont remplis: {reminder_parts}. Ne redemande RIEN. Propose de créer l'espace ou voir les annonces.]"
        # Add as a system-injected user context before the actual message
        messages.append({"role": "user", "content": f"{message}\n\n{reminder}"})
    else:
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
        print(f"[CHAT] Raw response: {raw[:300]}")

        # Robust JSON extraction — handle text before/after JSON, ```json blocks, etc.
        json_str = raw

        # Remove ```json ... ``` wrapping
        if '```' in json_str:
            parts = json_str.split('```')
            for part in parts:
                p = part.strip()
                if p.startswith('json'):
                    p = p[4:].strip()
                if p.startswith('{') and p.endswith('}'):
                    json_str = p
                    break

        # If still not valid JSON, try to extract { ... } from the text
        if not json_str.startswith('{'):
            brace_start = json_str.find('{')
            brace_end = json_str.rfind('}')
            if brace_start != -1 and brace_end > brace_start:
                json_str = json_str[brace_start:brace_end + 1]

        result = json.loads(json_str)
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
        criteria_to_save = result.get("criteria") or {}

        # Fallback: extract zone from user message if AI didn't put it in criteria
        if not criteria_to_save.get('zones') and not criteria_to_save.get('zone'):
            import re
            city_patterns = {
                'neuchâtel': 'Neuchâtel', 'neuchatel': 'Neuchâtel', 'neuch': 'Neuchâtel',
                'lausanne': 'Lausanne', 'genève': 'Genève', 'geneve': 'Genève',
                'montreux': 'Montreux', 'fribourg': 'Fribourg', 'sion': 'Sion',
                'nyon': 'Nyon', 'morges': 'Morges', 'yverdon': 'Yverdon',
                'vevey': 'Vevey', 'bienne': 'Bienne', 'biel': 'Bienne',
            }
            msg_lower = message.lower()
            # Find ALL city mentions and their positions — take the LAST one
            # (handles "Neuchâtel au lieu de Lausanne" → picks Lausanne wrongly if first match)
            # Instead: if "au lieu de" / "plutôt que" / "remplacer" pattern, take the FIRST city
            found_cities = []
            for key, city_name in city_patterns.items():
                pos = msg_lower.rfind(key)
                if pos != -1:
                    found_cities.append((pos, key, city_name))
            if found_cities:
                # If message contains "au lieu de", "plutôt que", "à la place de" → take the city BEFORE that phrase
                replace_patterns = ['au lieu de', 'plutôt que', 'à la place de', 'remplacer', 'changer pour', 'passer à', 'passer a']
                target_city = None
                for rp in replace_patterns:
                    rp_pos = msg_lower.find(rp)
                    if rp_pos != -1:
                        # Take city that appears BEFORE the replace phrase (the new target)
                        before_cities = [(p, k, c) for p, k, c in found_cities if p < rp_pos]
                        if before_cities:
                            target_city = max(before_cities, key=lambda x: x[0])[2]
                            break
                        # If no city before, take city after (e.g. "changer pour Neuchâtel")
                        after_cities = [(p, k, c) for p, k, c in found_cities if p > rp_pos]
                        if after_cities:
                            target_city = min(after_cities, key=lambda x: x[0])[2]
                            break
                # No replace pattern found — just take the last mentioned city
                if not target_city:
                    target_city = max(found_cities, key=lambda x: x[0])[2]

                radius_match = re.search(r'(\d+)\s*km', msg_lower)
                radius = int(radius_match.group(1)) if radius_match else 5
                criteria_to_save['zones'] = [{'city': target_city, 'radius_km': radius}]
                print(f"[CHAT] Fallback zone extraction: {target_city} +{radius}km from message: {message}")

        if criteria_to_save:
            try:
                _save_chat_criteria(db_uid, criteria_to_save)
                # Auto re-score after criteria change
                try:
                    from scoring_engine import score_all_for_profile
                    conn_score = get_db()
                    cur_score = conn_score.cursor()
                    cur_score.execute(
                        "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
                        (db_uid,)
                    )
                    p_row = cur_score.fetchone()
                    if p_row:
                        scored = score_all_for_profile(conn_score, p_row['id'])
                        print(f"[CHAT] Auto re-scored {scored} properties after criteria update")
                    cur_score.close()
                    conn_score.close()
                except Exception as score_err:
                    print(f"[CHAT] Auto re-score error: {score_err}")
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
    print(f"[SAVE] user_id={user_id} (type={type(user_id).__name__}) criteria={json.dumps(criteria, ensure_ascii=False, default=str)}")
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

    # Handle zones separately — support multiple formats
    zones_data = criteria.get('zones') or criteria.get('zone')
    if zones_data:
        # Normalize to list of dicts
        zone_list = []
        if isinstance(zones_data, list):
            zone_list = zones_data
        elif isinstance(zones_data, dict):
            zone_list = [zones_data]
        elif isinstance(zones_data, str):
            # Simple string like "Neuchâtel" or "Neuchâtel +3km"
            import re
            m = re.match(r'(.+?)\s*\+?\s*(\d+)\s*km', zones_data)
            if m:
                zone_list = [{'city': m.group(1).strip(), 'radius_km': int(m.group(2))}]
            else:
                zone_list = [{'city': zones_data.strip()}]

        # Delete existing zones first so chat criteria REPLACE old zones
        cur.execute("DELETE FROM search_zones WHERE profile_id = %s", (profile_id,))
        print(f"[SAVE] Deleted old zones for profile_id={profile_id}, inserting {len(zone_list)} new zones")

        for zone in zone_list:
            if isinstance(zone, dict) and zone.get('city'):
                city = zone['city'].strip()
                canton = zone.get('canton', '')
                radius = zone.get('radius_km', 5)
                # Auto-detect canton if not provided
                if not canton:
                    canton_map = {
                        'lausanne': 'VD', 'morges': 'VD', 'nyon': 'VD', 'vevey': 'VD', 'montreux': 'VD', 'yverdon': 'VD', 'renens': 'VD', 'pully': 'VD',
                        'genève': 'GE', 'geneve': 'GE', 'carouge': 'GE', 'lancy': 'GE', 'vernier': 'GE',
                        'neuchâtel': 'NE', 'neuchatel': 'NE', 'la chaux-de-fonds': 'NE',
                        'fribourg': 'FR', 'bulle': 'FR',
                        'sion': 'VS', 'sierre': 'VS', 'martigny': 'VS',
                        'bienne': 'BE', 'biel': 'BE', 'bern': 'BE', 'berne': 'BE',
                        'delémont': 'JU', 'delemont': 'JU',
                    }
                    canton = canton_map.get(city.lower(), '')
                print(f"[SAVE] Saving zone: city={city}, canton={canton}, radius={radius}")
                cur.execute("""
                    INSERT INTO search_zones (profile_id, city, canton, radius_km)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (profile_id, city) DO UPDATE SET
                        canton = EXCLUDED.canton, radius_km = EXCLUDED.radius_km
                """, (profile_id, city, canton, radius))
            elif isinstance(zone, str):
                # Plain string zone name
                import re
                m = re.match(r'(.+?)\s*\+?\s*(\d+)\s*km', zone)
                city = m.group(1).strip() if m else zone.strip()
                radius = int(m.group(2)) if m else 5
                print(f"[SAVE] Saving string zone: city={city}, radius={radius}")
                cur.execute("""
                    INSERT INTO search_zones (profile_id, city, canton, radius_km)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (profile_id, city) DO UPDATE SET
                        radius_km = EXCLUDED.radius_km
                """, (profile_id, city, '', radius))
            else:
                print(f"[SAVE] Skipping invalid zone: {zone}")
    elif 'zones' in criteria:
        print(f"[SAVE] Zones key present but empty: {criteria['zones']}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"[SAVE] Done for user_id={user_id}")


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
    """Re-score listings for the user's profile (scraping is handled by cron job)."""
    from scoring_engine import score_all_for_profile
    import time as _time

    start = _time.time()

    conn = get_db()
    cur = conn.cursor()
    try:
        # Get user's active profile
        cur.execute(
            "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
            (request.user_id,)
        )
        prof_row = cur.fetchone()
        if not prof_row:
            return jsonify({"error": "Aucun profil de recherche. Parlez a Lou pour configurer vos critères."}), 400

        # Re-score all properties against user's current criteria
        scored = score_all_for_profile(conn, prof_row['id'])
        elapsed = _time.time() - start
        print(f"[REFRESH] Re-scored {scored} properties for profile {prof_row['id']} in {elapsed:.1f}s")

        return jsonify({
            "ok": True,
            "total_scraped": scored,
            "total_saved": 0,
            "scored": scored,
            "time_s": round(elapsed, 1)
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


def _run_scrape_job(targets):
    """Background scraping job — runs in a separate thread so the HTTP request can return immediately."""
    import time as _time
    from scrapers import scrape_all, save_to_db
    from scoring_engine import score_all_for_profile

    start = _time.time()
    conn = get_db()
    cur = conn.cursor()

    try:
        total_scraped = 0
        total_saved = 0
        for city, transaction in targets:
            try:
                listings = scrape_all(city=city, transaction=transaction)
                saved = save_to_db(conn, listings)
                total_scraped += len(listings)
                total_saved += saved
                print(f"[CRON] {city} ({transaction}): {len(listings)} scraped, {saved} saved")
            except Exception as e:
                print(f"[CRON] {city} failed: {e}")
                conn.rollback()

        # Deactivate old listings
        cur.execute("""
            UPDATE properties SET is_active = FALSE
            WHERE scraped_at < NOW() - INTERVAL '30 days' AND is_active = TRUE
        """)
        deactivated = cur.rowcount
        conn.commit()

        # Re-score ALL active profiles
        cur.execute("SELECT id FROM search_profiles WHERE is_active = TRUE")
        profiles = cur.fetchall()
        total_scored = 0
        for p in profiles:
            try:
                scored = score_all_for_profile(conn, p['id'])
                total_scored += scored
            except Exception as e:
                print(f"[CRON] Score error profile {p['id']}: {e}")
                conn.rollback()

        elapsed = _time.time() - start
        print(f"[CRON] Done in {elapsed:.1f}s: {total_scraped} scraped, {total_saved} saved, {total_scored} scored, {deactivated} deactivated")

    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        cur.close()
        conn.close()


@app.route('/api/cron/scrape', methods=['POST'])
def api_cron_scrape():
    """Centralized scraping endpoint — called by Render Cron Job or manually.
    Protected by CRON_SECRET env var. Launches scraping in background thread."""
    import threading

    # Auth: require CRON_SECRET header or param
    cron_secret = os.environ.get('CRON_SECRET', '')
    provided = request.headers.get('X-Cron-Secret') or request.args.get('secret', '')
    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    cur = conn.cursor()

    try:
        # Get all unique cities from active profiles
        cur.execute("""
            SELECT DISTINCT sz.city, sp.transaction
            FROM search_zones sz
            JOIN search_profiles sp ON sp.id = sz.profile_id
            WHERE sp.is_active = TRUE AND sz.city IS NOT NULL AND sz.city != ''
        """)
        targets = [(r['city'], r['transaction'] or 'location') for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    if not targets:
        return jsonify({"ok": True, "message": "No active profiles/cities"})

    # Launch scraping in background thread — return immediately
    thread = threading.Thread(target=_run_scrape_job, args=(targets,), daemon=True)
    thread.start()

    return jsonify({
        "ok": True,
        "message": f"Scraping lancé en arrière-plan pour {len(targets)} ville(s)",
        "targets": [{"city": c, "transaction": t} for c, t in targets]
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

        # Clean old scores and re-score with current profile
        cur.execute("DELETE FROM scored_properties WHERE profile_id = %s", (profile['id'],))

        # Get properties matching transaction type
        cur.execute("SELECT * FROM properties WHERE is_active = TRUE AND transaction = %s", (profile['transaction'],))
        properties = cur.fetchall()

        # Fallback: if no matching properties, score all
        if not properties:
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
    """Test scrapers individually. Use ?portal=Flatfox to test one, or ?portal=all for all."""
    import traceback, time as _time
    city = request.args.get('city', 'Lausanne')
    tx = request.args.get('transaction', 'achat')
    portal_filter = request.args.get('portal', 'all')

    from scrapers import (scrape_flatfox, scrape_immoscout24, scrape_homegate,
                          scrape_comparis, scrape_anibis, scrape_immobilier_ch,
                          scrape_acheter_louer, scrape_properstar, scrape_newhome,
                          scrape_tutti, scrape_realadvisor, SCRAPINGBEE_KEY)

    all_scrapers = [
        ('Flatfox', scrape_flatfox),
        ('ImmoScout24', scrape_immoscout24),
        ('Homegate', scrape_homegate),
        ('Comparis', scrape_comparis),
        ('Anibis', scrape_anibis),
        ('Immobilier.ch', scrape_immobilier_ch),
        ('Acheter-Louer', scrape_acheter_louer),
        ('Properstar', scrape_properstar),
        ('Newhome', scrape_newhome),
        ('Tutti', scrape_tutti),
        ('RealAdvisor', scrape_realadvisor),
    ]

    # Filter to specific portal(s)
    if portal_filter != 'all':
        names = [p.strip() for p in portal_filter.split(',')]
        all_scrapers = [(n, s) for n, s in all_scrapers if n.lower() in [x.lower() for x in names]]

    results = {
        "city": city,
        "transaction": tx,
        "scrapingbee_active": bool(SCRAPINGBEE_KEY),
        "portals": {},
    }
    total = 0

    for name, scraper in all_scrapers:
        start = _time.time()
        try:
            # All scrapers accept max_pages except flatfox (uses limit)
            if name == 'Flatfox':
                items = scraper(city=city, transaction=tx, limit=10)
            else:
                items = scraper(city=city, transaction=tx, max_pages=1)
            elapsed = round(_time.time() - start, 1)
            results["portals"][name] = {
                "count": len(items),
                "status": "ok" if items else "empty",
                "time_s": elapsed,
                "sample": [
                    {
                        "id": l["external_id"],
                        "title": l["title"][:80],
                        "price": l["price"],
                        "rooms": l["rooms"],
                        "url": l["source_url"][:120],
                    }
                    for l in items[:3]
                ]
            }
            total += len(items)
        except Exception as e:
            elapsed = round(_time.time() - start, 1)
            results["portals"][name] = {
                "count": 0,
                "status": "error",
                "time_s": elapsed,
                "error": str(e),
                "traceback": traceback.format_exc()[-500:],
            }

    results["total"] = total
    return jsonify(results)


@app.route('/api/scrape/test', methods=['GET'])
def api_scrape_test():
    """Debug endpoint: test raw HTTP connectivity to each portal using scrapers' HTTP client."""
    import re as re_mod
    from scrapers import _get, _get_json, _curl_session, _cloudscraper_session, PROXY_URL, PROXY_URL

    city = request.args.get('city', 'Lausanne')
    slug = city.lower().replace(' ', '-').replace('â', 'a').replace('é', 'e')

    # Report which HTTP clients are active
    clients = []
    if PROXY_URL:
        clients.append("residential_proxy ★")
    if _curl_session:
        clients.append("curl_cffi (Chrome TLS)")
    if _cloudscraper_session:
        clients.append("cloudscraper (JS solver)")
    clients.append("requests (fallback)")

    results = {"http_clients": clients, "fallback_chain": " → ".join(clients), "proxy_active": bool(PROXY_URL)}

    # Test each portal
    portals = [
        ("Flatfox_API", f"https://flatfox.ch/api/v1/public-listing/?city={city}&offer_type=SALE&limit=3"),
        ("ImmoScout24", f"https://www.immoscout24.ch/fr/immobilier/acheter/lieu-{slug}"),
        ("Homegate", f"https://www.homegate.ch/buy/real-estate/city-{slug}/matching-list"),
        ("Comparis", f"https://www.comparis.ch/immobilien/result/list?requestobject=%7B%22DealType%22%3A20%2C%22LocationSearchString%22%3A%22{city}%22%2C%22Sort%22%3A3%2C%22Page%22%3A1%7D"),
        ("Anibis", f"https://www.anibis.ch/fr/immobilier--acheter/{slug}"),
        ("Immobilier.ch", f"https://www.immobilier.ch/fr/acheter/appartement-maison/{slug}"),
        ("Acheter-Louer", f"https://www.acheter-louer.ch/acheter/{slug}"),
        ("Properstar", f"https://www.properstar.ch/switzerland/{slug}/buy/apartment"),
        ("Newhome", f"https://www.newhome.ch/fr/acheter/immobilier/{slug}/liste"),
        ("Tutti", f"https://www.tutti.ch/fr/immobilier/{slug}"),
        ("RealAdvisor", f"https://realadvisor.ch/fr/acheter/{slug}"),
    ]

    for name, url in portals:
        try:
            status, html = _get(url, timeout=15)
            has_next = '__NEXT_DATA__' in html
            has_init = '__INITIAL_STATE__' in html
            is_cloudflare = 'Just a moment' in html[:500] or 'cf-browser-verification' in html[:2000]

            info = {
                "http": status,
                "page_size": len(html),
                "has_NEXT_DATA": has_next,
                "has_INITIAL_STATE": has_init,
                "cloudflare_blocked": is_cloudflare,
            }

            # Show snippet of what we got
            if has_next:
                m = re_mod.search(r'<script id="__NEXT_DATA__"[^>]*>(.{0,200})', html)
                info["next_data_start"] = m.group(1)[:200] if m else ''
            elif has_init:
                m = re_mod.search(r'window\.__INITIAL_STATE__\s*=\s*(.{0,200})', html)
                info["init_state_start"] = m.group(1)[:200] if m else ''
            elif is_cloudflare:
                info["note"] = "Cloudflare challenge page — TLS bypass not working for this site"
            else:
                # Show title for debugging
                m = re_mod.search(r'<title>(.*?)</title>', html[:2000])
                info["page_title"] = m.group(1)[:100] if m else html[:200]

            results[name] = info
        except Exception as e:
            results[name] = {"error": str(e)}

    return jsonify({"city": city, "results": results})


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


@app.route('/privacy')
def privacy():
    return send_from_directory('static', 'privacy.html')


@app.route('/terms')
def terms():
    return send_from_directory('static', 'terms.html')


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
