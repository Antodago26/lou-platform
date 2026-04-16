"""
Bon Home — Authentication & account Blueprint.
Exports token_required, admin_required, plan_feature decorators so other
blueprints can import them without depending on the full app module.
"""
import os
import sys
import re
import logging
import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone

import jwt
import bcrypt
import psycopg2
import requests as http_requests
from flask import Blueprint, jsonify, request

from db import get_db, return_db
from helpers import (
    validate_json, SignupRequest, LoginRequest, ProfileUpdateRequest,
    parse_budget, parse_rooms, _HAS_PYDANTIC,
)

log = logging.getLogger('lou-app')

# --- Secrets & config (read at import time, same pattern as legacy app.py) ---
JWT_SECRET = os.environ.get('JWT_SECRET', '')
if not JWT_SECRET:
    if os.environ.get('FLASK_ENV') == 'production':
        log.error("FATAL: JWT_SECRET must be set in production! Refusing to start.")
        sys.exit(1)
    log.warning("JWT_SECRET not set! Using random secret (tokens won't persist across restarts). NEVER use this in production.")
    JWT_SECRET = secrets.token_hex(32)

HCAPTCHA_SECRET = os.environ.get('HCAPTCHA_SECRET', '')
HCAPTCHA_SITEKEY = os.environ.get('HCAPTCHA_SITEKEY', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')


auth_bp = Blueprint('auth', __name__)


# ============================================================
# CORE HELPERS
# ============================================================

def make_token(user_id):
    """Generate a JWT token."""
    return jwt.encode(
        {'user_id': user_id, 'exp': datetime.now(timezone.utc) + timedelta(days=7)},
        JWT_SECRET, algorithm='HS256'
    )


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
            return jsonify({"error": "Admin non configuré"}), 403

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("SELECT email FROM users WHERE id = %s", (request.user_id,))
            user = cur.fetchone()
            if not user or user['email'] != ADMIN_EMAIL.lower().strip():
                return jsonify({"error": "Accès refusé"}), 403
        finally:
            cur.close()
            return_db(conn)
        return f(*args, **kwargs)
    return decorated


def _get_user_plan(user_id):
    """Fetch the plan name for a user. Defaults to 'free' if missing."""
    if not user_id:
        return 'free'
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT plan FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            return 'free'
        return (row.get('plan') or 'free') if isinstance(row, dict) else (row['plan'] or 'free')
    except Exception:
        return 'free'
    finally:
        cur.close()
        return_db(conn)


def plan_feature(feature):
    """Check if the user's plan authorizes a given feature.
    No-op when PRICING_ENABLED is False — limits stay prepared but inactive.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            try:
                from plans import is_feature_allowed, PRICING_ENABLED
            except Exception:
                return f(*args, **kwargs)
            if not PRICING_ENABLED:
                return f(*args, **kwargs)
            user_plan = _get_user_plan(getattr(request, 'user_id', None))
            if not is_feature_allowed(user_plan, feature):
                return jsonify({
                    "error": "Fonctionnalité réservée aux abonnés",
                    "required_feature": feature,
                    "current_plan": user_plan,
                    "upgrade_url": "/pricing"
                }), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def verify_hcaptcha(token):
    """Verify hCaptcha token. Fail-closed when HCAPTCHA_SECRET is configured."""
    if not HCAPTCHA_SECRET:
        return True
    if not token:
        log.warning("hCaptcha: missing token")
        return False
    try:
        resp = http_requests.post('https://api.hcaptcha.com/siteverify', data={
            'secret': HCAPTCHA_SECRET,
            'response': token
        }, timeout=5)
        success = resp.json().get('success', False)
        if not success:
            log.warning(f"hCaptcha verification failed: {resp.json()}")
        return success
    except Exception as e:
        log.error(f"hCaptcha verification error: {e}")
        return False


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
                <p><strong>Nom :</strong> {user_name or "Non renseigné"}</p>
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


# ============================================================
# PUBLIC CONFIG (kept here since it exposes HCAPTCHA_SITEKEY)
# ============================================================

@auth_bp.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'hcaptcha_sitekey': HCAPTCHA_SITEKEY or ''
    })


# ============================================================
# SIGNUP / LOGIN
# ============================================================

@auth_bp.route('/api/signup', methods=['POST'])
def signup():
    req, err = validate_json(SignupRequest)
    if err:
        return err
    email = getattr(req, 'email', '') or ''
    password = getattr(req, 'password', '') or ''
    name = getattr(req, 'name', '') or ''
    criteria = getattr(req, 'criteria', {}) or {}
    captcha_token = getattr(req, 'captcha_token', '') or ''
    if not _HAS_PYDANTIC:
        email = email.strip().lower()
        if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            return jsonify({"error": "Email invalide"}), 400
        if len(password) < 8:
            return jsonify({"error": "Mot de passe trop court (8 car. min)"}), 400

    if HCAPTCHA_SECRET and not verify_hcaptcha(captcha_token):
        return jsonify({"error": "Vérification CAPTCHA échouée"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING id, email, name",
            (email, pw_hash, name)
        )
        user = dict(cur.fetchone())

        if criteria:
            prop_types = criteria.get('property_types') or [criteria.get('property_type', 'appartement')]
            if isinstance(prop_types, str):
                prop_types = [prop_types]
            transaction = criteria.get('transaction') or criteria.get('transaction_type', 'location')
            budget_max = criteria.get('budget_max') or parse_budget(criteria.get('budget', ''))
            rooms_min = criteria.get('rooms_min') or parse_rooms(criteria.get('rooms', ''))

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
                city = criteria.get('city', '')
                canton = criteria.get('canton', '')
                if city:
                    cur.execute("""
                        INSERT INTO search_zones (profile_id, city, canton, radius_km)
                        VALUES (%s, %s, %s, %s)
                    """, (profile_id, city, canton, 3.0))

        conn.commit()
        token = make_token(user['id'])

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


@auth_bp.route('/api/login', methods=['POST'])
def login():
    req, err = validate_json(LoginRequest)
    if err:
        return err
    email = getattr(req, 'email', '') or ''
    password = getattr(req, 'password', '') or ''
    if not _HAS_PYDANTIC:
        email = email.strip().lower()

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
# PROFILE (user's own search criteria)
# ============================================================

@auth_bp.route('/api/profile', methods=['GET'])
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


@auth_bp.route('/api/profile', methods=['PUT'])
@token_required
def update_profile():
    _req, _err = validate_json(ProfileUpdateRequest)
    if _err:
        return _err
    data = request.json or {}
    # C3.3 — custom_weights is a premium feature. If the payload ships weights
    # and the user isn't on a plan that allows it, strip them and use defaults.
    # No-op while PRICING_ENABLED is False (is_feature_allowed returns True).
    if data.get('weights'):
        try:
            from plans import is_feature_allowed
            user_plan = _get_user_plan(request.user_id)
            if not is_feature_allowed(user_plan, 'custom_weights'):
                data = dict(data)
                data.pop('weights', None)
        except Exception:
            pass
    conn = get_db()
    cur = conn.cursor()
    try:
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

        zones = data.get('zones', [])
        # Compare old zones to new zones to avoid unnecessary re-scraping
        old_zone_cities = set()
        if zones:
            cur.execute("SELECT city FROM search_zones WHERE profile_id = %s", (profile_id,))
            old_zone_cities = {r['city'].lower().strip() for r in cur.fetchall() if r['city']}
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

        # Background scrape + score: only scrape NEW cities (not already in DB).
        # Re-scoring always runs (it's free), but scraping costs ScrapingBee credits.
        new_zone_cities = {z.get('city', '').lower().strip() for z in zones if z.get('city')}
        cities_to_scrape = [z.get('city') for z in zones if z.get('city') and z.get('city', '').lower().strip() not in old_zone_cities]
        all_cities = [z.get('city') for z in zones if z.get('city')]
        transaction = data.get('transaction', 'location')
        if all_cities:
            import threading
            def _bg_scrape_and_score(city_list, scrape_list, tx, pid, uid):
                from scrapers import scrape_all, save_to_db
                from scoring_engine import score_property
                bg_conn = get_db()
                bg_cur = bg_conn.cursor()
                try:
                    # Only scrape cities that are genuinely NEW (not already in the profile)
                    if scrape_list:
                        log.info(f"Profile update: scraping {len(scrape_list)} NEW cities: {scrape_list}")
                    else:
                        log.info(f"Profile update: zones unchanged, skipping scrape (score-only)")
                    for city in scrape_list:
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
                        params.append(int(float(profile['budget_max']) * 1.3))
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

            thread = threading.Thread(target=_bg_scrape_and_score, args=(all_cities, cities_to_scrape, transaction, profile_id, request.user_id))
            thread.daemon = True
            thread.start()

        return jsonify({"ok": True, "profile_id": profile_id})
    except Exception as e:
        conn.rollback()
        log.error(f"Profile update error: {e}")
        return jsonify({"error": "Erreur serveur lors de la mise à jour du profil"}), 500
    finally:
        cur.close()
        return_db(conn)
