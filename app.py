"""
Bon Home — Backend V2
Flask API avec PostgreSQL, Claude AI chatbot, scoring engine

Environment variables needed on Render:
  DATABASE_URL=postgresql://...
  ANTHROPIC_API_KEY=sk-ant-...
  JWT_SECRET=your-secret-key
  FLASK_ENV=production
"""

import os
import re
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
import requests as http_requests
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-app')

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__, static_folder='static')


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# CORS: restrict to known domains (add your domains here)
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://bonhome.ch,https://www.bonhome.ch,https://garou.ch,https://www.garou.ch,https://lou-platform.onrender.com,http://localhost:5000').split(',')
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

DATABASE_URL = os.environ.get('DATABASE_URL', '')
JWT_SECRET = os.environ.get('JWT_SECRET', '')
if not JWT_SECRET:
    log.warning("JWT_SECRET not set! Using random secret (tokens won't persist across restarts)")
    import secrets
    JWT_SECRET = secrets.token_hex(32)
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
CRON_SECRET = os.environ.get('CRON_SECRET', '')
HCAPTCHA_SECRET = os.environ.get('HCAPTCHA_SECRET', '')
HCAPTCHA_SITEKEY = os.environ.get('HCAPTCHA_SITEKEY', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')  # Email to receive signup notifications + admin access

# Anthropic client singleton
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Simple rate limiter for chat endpoint
_chat_rate = defaultdict(list)  # user_id -> list of timestamps
CHAT_RATE_LIMIT = 20  # max requests per minute
CHAT_RATE_WINDOW = 60  # seconds
ANON_RATE_LIMIT = 5  # anonymous users get fewer messages per minute


def get_db():
    """Create a fresh database connection (no pool — eliminates SSL stale issues)."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def return_db(conn):
    """Close the database connection."""
    try:
        conn.close()
    except Exception:
        pass


def _run_migrations():
    """Run schema migrations on startup (idempotent)."""
    try:
        db = get_db()
        cur = db.cursor()
        # Add first_seen_at to properties if missing
        cur.execute("""
            ALTER TABLE properties ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT NOW()
        """)
        # Create price_history table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id              SERIAL PRIMARY KEY,
                property_id     INTEGER REFERENCES properties(id) ON DELETE CASCADE,
                old_price       INTEGER,
                new_price       INTEGER,
                change_pct      DECIMAL(5,2),
                detected_at     TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_price_hist_prop ON price_history(property_id, detected_at DESC)")
        # Backfill first_seen_at from scraped_at for existing rows
        cur.execute("UPDATE properties SET first_seen_at = scraped_at WHERE first_seen_at IS NULL")
        # Auto-create alert rows for users who have an active profile but no alert
        cur.execute("""
            INSERT INTO alerts (user_id, profile_id, channel, frequency, min_score, is_active)
            SELECT DISTINCT ON (u.id) u.id, sp.id, 'email', 'daily', 70, TRUE
            FROM users u
            JOIN search_profiles sp ON sp.user_id = u.id AND sp.is_active = TRUE
            LEFT JOIN alerts a ON a.user_id = u.id
            WHERE a.id IS NULL AND u.is_active = TRUE
            ORDER BY u.id, sp.created_at
        """)
        db.commit()
        cur.close()
        return_db(db)
        log.info("Migrations OK")
    except Exception as e:
        log.error(f"Migration error: {e}")

_run_migrations()


def token_required(f):
    """JWT authentication decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            token = request.args.get('token', '')
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
        {'user_id': user_id, 'exp': datetime.now(timezone.utc) + timedelta(days=7)},
        JWT_SECRET, algorithm='HS256'
    )


def _check_rate_limit(user_id, is_anonymous=False):
    """Simple in-memory rate limiter. Returns True if allowed."""
    now = time.time()
    timestamps = _chat_rate[user_id]
    # Remove old entries
    _chat_rate[user_id] = [t for t in timestamps if now - t < CHAT_RATE_WINDOW]
    limit = ANON_RATE_LIMIT if is_anonymous else CHAT_RATE_LIMIT
    if len(_chat_rate[user_id]) >= limit:
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
# PUBLIC CONFIG (expose non-secret settings to frontend)
# ============================================================

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'hcaptcha_sitekey': HCAPTCHA_SITEKEY or ''
    })


# ============================================================
def verify_hcaptcha(token):
    """Verify hCaptcha token. Returns True if valid or if CAPTCHA is not configured."""
    if not HCAPTCHA_SECRET:
        return True  # Skip if not configured
    try:
        resp = http_requests.post('https://api.hcaptcha.com/siteverify', data={
            'secret': HCAPTCHA_SECRET,
            'response': token
        }, timeout=5)
        return resp.json().get('success', False)
    except Exception as e:
        log.error(f"hCaptcha verification error: {e}")
        return True  # Fail open — don't block signups if hCaptcha is down


def notify_new_signup(user_email, user_name):
    """Send email notification to admin when a new user signs up."""
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        return
    try:
        http_requests.post('https://api.resend.com/emails', json={
            'from': 'Bon Home <noreply@bonhome.ch>',
            'to': [ADMIN_EMAIL],
            'subject': f'Nouvelle inscription — {user_name or user_email}',
            'html': f'''<div style="font-family:sans-serif;max-width:500px">
                <h2 style="color:#0369a1">Nouvelle inscription sur Bon Home</h2>
                <p><strong>Nom :</strong> {user_name or "Non renseigne"}</p>
                <p><strong>Email :</strong> {user_email}</p>
                <p><strong>Date :</strong> {datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")}</p>
                <hr style="border:none;border-top:1px solid #e2e8f0">
                <p style="color:#64748b;font-size:13px">Bon Home — bonhome.ch</p>
            </div>'''
        }, headers={
            'Authorization': f'Bearer {RESEND_API_KEY}',
            'Content-Type': 'application/json'
        }, timeout=5)
    except Exception as e:
        log.error(f"Resend notification error: {e}")


# AUTH ENDPOINTS
# ============================================================

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    name = data.get('name', '')
    criteria = data.get('criteria', {})

    captcha_token = data.get('captcha_token', '')

    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({"error": "Email invalide"}), 400
    if len(password) < 8:
        return jsonify({"error": "Mot de passe trop court (8 car. min)"}), 400
    if HCAPTCHA_SECRET and not verify_hcaptcha(captcha_token):
        return jsonify({"error": "Verification CAPTCHA echouee"}), 400

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
        # Lou sends: property_types (array), transaction, budget_max (int),
        #            rooms_min (float), zones (array of {city, canton, radius_km})
        if criteria:
            prop_types = criteria.get('property_types') or [criteria.get('property_type', 'appartement')]
            if isinstance(prop_types, str):
                prop_types = [prop_types]
            transaction = criteria.get('transaction') or criteria.get('transaction_type', 'location')
            budget_max = criteria.get('budget_max') or _parse_budget(criteria.get('budget', ''))
            rooms_min = criteria.get('rooms_min') or _parse_rooms(criteria.get('rooms', ''))

            cur.execute("""
                INSERT INTO search_profiles (user_id, property_types, transaction, budget_max, rooms_min, priorities)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (
                user['id'],
                prop_types,
                transaction,
                budget_max,
                rooms_min,
                criteria.get('priorities', [])
            ))
            profile = cur.fetchone()
            profile_id = profile['id']

            # Create search zones from Lou's zones array
            zones = criteria.get('zones', [])
            if zones and isinstance(zones, list):
                for z in zones:
                    city = z.get('city', '')
                    canton = z.get('canton', '')
                    radius = z.get('radius_km', 3.0)
                    if city:
                        cur.execute("""
                            INSERT INTO search_zones (profile_id, city, canton, radius_km)
                            VALUES (%s, %s, %s, %s)
                        """, (profile_id, city, canton, radius))
            else:
                # Fallback: flat city/canton fields (legacy)
                city = criteria.get('city', '')
                canton = criteria.get('canton', '')
                if city:
                    cur.execute("""
                        INSERT INTO search_zones (profile_id, city, canton, radius_km)
                        VALUES (%s, %s, %s, %s)
                    """, (profile_id, city, canton, 3.0))

        conn.commit()
        token = make_token(user['id'])

        # Notify admin of new signup (async-ish, don't block response)
        notify_new_signup(email, name)

        return jsonify({"ok": True, "token": token, "user": user})

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Email déjà utilisé"}), 409
    except Exception as e:
        conn.rollback()
        log.error(f"Signup error: {e}")
        return jsonify({"error": "Erreur serveur lors de l'inscription"}), 500
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
    except Exception as e:
        conn.rollback()
        log.error(f"Login error: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
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

        # Trigger background scraping + scoring for the user's cities
        cities = [z.get('city') for z in zones if z.get('city')] if zones else []
        transaction = data.get('transaction', 'location')
        if cities:
            import threading
            def _bg_scrape_and_score(city_list, tx, pid, uid):
                from scrapers import scrape_all, save_to_db
                from scoring_engine import score_property
                bg_conn = get_db()
                bg_cur = bg_conn.cursor()
                try:
                    for city in city_list:
                        try:
                            listings = scrape_all(city=city, transaction=tx)
                            if listings:
                                saved = save_to_db(bg_conn, listings)
                                log.info(f"Profile update scrape: saved {saved} for {city} ({tx})")
                        except Exception as e:
                            log.error(f"Profile update scrape failed for {city}: {e}")
                            try:
                                bg_conn.rollback()
                            except Exception:
                                pass

                    # Score properties for this profile
                    bg_cur.execute("SELECT * FROM search_profiles WHERE id = %s", (pid,))
                    profile = dict(bg_cur.fetchone())
                    bg_cur.execute("SELECT * FROM search_zones WHERE profile_id = %s", (pid,))
                    zones_data = [dict(z) for z in bg_cur.fetchall()]

                    query = "SELECT * FROM properties WHERE is_active = TRUE"
                    params = []
                    if profile.get('transaction'):
                        query += " AND transaction = %s"
                        params.append(profile['transaction'])
                    if profile.get('budget_max'):
                        query += " AND (price IS NULL OR price <= %s)"
                        params.append(int(profile['budget_max'] * 1.3))
                    bg_cur.execute(query, params)
                    properties = bg_cur.fetchall()

                    scored = 0
                    for prop in properties:
                        prop = dict(prop)
                        result = score_property(prop, profile, zones_data)
                        bg_cur.execute("""
                            INSERT INTO scored_properties
                                (property_id, profile_id, user_id, total_score, grade,
                                 score_zone, score_budget, score_type, score_surface,
                                 score_equipment, score_freshness, distance_km)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (property_id, profile_id)
                            DO UPDATE SET
                                total_score = EXCLUDED.total_score, grade = EXCLUDED.grade,
                                score_zone = EXCLUDED.score_zone, score_budget = EXCLUDED.score_budget,
                                score_type = EXCLUDED.score_type, score_surface = EXCLUDED.score_surface,
                                score_equipment = EXCLUDED.score_equipment, score_freshness = EXCLUDED.score_freshness,
                                distance_km = EXCLUDED.distance_km, scored_at = NOW()
                        """, (
                            prop['id'], pid, uid,
                            result['total_score'], result['grade'],
                            result['score_zone'], result['score_budget'],
                            result['score_type'], result['score_surface'],
                            result['score_equipment'], result['score_freshness'],
                            result['distance_km']
                        ))
                        scored += 1
                    bg_conn.commit()
                    log.info(f"Profile update scoring: {scored} properties scored for profile {pid}")
                except Exception as e:
                    log.error(f"Profile update bg error: {e}", exc_info=True)
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass
                finally:
                    bg_cur.close()
                    return_db(bg_conn)

            thread = threading.Thread(target=_bg_scrape_and_score, args=(cities, transaction, profile_id, request.user_id))
            thread.daemon = True
            thread.start()

        return jsonify({"ok": True, "profile_id": profile_id})
    except Exception as e:
        conn.rollback()
        log.error(f"Profile update error: {e}")
        return jsonify({"error": "Erreur serveur lors de la mise a jour du profil"}), 500
    finally:
        cur.close()
        return_db(conn)


# ============================================================
# ADMIN ENDPOINTS
# ============================================================

def admin_required(f):
    """Admin access decorator — checks JWT user email against ADMIN_EMAIL."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({"error": "Token manquant"}), 401
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            request.user_id = data['user_id']
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token invalide"}), 401

        if not ADMIN_EMAIL:
            return jsonify({"error": "Admin non configure"}), 403

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT email FROM users WHERE id = %s", (request.user_id,))
            user = cur.fetchone()
            if not user or user['email'] != ADMIN_EMAIL.lower().strip():
                return jsonify({"error": "Acces refuse"}), 403
        finally:
            cur.close()
            return_db(conn)
        return f(*args, **kwargs)
    return decorated


@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    """List all registered users with stats."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.email, u.name, u.created_at, u.last_login, u.is_active, u.plan,
                   COUNT(DISTINCT sp.id) AS profiles_count,
                   COUNT(DISTINCT f.property_id) AS favorites_count
            FROM users u
            LEFT JOIN search_profiles sp ON sp.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created_at DESC
        """)
        users = []
        for row in cur.fetchall():
            users.append({
                'id': row['id'],
                'email': row['email'],
                'name': row['name'] or '',
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'last_login': row['last_login'].isoformat() if row['last_login'] else None,
                'is_active': row['is_active'],
                'plan': row['plan'] or 'free',
                'profiles_count': row['profiles_count'],
                'favorites_count': row['favorites_count'],
            })
        return jsonify({"users": users, "total": len(users)})
    finally:
        cur.close()
        return_db(conn)


@app.route('/api/admin/check', methods=['GET'])
@token_required
def admin_check():
    """Check if current user is admin."""
    if not ADMIN_EMAIL:
        return jsonify({"is_admin": False})
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT email FROM users WHERE id = %s", (request.user_id,))
        user = cur.fetchone()
        is_admin = user and user['email'] == ADMIN_EMAIL.lower().strip()
        return jsonify({"is_admin": is_admin})
    finally:
        cur.close()
        return_db(conn)


# ============================================================
# PROPERTIES ENDPOINTS
# ============================================================

def _days_since(dt):
    """Calculate days since a datetime, handling timezone-naive datetimes."""
    if not dt:
        return None
    try:
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return None

@app.route('/api/properties', methods=['GET'])
@token_required
def get_properties():
    user_id = request.user_id
    sort = request.args.get('sort', 'score')
    min_score = int(request.args.get('min_score', 0))
    new_only = request.args.get('new_only', '').lower() in ('true', '1', 'yes')
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)  # Cap at 50
    offset = (page - 1) * per_page

    order_map = {
        'score': 'sp.total_score DESC, sp.distance_km ASC NULLS LAST',
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
        # Get user's active profile (transaction + profile_id)
        cur.execute("""
            SELECT id, transaction FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        profile_row = cur.fetchone()
        user_transaction = profile_row['transaction'] if profile_row else None
        active_profile_id = profile_row['id'] if profile_row else None

        # Build filters
        tx_filter = ""
        tx_params = [user_id, user_id, min_score]
        if active_profile_id:
            tx_filter += " AND sp.profile_id = %s"
            tx_params.append(active_profile_id)
        if user_transaction:
            tx_filter += " AND p.transaction = %s"
            tx_params.append(user_transaction)
        if new_only:
            tx_filter += " AND p.first_seen_at > NOW() - INTERVAL '24 hours'"

        # Get ALL scored properties (no LIMIT) for cross-portal merging
        cur.execute(f"""
            SELECT p.*, sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   EXISTS(SELECT 1 FROM favorites f WHERE f.user_id = %s AND f.property_id = p.id) as is_favorite,
                   p.first_seen_at,
                   (SELECT json_agg(json_build_object('old_price', ph.old_price, 'new_price', ph.new_price, 'change_pct', ph.change_pct, 'detected_at', ph.detected_at) ORDER BY ph.detected_at DESC) FROM price_history ph WHERE ph.property_id = p.id) as price_changes
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND sp.total_score >= %s AND p.is_active = TRUE
                  AND p.price > 1000{tx_filter}
            ORDER BY {order}
        """, tx_params)
        properties = [dict(r) for r in cur.fetchall()]

        # Format for frontend with cross-portal merging
        def _merge_keys(p):
            """Generate all possible merge keys for a property.
            Returns a list of keys — a property matches if ANY key overlaps with another property's keys.
            This handles portals that have postal_code vs those that don't,
            and portals that may be missing rooms or surface data.
            """
            keys = []
            postal = (p.get('postal_code') or '').strip()
            price = int(p.get('price') or 0)  # Exact price match
            rooms = p.get('rooms')
            rooms_norm = str(round(float(rooms) * 2) / 2) if rooms else ''
            surface = p.get('surface') or 0
            surface_bucket = round(surface / 15) * 15 if surface else 0

            city = (p.get('city') or '').lower().strip()

            # Key 1: city + exact price + rooms + surface (strongest match)
            if city and price and rooms_norm:
                keys.append(f"city:{city}:{price}:{rooms_norm}:{surface_bucket}")
            # Key 2: postal + exact price + rooms (cross-portal match)
            if postal and price and rooms_norm:
                keys.append(f"npa:{postal}:{price}:{rooms_norm}:{surface_bucket}")

            # Key 3: city + exact price + surface (when rooms missing — common on Homegate)
            if city and price and surface_bucket and not rooms_norm:
                keys.append(f"citysurf:{city}:{price}:{surface_bucket}")
            if postal and price and surface_bucket and not rooms_norm:
                keys.append(f"npasurf:{postal}:{price}:{surface_bucket}")

            # Key 4: city + exact price + rooms (when surface missing)
            if city and price and rooms_norm and not surface:
                keys.append(f"cityrooms:{city}:{price}:{rooms_norm}")
            if postal and price and rooms_norm and not surface:
                keys.append(f"nparooms:{postal}:{price}:{rooms_norm}")

            # Last resort: city + price + title prefix (when both rooms and surface missing)
            if not keys:
                title_norm = re.sub(r'[^a-z0-9]', '', (p.get('title') or '').lower())[:30]
                if title_norm:
                    keys.append(f"title:{city}:{price}:{title_norm}")
                elif price:
                    keys.append(f"priceonly:{city}:{price}")

            return keys

        # Group properties by merge key — multi-key approach
        # key_to_group maps each key to a group ID, so properties sharing any key merge together
        merged = {}
        key_to_group = {}  # merge_key -> group_id (first property's id)
        merge_debug = {}
        for p in properties:
            keys = _merge_keys(p)
            src = p.get('source') or 'unknown'

            # Find if any of this property's keys already belong to a group
            group_id = None
            for k in keys:
                if k in key_to_group:
                    group_id = key_to_group[k]
                    break

            if group_id is None:
                # New group
                group_id = p['id']
                merged[group_id] = p
                merged[group_id]['_all_sources'] = [{'source': p['source'] or '', 'url': p['source_url'] or ''}]
                merged[group_id]['_best_images'] = p['images'] or []
                for k in keys:
                    key_to_group[k] = group_id
                merge_debug[group_id] = [f"{src}(id={p['id']},price={p.get('price')},rooms={p.get('rooms')},postal={p.get('postal_code')},city={p.get('city')})"]
            else:
                # Merge into existing group — register all keys
                for k in keys:
                    key_to_group[k] = group_id
                merge_debug[group_id].append(f"{src}(id={p['id']},price={p.get('price')},rooms={p.get('rooms')},postal={p.get('postal_code')},city={p.get('city')})")
                # Merge: add source link (skip if same portal already listed)
                new_src = p['source'] or ''
                existing_sources = {s['source'] for s in merged[group_id]['_all_sources']}
                if new_src not in existing_sources:
                    merged[group_id]['_all_sources'].append({'source': new_src, 'url': p['source_url'] or ''})
                # Combine images from all sources (deduplicated)
                if p['images']:
                    existing_imgs = set(merged[group_id]['_best_images'])
                    for img in p['images']:
                        if img and img not in existing_imgs:
                            merged[group_id]['_best_images'].append(img)
                            existing_imgs.add(img)
                # Keep higher score
                if (p['total_score'] or 0) > (merged[group_id]['total_score'] or 0):
                    old_sources = merged[group_id]['_all_sources']
                    old_images = merged[group_id]['_best_images']
                    merged[group_id] = p
                    merged[group_id]['_all_sources'] = old_sources
                    merged[group_id]['_best_images'] = old_images

        # Log merge stats
        multi_source = {k: v for k, v in merge_debug.items() if len(v) > 1}
        if multi_source:
            log.info(f"Merge: {len(multi_source)} groups merged from {sum(len(v) for v in multi_source.values())} properties")
            for gid, sources in list(multi_source.items())[:5]:
                log.info(f"  Merged group {gid}: {sources}")
        log.info(f"Merge total: {len(properties)} properties -> {len(merged)} unique results")

        results = []
        for p in merged.values():
            all_sources = p.pop('_all_sources', [])
            best_images = p.pop('_best_images', [])
            results.append({
                'id': p['id'],
                'title': (p['title'] or '').strip() or (
                    f"{float(p['rooms'])} pcs, {p['surface']} m2" if p.get('rooms') and p.get('surface')
                    else f"{p.get('city') or 'Bien immobilier'}"
                ),
                'address': (p['address'] or '').strip(),
                'price': p['price'] or 0,
                'unit': f"{(p['currency'] or 'CHF').encode('ascii', 'ignore').decode()}/{(p['price_unit'] or 'mois').encode('ascii', 'ignore').decode()}",
                'rooms': float(p['rooms']) if p['rooms'] else 0,
                'surface': p['surface'] or 0,
                'floor': p['floor'],
                'features': p['features'] or [],
                'source': all_sources[0]['source'] if all_sources else '',
                'source_url': all_sources[0]['url'] if all_sources else '',
                'all_sources': all_sources,
                'description': (p.get('description') or '').strip(),
                'contact_name': p['contact_name'] or '',
                'contact_phone': p['contact_phone'] or '',
                'contact_email': p['contact_email'] or '',
                'images': best_images or p['images'] or [],
                'score': p['total_score'],
                'grade': p['grade'],
                'latitude': p.get('latitude'),
                'longitude': p.get('longitude'),
                'city': p.get('city'),
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
                'is_favorite': p['is_favorite'],
                'first_seen_at': p['first_seen_at'].isoformat() if p.get('first_seen_at') else p['scraped_at'].isoformat() if p.get('scraped_at') else None,
                'days_online': _days_since(p.get('first_seen_at') or p.get('scraped_at')),
                'price_drop': None,
            })

            # Extract latest price drop
            price_changes = p.get('price_changes')
            if price_changes and isinstance(price_changes, list) and len(price_changes) > 0:
                latest = price_changes[0]  # Most recent change
                if latest.get('change_pct') and latest['change_pct'] < 0:
                    results[-1]['price_drop'] = {
                        'old_price': latest['old_price'],
                        'new_price': latest['new_price'],
                        'change_pct': float(latest['change_pct']),
                        'detected_at': latest['detected_at'].isoformat() if hasattr(latest['detected_at'], 'isoformat') else str(latest['detected_at'])
                    }

        # Paginate AFTER merge (total is now the merged count)
        total = len(results)
        page_results = results[offset:offset + per_page]
        return jsonify({"properties": page_results, "total": total, "page": page, "per_page": per_page})
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
        # Get user's active profile
        cur.execute("""
            SELECT id, transaction FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        prof = cur.fetchone()
        user_tx = prof['transaction'] if prof else None
        active_pid = prof['id'] if prof else None

        tx_filter = ""
        extra_params = []
        if active_pid:
            tx_filter += " AND sp.profile_id = %s"
            extra_params.append(active_pid)
        if user_tx:
            tx_filter += " AND p.transaction = %s"
            extra_params.append(user_tx)

        cur.execute(f"""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE p.first_seen_at > NOW() - INTERVAL '24 hours') as new_count,
                (SELECT COUNT(*) FROM favorites WHERE user_id = %s) as favorites
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id AND p.price > 1000
            WHERE sp.user_id = %s AND p.is_active = TRUE{tx_filter}
        """, [user_id, user_id] + extra_params)
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


@app.route('/api/favorite/<int:property_id>/note', methods=['PUT'])
@token_required
def update_favorite_note(property_id):
    """Update the note on a favorited property."""
    data = request.json or {}
    note = (data.get('note') or '').strip()[:500]  # Max 500 chars
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE favorites SET notes = %s WHERE user_id = %s AND property_id = %s RETURNING id",
            (note or None, request.user_id, property_id)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Favori non trouve"}), 404
        conn.commit()
        return jsonify({"ok": True})
    finally:
        cur.close()
        return_db(conn)


@app.route('/api/favorites', methods=['GET'])
@token_required
def get_favorites():
    """Get all favorites with full property data, scores, and notes."""
    user_id = request.user_id
    sort = request.args.get('sort', 'date')
    conn = get_db()
    cur = conn.cursor()
    try:
        order_map = {
            'date': 'f.created_at DESC',
            'score': 'sp.total_score DESC',
            'price_asc': 'p.price ASC NULLS LAST',
            'price_desc': 'p.price DESC NULLS LAST',
        }
        order = order_map.get(sort, 'f.created_at DESC')

        cur.execute(f"""
            SELECT p.*, f.notes as fav_note, f.created_at as fav_date,
                   sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   p.first_seen_at,
                   (SELECT json_agg(json_build_object('old_price', ph.old_price, 'new_price', ph.new_price, 'change_pct', ph.change_pct, 'detected_at', ph.detected_at) ORDER BY ph.detected_at DESC) FROM price_history ph WHERE ph.property_id = p.id) as price_changes
            FROM favorites f
            JOIN properties p ON p.id = f.property_id
            LEFT JOIN scored_properties sp ON sp.property_id = p.id AND sp.user_id = %s
            WHERE f.user_id = %s AND p.is_active = TRUE
            ORDER BY {order}
        """, (user_id, user_id))
        rows = [dict(r) for r in cur.fetchall()]

        # Format like /api/properties
        formatted = []
        for p in rows:
            images = p.get('images') or []
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except Exception:
                    images = [images] if images else []

            title = (p.get('title') or '').strip()
            address = (p.get('address') or '').strip()

            days_online = 0
            if p.get('first_seen_at'):
                days_online = _days_since(p['first_seen_at'])

            price_drop = None
            changes = p.get('price_changes')
            if changes and isinstance(changes, list) and len(changes) > 0:
                latest = changes[0]
                if latest.get('change_pct') and latest['change_pct'] < 0:
                    price_drop = {
                        'old_price': latest['old_price'],
                        'new_price': latest['new_price'],
                        'change_pct': latest['change_pct']
                    }

            item = {
                'id': p['id'],
                'title': title,
                'address': address,
                'city': p.get('city'),
                'price': p.get('price'),
                'unit': p.get('unit'),
                'rooms': p.get('rooms'),
                'surface': p.get('surface'),
                'floor': p.get('floor'),
                'images': images,
                'source': p.get('source'),
                'source_url': p.get('source_url'),
                'transaction': p.get('transaction'),
                'description': p.get('description'),
                'features': p.get('features'),
                'score': p.get('total_score') or 0,
                'grade': p.get('grade') or 'D',
                'latitude': p.get('latitude'),
                'longitude': p.get('longitude'),
                'distance_km': p.get('distance_km'),
                'is_favorite': True,
                'fav_note': p.get('fav_note') or '',
                'fav_date': p.get('fav_date').isoformat() if p.get('fav_date') else None,
                'days_online': days_online,
                'price_drop': price_drop,
                'score_detail': {
                    'zone': p.get('score_zone') or 0,
                    'budget': p.get('score_budget') or 0,
                    'type': p.get('score_type') or 0,
                    'surface': p.get('score_surface') or 0,
                    'equipment': p.get('score_equipment') or 0,
                    'freshness': p.get('score_freshness') or 0,
                },
            }
            formatted.append(item)

        return jsonify({"favorites": formatted, "total": len(formatted)})
    finally:
        cur.close()
        return_db(conn)


@app.route('/api/favorites/export', methods=['GET'])
@token_required
def export_favorites():
    """Export favorites as CSV."""
    user_id = request.user_id
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.title, p.address, p.city, p.price, p.unit, p.rooms, p.surface,
                   p.source, p.source_url, p.transaction,
                   sp.total_score, sp.grade, f.notes, f.created_at as fav_date
            FROM favorites f
            JOIN properties p ON p.id = f.property_id
            LEFT JOIN scored_properties sp ON sp.property_id = p.id AND sp.user_id = %s
            WHERE f.user_id = %s AND p.is_active = TRUE
            ORDER BY f.created_at DESC
        """, (user_id, user_id))
        rows = cur.fetchall()

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Titre', 'Adresse', 'Ville', 'Prix', 'Unite', 'Pieces', 'Surface m2',
                         'Source', 'URL', 'Transaction', 'Score', 'Grade', 'Notes', 'Date favori'])
        for r in rows:
            title = (r.get('title') or '').strip()
            writer.writerow([
                title, r.get('address'), r.get('city'), r.get('price'), r.get('unit'),
                r.get('rooms'), r.get('surface'), r.get('source'), r.get('source_url'),
                r.get('transaction'), r.get('total_score'), r.get('grade'),
                r.get('notes') or '', r.get('fav_date', '')
            ])

        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename=favoris-bonhome.csv'
        return response
    finally:
        cur.close()
        return_db(conn)


# ============================================================
# ALERT SETTINGS ENDPOINTS
# ============================================================

@app.route('/api/alerts', methods=['GET'])
@token_required
def get_alerts():
    """Get user's alert settings (auto-create if none exists)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, frequency, min_score, is_active, channel, last_sent, created_at FROM alerts WHERE user_id = %s LIMIT 1",
            (request.user_id,)
        )
        alert = cur.fetchone()
        if not alert:
            # Get user's first active profile
            cur.execute(
                "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at LIMIT 1",
                (request.user_id,)
            )
            profile = cur.fetchone()
            profile_id = profile['id'] if profile else None
            cur.execute("""
                INSERT INTO alerts (user_id, profile_id, channel, frequency, min_score, is_active)
                VALUES (%s, %s, 'email', 'daily', 70, TRUE)
                RETURNING id, frequency, min_score, is_active, channel, last_sent, created_at
            """, (request.user_id, profile_id))
            alert = cur.fetchone()
            conn.commit()
        return jsonify({
            "id": alert['id'],
            "frequency": alert['frequency'] if alert['is_active'] else 'off',
            "min_score": alert['min_score'],
            "is_active": alert['is_active'],
            "channel": alert['channel'],
            "last_sent": alert['last_sent'].isoformat() if alert['last_sent'] else None,
        })
    except Exception as e:
        conn.rollback()
        log.error(f"GET /api/alerts error: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)


@app.route('/api/alerts', methods=['PUT'])
@token_required
def update_alerts():
    """Update alert settings."""
    data = request.json or {}
    frequency = data.get('frequency')
    min_score = data.get('min_score')

    if frequency and frequency not in ('daily', 'instant', 'weekly', 'off'):
        return jsonify({"error": "Frequence invalide"}), 400
    if min_score is not None:
        try:
            min_score = int(min_score)
            if min_score < 0 or min_score > 100:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "Score minimum invalide"}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        # Ensure alert row exists
        cur.execute("SELECT id FROM alerts WHERE user_id = %s LIMIT 1", (request.user_id,))
        alert = cur.fetchone()
        if not alert:
            cur.execute(
                "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at LIMIT 1",
                (request.user_id,)
            )
            profile = cur.fetchone()
            profile_id = profile['id'] if profile else None
            cur.execute("""
                INSERT INTO alerts (user_id, profile_id, channel, frequency, min_score, is_active)
                VALUES (%s, %s, 'email', 'daily', 70, TRUE)
                RETURNING id
            """, (request.user_id, profile_id))
            alert = cur.fetchone()
            conn.commit()

        updates = []
        params = []
        if frequency == 'off':
            updates.append("is_active = FALSE")
        elif frequency:
            updates.append("frequency = %s")
            updates.append("is_active = TRUE")
            params.append(frequency)
        if min_score is not None:
            updates.append("min_score = %s")
            params.append(min_score)

        if updates:
            params.append(alert['id'])
            cur.execute(f"UPDATE alerts SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()

        # Return updated row
        cur.execute(
            "SELECT id, frequency, min_score, is_active, channel, last_sent FROM alerts WHERE id = %s",
            (alert['id'],)
        )
        updated = cur.fetchone()
        return jsonify({
            "ok": True,
            "frequency": updated['frequency'] if updated['is_active'] else 'off',
            "min_score": updated['min_score'],
            "is_active": updated['is_active'],
        })
    except Exception as e:
        conn.rollback()
        log.error(f"PUT /api/alerts error: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)


# ============================================================
# CHATBOT IA ENDPOINT
# ============================================================

LOU_SYSTEM_PROMPT = """Tu es Lou, un chasseur immobilier digital suisse. Tu es un loup sympathique et efficace.

TON ROLE: Aider les gens a definir leur recherche immobiliere en Suisse romande via une conversation naturelle.

REGLES STRICTES:
- Parle en francais, tutoie, sois chaleureux mais pro
- UNE question a la fois
- Si l'utilisateur donne plusieurs infos d'un coup, enregistre TOUT dans criteria
- Sois bref: 1-3 phrases max
- Emojis avec parcimonie
- Ne te presente PAS (le message de bienvenue est deja affiche)
- IMPORTANT: Ne redemande JAMAIS un critere deja collecte. Lis attentivement les [Critères déjà collectés] en debut de message utilisateur. Passe au critere SUIVANT manquant.
- Deduis les infos implicites: "acheter un appartement sur Cortaillod" = transaction:achat + type:appartement + zone:Cortaillod

CRITERES A COLLECTER (dans cet ordre de priorite):
1. Zone: ville(s), canton — SOUVENT donne dans le premier message
2. Type: appartement, maison, villa, studio, loft, attique, duplex — SOUVENT donne dans le premier message
3. Transaction: location ou achat — SOUVENT donne dans le premier message
4. Budget: min et/ou max CHF
5. Pieces: min et/ou max
6. Priorites: balcon, parking, vue, calme, transports, animaux, cave, jardin, ascenseur (optionnel)
7. Surface: m2 min (optionnel)

COMPORTEMENT:
- Apres chaque message, mets a jour TOUS les criteres deja connus dans "criteria"
- Quand tu as zone + type + transaction + budget + pieces, mets profile_ready: true
- Les suggestions doivent etre contextuelles (pas de suggestions de region si la region est deja connue)
- Ne force pas les criteres optionnels

REPONSE: JSON uniquement:
{"message":"texte","suggestions":["btn1","btn2"],"criteria":{"zones":[{"city":"X","canton":"XX","radius_km":3}],"property_types":["appartement"],"transaction":"achat","budget_min":null,"budget_max":900000,"currency":"CHF","rooms_min":3,"rooms_max":null,"surface_min":null,"priorities":[],"move_date":null},"profile_ready":false,"confirmed":false}

Cantons: VD, GE, NE, FR, VS, JU, BE, ZH, BS, TI, LU, SG"""

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
    message = data.get('message', '').strip()

    if not message:
        return jsonify({"error": "Message vide"}), 400

    # Limit message size to prevent abuse (max 2000 chars)
    if len(message) > 2000:
        return jsonify({"error": "Message trop long (2000 caracteres max)"}), 400

    # Authenticate: use JWT if present, else anonymous session
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    is_anonymous = False
    if token:
        try:
            token_data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            user_id = str(token_data['user_id'])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            # Invalid token — use anonymous session ID (not client-provided user_id)
            session_id = data.get('session_id', 'anon')
            user_id = f"anon-{session_id}"
            is_anonymous = True
    else:
        # No token — use anonymous session ID (not client-provided user_id)
        session_id = data.get('session_id', 'anon')
        user_id = f"anon-{session_id}"
        is_anonymous = True

    if not anthropic_client:
        return jsonify({
            "message": "Chatbot IA non configure. Ajoutez ANTHROPIC_API_KEY.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        })

    # Rate limiting
    if not _check_rate_limit(user_id, is_anonymous=is_anonymous):
        return jsonify({
            "message": "Trop de messages. Attends quelques secondes avant de reessayer.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        }), 429

    # Load history from DB
    history = _load_chat_history(user_id)

    # Extract already-collected criteria by merging ALL assistant messages (not just the last)
    collected = {}
    for h in history:
        if h['role'] == 'assistant':
            try:
                parsed = _parse_llm_json(h['content'])
                if parsed and parsed.get('criteria'):
                    c = parsed['criteria']
                    for k, v in c.items():
                        if v is not None and v != [] and v != '':
                            collected[k] = v
            except Exception:
                pass

    # Build messages — clean assistant history to just message text
    # If no history, inject the welcome message so Claude has context
    messages = []
    if not history:
        messages.append({"role": "assistant", "content": "Salut ! Je suis Lou, ton chasseur immobilier digital. Dis-moi ce que tu cherches et je me mets en chasse !"})
    for h in history:
        if h['role'] == 'assistant':
            try:
                parsed = _parse_llm_json(h['content'])
                if parsed and parsed.get('message'):
                    messages.append({"role": "assistant", "content": parsed['message']})
                else:
                    messages.append(h)
            except Exception:
                messages.append(h)
        else:
            messages.append(h)

    # Inject criteria reminder into user message so Claude knows what's already collected
    enriched_message = message
    if collected:
        reminder_parts = []
        if collected.get('zones'):
            cities = [z.get('city', '') for z in collected['zones'] if z.get('city')]
            if cities:
                reminder_parts.append(f"zones: {', '.join(cities)}")
        if collected.get('transaction'):
            reminder_parts.append(f"transaction: {collected['transaction']}")
        if collected.get('property_types'):
            reminder_parts.append(f"type: {', '.join(collected['property_types'])}")
        if collected.get('budget_max'):
            reminder_parts.append(f"budget max: {collected['budget_max']} CHF")
        if collected.get('budget_min'):
            reminder_parts.append(f"budget min: {collected['budget_min']} CHF")
        if collected.get('rooms_min'):
            reminder_parts.append(f"pieces min: {collected['rooms_min']}")
        if collected.get('rooms_max'):
            reminder_parts.append(f"pieces max: {collected['rooms_max']}")
        if collected.get('surface_min'):
            reminder_parts.append(f"surface min: {collected['surface_min']} m2")
        if collected.get('priorities'):
            reminder_parts.append(f"priorites: {', '.join(collected['priorities'])}")
        if reminder_parts:
            # Determine what's still missing
            missing = []
            if not collected.get('zones'):
                missing.append('zone')
            if not collected.get('transaction'):
                missing.append('transaction')
            if not collected.get('property_types'):
                missing.append('type de bien')
            if not collected.get('budget_max') and not collected.get('budget_min'):
                missing.append('budget')
            if not collected.get('rooms_min'):
                missing.append('nombre de pieces')
            missing_str = f" | Critères manquants: {', '.join(missing)}" if missing else " | Tous les critères essentiels sont collectés!"
            enriched_message = f"[Critères déjà collectés: {', '.join(reminder_parts)}{missing_str}]\n{message}"

    messages.append({"role": "user", "content": enriched_message})

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
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
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        try:
            token_data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            uid = str(token_data['user_id'])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            session_id = data.get('session_id', 'anon')
            uid = f"anon-{session_id}"
    else:
        session_id = data.get('session_id', 'anon')
        uid = f"anon-{session_id}"
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
    """Trigger scraping based on user's search profile zones.
       Runs in background thread to avoid Render's 30s timeout."""
    import threading

    data = request.get_json(silent=True) or {}
    city = data.get('city')
    transaction = data.get('transaction', 'location')
    user_id = request.user_id

    # If no city specified, use the user's profile zones
    if not city:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT sz.city FROM search_zones sz
                JOIN search_profiles sp ON sp.id = sz.profile_id
                WHERE sp.user_id = %s AND sp.is_active = TRUE
            """, (user_id,))
            rows = cur.fetchall()
            cities = [r['city'] for r in rows if r['city']]
            if not cities:
                return jsonify({"error": "Aucune zone configuree. Ajoutez des zones dans votre profil."}), 400

            # Also get transaction from profile
            cur.execute("""
                SELECT transaction FROM search_profiles
                WHERE user_id = %s AND is_active = TRUE
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            prof = cur.fetchone()
            if prof and prof['transaction']:
                transaction = prof['transaction']
        finally:
            cur.close()
            return_db(conn)
    else:
        cities = [city]

    # Run scraping + scoring in background thread to avoid 30s timeout
    def _bg_scrape(city_list, tx, uid):
        from scrapers import scrape_all, save_to_db
        from scoring_engine import score_property
        bg_conn = get_db()
        bg_cur = bg_conn.cursor()
        try:
            total_saved = 0
            for c in city_list:
                try:
                    listings = scrape_all(city=c, transaction=tx)
                    if listings:
                        saved = save_to_db(bg_conn, listings)
                        total_saved += (saved or 0)
                        log.info(f"User scrape: saved {saved} for {c} ({tx})")
                except Exception as e:
                    log.error(f"User scrape failed for {c}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass
            log.info(f"User scrape complete: {total_saved} saved for {len(city_list)} cities")

            # Now score all properties for this user's profile
            try:
                bg_cur.execute("""
                    SELECT * FROM search_profiles
                    WHERE user_id = %s AND is_active = TRUE
                    ORDER BY created_at DESC LIMIT 1
                """, (uid,))
                profile = bg_cur.fetchone()
                if profile:
                    profile = dict(profile)
                    bg_cur.execute("SELECT * FROM search_zones WHERE profile_id = %s", (profile['id'],))
                    zones = [dict(z) for z in bg_cur.fetchall()]

                    score_query = "SELECT * FROM properties WHERE is_active = TRUE"
                    score_params = []
                    if profile.get('transaction'):
                        score_query += " AND transaction = %s"
                        score_params.append(profile['transaction'])
                    if profile.get('budget_max'):
                        score_query += " AND (price IS NULL OR price <= %s)"
                        score_params.append(int(profile['budget_max'] * 1.3))
                    bg_cur.execute(score_query, score_params)
                    properties = bg_cur.fetchall()

                    scored = 0
                    for prop in properties:
                        prop = dict(prop)
                        result = score_property(prop, profile, zones)
                        bg_cur.execute("""
                            INSERT INTO scored_properties
                                (property_id, profile_id, user_id, total_score, grade,
                                 score_zone, score_budget, score_type, score_surface,
                                 score_equipment, score_freshness, distance_km)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (property_id, profile_id)
                            DO UPDATE SET
                                total_score = EXCLUDED.total_score, grade = EXCLUDED.grade,
                                score_zone = EXCLUDED.score_zone, score_budget = EXCLUDED.score_budget,
                                score_type = EXCLUDED.score_type, score_surface = EXCLUDED.score_surface,
                                score_equipment = EXCLUDED.score_equipment, score_freshness = EXCLUDED.score_freshness,
                                distance_km = EXCLUDED.distance_km, scored_at = NOW()
                        """, (
                            prop['id'], profile['id'], uid,
                            result['total_score'], result['grade'],
                            result['score_zone'], result['score_budget'],
                            result['score_type'], result['score_surface'],
                            result['score_equipment'], result['score_freshness'],
                            result['distance_km']
                        ))
                        scored += 1
                    bg_conn.commit()
                    log.info(f"User scrape scoring: {scored} properties scored for user {uid}")
            except Exception as e:
                log.error(f"User scrape scoring error: {e}", exc_info=True)
                try:
                    bg_conn.rollback()
                except Exception:
                    pass
        except Exception as e:
            log.error(f"User scrape bg error: {e}", exc_info=True)
        finally:
            bg_cur.close()
            return_db(bg_conn)

    thread = threading.Thread(target=_bg_scrape, args=(cities, transaction, user_id))
    thread.daemon = True
    thread.start()

    return jsonify({
        "ok": True,
        "message": f"Scraping lance en arriere-plan pour {len(cities)} ville(s)",
        "cities": cities
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
    if not isinstance(listings, list) or len(listings) > 500:
        return jsonify({"error": "Listings invalides (max 500)"}), 400

    # Validate each listing has required fields
    required_fields = ['title', 'source', 'source_url']
    for i, listing in enumerate(listings):
        if not isinstance(listing, dict):
            return jsonify({"error": f"Listing {i} invalide"}), 400
        for field in required_fields:
            if not listing.get(field):
                return jsonify({"error": f"Listing {i}: champ '{field}' requis"}), 400

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
    from scoring_engine import score_property
    conn = get_db()
    cur = conn.cursor()
    try:

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

        # Get active properties matching user's transaction type + canton pre-filter
        score_query = "SELECT * FROM properties WHERE is_active = TRUE"
        score_params = []
        if profile.get('transaction'):
            score_query += " AND transaction = %s"
            score_params.append(profile['transaction'])
        zone_cantons = [z.get('canton', '').upper() for z in zones if z.get('canton')]
        if zone_cantons:
            placeholders = ','.join(['%s'] * len(zone_cantons))
            score_query += f" AND (canton IN ({placeholders}) OR canton IS NULL OR canton = '')"
            score_params.extend(zone_cantons)
        if profile.get('budget_max'):
            score_query += " AND (price IS NULL OR price <= %s)"
            score_params.append(int(profile['budget_max'] * 1.3))
        cur.execute(score_query, score_params)
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
        return jsonify({"ok": True, "scored": scored, "profile_id": profile['id']})
    except Exception as e:
        log.error(f"Score error: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erreur lors du scoring. Verifiez votre profil."}), 500
    finally:
        cur.close()
        return_db(conn)


def _require_cron_secret():
    """Check that the request has the correct cron secret (via header or query param)."""
    secret = request.headers.get('X-Cron-Secret', '') or request.args.get('secret', '')
    if not CRON_SECRET:
        log.warning("CRON_SECRET not configured — cron endpoints are disabled")
        return False
    if secret != CRON_SECRET:
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
            "key_from_scrapers_set": bool(SCRAPINGBEE_KEY),
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
            log.error(f"httpbin test error: {e}")
            results["httpbin_test"] = {"error": type(e).__name__}

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
            results["homegate_scraper"] = {"error": type(e).__name__}

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
            log.error(f"{endpoint_name} test error: {e}")
            results[endpoint_name] = {"error": type(e).__name__}

    # 2. Homegate API
    try:
        r = req.get(f'https://www.homegate.ch/api/search/rent',
                     headers={**headers, 'Referer': 'https://www.homegate.ch/'},
                     params={'loc': city, 'ag': 3, 'o': 'dateCreated-desc'},
                     timeout=15)
        body = r.text[:500]
        results['Homegate_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        log.error(f"Homegate API test error: {e}")
        results['Homegate_API'] = {"error": type(e).__name__}

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
        log.error(f"Homegate Page test error: {e}")
        results['Homegate_Page'] = {"error": type(e).__name__}

    # 4. ImmoScout24
    try:
        slug = city.lower().replace(' ', '-')
        r = req.get(f'https://www.immoscout24.ch/en/real-estate/rent/city-{slug}',
                     headers=headers, timeout=15)
        has_initial = '__INITIAL_STATE__' in r.text
        results['ImmoScout24'] = {"http": r.status_code, "has_INITIAL_STATE": has_initial, "page_size": len(r.text)}
    except Exception as e:
        log.error(f"ImmoScout24 test error: {e}")
        results['ImmoScout24'] = {"error": type(e).__name__}

    # 5. Comparis API
    try:
        payload = {'DealType': 10, 'Keyword': city, 'LocationSearchString': city, 'Sort': 4, 'Page': 1, 'PageSize': 3, 'RootPropertyTypes': [1]}
        r = req.post('https://api.comparis.ch/realestate/v1/search/list',
                      headers={**headers, 'Content-Type': 'application/json'},
                      json=payload, timeout=15)
        body = r.text[:500]
        results['Comparis_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        log.error(f"Comparis API test error: {e}")
        results['Comparis_API'] = {"error": type(e).__name__}

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
    """Trigger scraping via cron. Protected by CRON_SECRET.
       Runs in background thread to avoid Render's 30s request timeout."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403

    import threading

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
        profiles = [dict(p) for p in cur.fetchall()]

        if not profiles:
            return jsonify({"ok": True, "message": "No active profiles"})

        # Collect unique city + transaction combos from user profiles
        # For each canton with active users, scrape its major cities
        CANTON_CITIES = {
            'NE': [
                'Neuchâtel', 'La Chaux-de-Fonds', 'Le Locle',
                'Peseux', 'Boudry', 'Cortaillod', 'Colombier',
                'Val-de-Travers', 'Milvignes', 'Val-de-Ruz',
                'Hauterive', 'Saint-Blaise', 'Corcelles-Cormondrèche',
                'La Tène', 'Le Landeron', 'Bevaix', 'Marin-Epagnier',
                'Fleurier', 'Couvet', 'Cernier', 'Fontainemelon',
            ],
            'VD': [
                'Lausanne', 'Montreux', 'Vevey', 'Nyon', 'Morges',
                'Yverdon-les-Bains', 'Renens', 'Prilly', 'Pully',
                'Ecublens', 'Lutry', 'Savigny',
            ],
            'GE': [
                'Genève', 'Carouge', 'Meyrin', 'Lancy',
                'Vernier', 'Onex', 'Thônex',
            ],
            'VS': ['Sion', 'Sierre', 'Martigny', 'Monthey'],
            'FR': ['Fribourg', 'Bulle'],
            'BE': ['Berne', 'Bienne'],
            'JU': ['Delémont', 'Porrentruy'],
        }

        scrape_targets = set()
        transactions_needed = set()
        cantons_needed = set()
        for p in profiles:
            zones = p.get('zones', [])
            tx = p.get('transaction', 'location')
            transactions_needed.add(tx)
            if zones:
                for z in zones:
                    if isinstance(z, dict) and z.get('city'):
                        city_norm = z['city'].strip().title()
                        scrape_targets.add((city_norm, tx))
                        # Track canton so we scrape all cities in that canton
                        ct = (z.get('canton') or '').upper()
                        if ct:
                            cantons_needed.add(ct)
                        else:
                            # Lookup canton from city
                            from scrapers import CITY_CANTONS
                            ct = CITY_CANTONS.get(city_norm.lower(), '')
                            if ct:
                                cantons_needed.add(ct.upper())

        # Add all cities from cantons with active users
        for tx in transactions_needed:
            for canton in cantons_needed:
                for city in CANTON_CITIES.get(canton, []):
                    scrape_targets.add((city, tx))

        targets_list = [{"city": c, "transaction": t} for c, t in scrape_targets]

    finally:
        cur.close()
        return_db(conn)

    # Run scraping + scoring in background thread
    def _background_scrape(targets, profiles_data):
        from scrapers import scrape_all, save_to_db
        from scoring_engine import score_property

        bg_conn = get_db()
        bg_cur = bg_conn.cursor()
        try:
            total_saved = 0
            for city, transaction in targets:
                try:
                    listings = scrape_all(city=city, transaction=transaction)
                    if listings:
                        saved = save_to_db(bg_conn, listings)
                        total_saved += (saved or 0)
                        log.info(f"Cron: saved {saved} for {city} ({transaction})")
                except Exception as e:
                    log.error(f"Cron: scrape failed for {city}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass

            # Score all profiles
            scored_total = 0
            for p in profiles_data:
                try:
                    profile_id = p['id']
                    user_id = p['user_id']
                    zones_data = [dict(z) for z in (p.get('zones') or []) if isinstance(z, dict) and z.get('city')]

                    bg_cur.execute("SELECT * FROM properties WHERE is_active = TRUE AND transaction = %s",
                                (p.get('transaction', 'location'),))
                    properties = bg_cur.fetchall()

                    for prop in properties:
                        prop = dict(prop)
                        result = score_property(prop, p, zones_data)
                        bg_cur.execute("""
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

                    bg_conn.commit()
                except Exception as e:
                    log.error(f"Cron: scoring failed for profile {p.get('id')}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass

            # Deactivate old listings
            bg_cur.execute("""
                UPDATE properties SET is_active = FALSE
                WHERE scraped_at < NOW() - INTERVAL '30 days' AND is_active = TRUE
            """)
            bg_conn.commit()

            log.info(f"Cron complete: saved={total_saved}, scored={scored_total}")

        except Exception as e:
            log.error(f"Cron background error: {e}", exc_info=True)
            try:
                bg_conn.rollback()
            except Exception:
                pass
        finally:
            bg_cur.close()
            return_db(bg_conn)

    thread = threading.Thread(target=_background_scrape, args=(scrape_targets, profiles))
    thread.daemon = True
    thread.start()

    return jsonify({
        "ok": True,
        "message": f"Scraping lancé en arrière-plan pour {len(scrape_targets)} ville(s)",
        "targets": targets_list
    })


# ============================================================
# STARTUP
# ============================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/manifest.json')
def manifest_json():
    return send_from_directory('static', 'manifest.json')


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
