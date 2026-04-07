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
import time
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from collections import defaultdict

import jwt
import bcrypt
import psycopg2
import psycopg2.extras
import psycopg2.pool
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-app')

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__, static_folder='static')

# CORS: restrict to known domains (add your domains here)
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://garou.ch,https://www.garou.ch,https://lou-platform.onrender.com,http://localhost:5000').split(',')
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

DATABASE_URL = os.environ.get('DATABASE_URL', '')
JWT_SECRET = os.environ.get('JWT_SECRET', '')
if not JWT_SECRET:
    log.warning("JWT_SECRET not set! Using random secret (tokens won't persist across restarts)")
    import secrets
    JWT_SECRET = secrets.token_hex(32)
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
CRON_SECRET = os.environ.get('CRON_SECRET', '')

# Anthropic client singleton
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Simple rate limiter for chat endpoint
_chat_rate = defaultdict(list)  # user_id -> list of timestamps
CHAT_RATE_LIMIT = 20  # max requests per minute
CHAT_RATE_WINDOW = 60  # seconds


_db_pool = None

def _get_pool():
    global _db_pool
    if _db_pool is None and DATABASE_URL:
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10, DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _db_pool

def get_db():
    """Get a database connection from pool."""
    pool = _get_pool()
    if pool:
        conn = pool.getconn()
        conn.autocommit = False
        return conn
    # Fallback for local dev without pool
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn

def return_db(conn):
    """Return a connection to the pool."""
    pool = _get_pool()
    if pool:
        pool.putconn(conn)
    else:
        conn.close()


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
        {'user_id': user_id, 'exp': datetime.now(timezone.utc) + timedelta(days=30)},
        JWT_SECRET, algorithm='HS256'
    )


def _check_rate_limit(user_id):
    """Simple in-memory rate limiter. Returns True if allowed."""
    now = time.time()
    timestamps = _chat_rate[user_id]
    # Remove old entries
    _chat_rate[user_id] = [t for t in timestamps if now - t < CHAT_RATE_WINDOW]
    if len(_chat_rate[user_id]) >= CHAT_RATE_LIMIT:
        return False
    _chat_rate[user_id].append(now)
    return True


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
        return_db(conn)
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
        return_db(conn)


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
        return_db(conn)


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
        return_db(conn)


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
        return_db(conn)


# ============================================================
# PROPERTIES ENDPOINTS
# ============================================================

@app.route('/api/properties', methods=['GET'])
@token_required
def get_properties():
    user_id = request.user_id
    sort = request.args.get('sort', 'score')
    min_score = int(request.args.get('min_score', 0))
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)  # Cap at 50
    offset = (page - 1) * per_page

    order_map = {
        'score': 'sp.total_score DESC',
        'price_asc': 'p.price ASC',
        'price_desc': 'p.price DESC',
        'newest': 'p.published_at DESC NULLS LAST',
        'surface': 'p.surface DESC NULLS LAST'
    }
    order = order_map.get(sort)
    if not order:
        order = 'sp.total_score DESC'

    conn = get_db()
    cur = conn.cursor()
    try:
        # Get properties with scores — order is safe (from whitelist above)
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
        return_db(conn)


@app.route('/api/stats', methods=['GET'])
@token_required
def get_stats():
    user_id = request.user_id
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
        return_db(conn)


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
        return_db(conn)


# ============================================================
# CHATBOT IA ENDPOINT
# ============================================================

LOU_SYSTEM_PROMPT = """Tu es Lou, un chasseur immobilier digital suisse. Tu es un loup sympathique et efficace.

TON ROLE: Aider les gens a definir leur recherche immobiliere en Suisse romande via une conversation naturelle.

REGLES:
- Parle en francais, tutoie, sois chaleureux mais pro
- UNE question a la fois
- Si l'utilisateur donne plusieurs infos d'un coup, enregistre tout
- Sois bref: 1-3 phrases max
- Emojis avec parcimonie

CRITERES A COLLECTER:
1. Zone: ville(s), canton, rayon km
2. Type: appartement, maison, villa, studio, loft, attique, duplex
3. Transaction: location ou achat
4. Budget: min et/ou max CHF
5. Pieces: min et/ou max
6. Surface: m2 min
7. Priorites: balcon, parking, vue, calme, transports, animaux, cave, jardin, ascenseur
8. Etage (optionnel)
9. Date emmenagement (optionnel)

COMPORTEMENT:
- Commence par te presenter et demander la region
- Quand tu as zone + type + transaction + budget, propose de creer l'espace
- Ne force pas les criteres optionnels

REPONSE: JSON uniquement:
{"message":"texte","suggestions":["btn1","btn2"],"criteria":{"zones":[{"city":"X","canton":"XX","radius_km":3}],"property_types":["appartement"],"transaction":"location","budget_min":null,"budget_max":2500,"currency":"CHF","rooms_min":3,"rooms_max":null,"surface_min":null,"floor_preference":null,"priorities":["vue"],"move_date":null},"profile_ready":false,"confirmed":false}

Cantons romands: VD, GE, NE, FR, VS, JU, BE"""

def _load_chat_history(user_id):
    """Load conversation history from DB."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT role, content FROM conversations
            WHERE (user_id = %s OR session_id = %s)
            ORDER BY created_at ASC
            LIMIT 20
        """, (user_id if user_id.isdigit() else None, user_id))
        rows = cur.fetchall()
        return [{"role": r['role'], "content": r['content']} for r in rows]
    except Exception:
        return []
    finally:
        cur.close()
        return_db(conn)


def _save_chat_message(user_id, role, content, criteria_json=None):
    """Save a chat message to DB."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO conversations (user_id, session_id, role, content, criteria_json)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            int(user_id) if user_id.isdigit() else None,
            user_id,
            role,
            content,
            json.dumps(criteria_json) if criteria_json else None
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"Failed to save chat message: {e}")
    finally:
        cur.close()
        return_db(conn)


def _parse_llm_json(raw):
    """Robustly parse JSON from Claude's response, handling markdown wrapping."""
    text = raw.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Handle ```json ... ``` wrapping
    import re
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try to find first { ... } block
    brace_start = text.find('{')
    brace_end = text.rfind('}')
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start:brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json or {}
    user_id = str(data.get('user_id', 'anon'))
    message = data.get('message', '').strip()

    if not message:
        return jsonify({"error": "Message vide"}), 400

    if not anthropic_client:
        return jsonify({
            "message": "Chatbot IA non configure. Ajoutez ANTHROPIC_API_KEY.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        })

    # Rate limiting
    if not _check_rate_limit(user_id):
        return jsonify({
            "message": "Trop de messages. Attends quelques secondes avant de reessayer.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        }), 429

    # Load history from DB
    history = _load_chat_history(user_id)

    # Build messages
    messages = list(history)
    messages.append({"role": "user", "content": message})

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=LOU_SYSTEM_PROMPT,
            messages=messages
        )
        raw = response.content[0].text.strip()

        # Parse JSON robustly
        result = _parse_llm_json(raw)

        if result is None:
            # Claude didn't return valid JSON — use raw text as message
            result = {
                "message": raw,
                "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
            }

        result.setdefault("message", "...")
        result.setdefault("suggestions", [])
        result.setdefault("criteria", {})
        result.setdefault("profile_ready", False)
        result.setdefault("confirmed", False)

        # Save to DB
        _save_chat_message(user_id, "user", message)
        _save_chat_message(user_id, "assistant", raw, criteria_json=result.get("criteria"))

        return jsonify(result)

    except Exception as e:
        log.error(f"Chat error for user {user_id}: {e}")
        return jsonify({
            "message": "Probleme technique, reessaie dans quelques secondes.",
            "suggestions": ["Reessayer"], "criteria": {},
            "profile_ready": False, "confirmed": False
        }), 500


@app.route('/api/chat/reset', methods=['POST'])
def chat_reset():
    data = request.json or {}
    uid = str(data.get('user_id', 'anon'))
    # Delete conversation history from DB
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM conversations
            WHERE user_id = %s OR session_id = %s
        """, (int(uid) if uid.isdigit() else None, uid))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        return_db(conn)
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
            return_db(conn)
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
        return_db(conn)

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
        return_db(conn)

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
        return_db(conn)

        return jsonify({"ok": True, "scored": scored, "profile_id": profile['id']})

    except Exception as e:
        log.error(f"Score error: {e}", exc_info=True)
        return jsonify({"error": "Erreur lors du scoring. Verifiez votre profil."}), 500


def _require_cron_secret():
    """Check that the request has the correct cron secret."""
    secret = request.args.get('secret', '')
    if not CRON_SECRET or secret != CRON_SECRET:
        return False
    return True


@app.route('/api/scrape/debug', methods=['GET'])
def api_scrape_debug():
    """Test ScrapingBee with a single Homegate request. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403
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
            log.error(f"Homegate scraper error: {e}", exc_info=True)
            results["homegate_scraper"] = {"error": str(e)}

        return jsonify(results)

    except Exception as e:
        log.error(f"Scrape debug error: {e}", exc_info=True)
        return jsonify({"error": "Internal error"}), 500


@app.route('/api/scrape/test', methods=['GET'])
def api_scrape_test():
    """Debug endpoint: test raw HTTP responses from each portal. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403
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
# CRON ENDPOINT
# ============================================================

@app.route('/api/cron/scrape', methods=['POST', 'GET'])
def api_cron_scrape():
    """Trigger scraping via cron. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403

    from scrapers import scrape_all, save_to_db
    from scoring_engine import score_property

    conn = get_db()
    cur = conn.cursor()
    try:
        # Get all active profiles with their zones
        cur.execute("""
            SELECT sp.*, u.email,
                   json_agg(json_build_object(
                       'city', sz.city, 'canton', sz.canton,
                       'radius_km', sz.radius_km
                   )) as zones
            FROM search_profiles sp
            JOIN users u ON u.id = sp.user_id
            LEFT JOIN search_zones sz ON sz.profile_id = sp.id
            WHERE sp.is_active = TRUE AND u.is_active = TRUE
            GROUP BY sp.id, u.email
        """)
        profiles = cur.fetchall()

        if not profiles:
            return jsonify({"ok": True, "message": "No active profiles"})

        # Collect unique city + transaction combos
        scrape_targets = set()
        for p in profiles:
            zones = p.get('zones', [])
            tx = p.get('transaction', 'location')
            if zones:
                for z in zones:
                    if isinstance(z, dict) and z.get('city'):
                        scrape_targets.add((z['city'], tx))

        # Scrape
        total_saved = 0
        for city, transaction in scrape_targets:
            try:
                listings = scrape_all(city=city, transaction=transaction)
                if listings:
                    saved = save_to_db(conn, listings)
                    total_saved += saved
                    log.info(f"Cron: saved {saved} for {city} ({transaction})")
            except Exception as e:
                log.error(f"Cron: scrape failed for {city}: {e}")
                conn.rollback()

        # Score all profiles
        scored_total = 0
        for p in profiles:
            p = dict(p)
            profile_id = p['id']
            user_id = p['user_id']
            zones_data = [dict(z) for z in (p.get('zones') or []) if isinstance(z, dict) and z.get('city')]

            cur.execute("SELECT * FROM properties WHERE is_active = TRUE AND transaction = %s",
                        (p.get('transaction', 'location'),))
            properties = cur.fetchall()

            for prop in properties:
                prop = dict(prop)
                result = score_property(prop, p, zones_data)
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
                    prop['id'], profile_id, user_id,
                    result['total_score'], result['grade'],
                    result['score_zone'], result['score_budget'],
                    result['score_type'], result['score_surface'],
                    result['score_equipment'], result['score_freshness'],
                    result['distance_km']
                ))
                scored_total += 1

            conn.commit()

        # Deactivate old listings
        cur.execute("""
            UPDATE properties SET is_active = FALSE
            WHERE scraped_at < NOW() - INTERVAL '30 days' AND is_active = TRUE
        """)
        conn.commit()

        return jsonify({"ok": True, "saved": total_saved, "scored": scored_total})

    except Exception as e:
        conn.rollback()
        log.error(f"Cron error: {e}", exc_info=True)
        return jsonify({"error": "Cron job failed"}), 500
    finally:
        cur.close()
        return_db(conn)


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
