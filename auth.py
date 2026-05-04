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
from rate_limit import client_ip, check_rate_limit, rate_limited_response

# Brute-force throttles (audit C2, 2026-05). Per-worker in-memory — see
# rate_limit.py for the Redis upgrade TODO.
_LOGIN_PER_IP_MIN = 5
_LOGIN_PER_IP_HOUR = 30
_LOGIN_PER_EMAIL_MIN = 5
_LOGIN_PER_EMAIL_HOUR = 20
_SIGNUP_PER_IP_MIN = 3
_SIGNUP_PER_IP_HOUR = 10

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

# v6.3.3 (S5) : log.error visible en prod si un secret secondaire est
# absent. Pas fail-fast car ils sont non-critiques (le serveur peut
# démarrer sans), mais on veut que Sentry capture l'event au boot pour
# qu'il ne reste pas silencieux pendant des semaines.
if os.environ.get('FLASK_ENV') == 'production':
    _missing_prod_secrets = []
    if not HCAPTCHA_SECRET:
        _missing_prod_secrets.append('HCAPTCHA_SECRET (signup abuse risk)')
    if not RESEND_API_KEY:
        _missing_prod_secrets.append('RESEND_API_KEY (email alerts disabled)')
    if not ADMIN_EMAIL:
        _missing_prod_secrets.append('ADMIN_EMAIL (scraper alerts lost)')
    if _missing_prod_secrets:
        log.error(
            f"Production secrets missing (server still starting): {', '.join(_missing_prod_secrets)}"
        )

# v6.3 security fix: bcrypt silently truncates passwords to 72 bytes.
# Without this guard, two passwords that share the first 72 UTF-8 bytes
# collide — anyone knowing a 72-byte prefix authenticates as the user.
MAX_PASSWORD_BYTES = 72


def _password_too_long(password: str) -> bool:
    """Return True iff the password (UTF-8 encoded) exceeds bcrypt's 72-byte ceiling."""
    try:
        return len((password or '').encode('utf-8')) > MAX_PASSWORD_BYTES
    except Exception:
        return True


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


def _decode_jwt_or_401(token):
    """Returns (user_id, error_response). error_response is None on success."""
    if not token:
        return None, (jsonify({"error": "Token manquant"}), 401)
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return data['user_id'], None
    except jwt.ExpiredSignatureError:
        return None, (jsonify({"error": "Token expiré"}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({"error": "Token invalide"}), 401)


def token_required(f):
    """JWT authentication decorator. Header-only — see audit H1 (2026-05-04):
    accepting ?token= leaks JWTs into Cloudflare/Render logs, browser history,
    and Referer. For download endpoints that need the token in the URL (e.g.
    window.open for CSV export), use token_required_query_ok instead."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        user_id, err = _decode_jwt_or_401(token)
        if err:
            return err
        request.user_id = user_id
        return f(*args, **kwargs)
    return decorated


def token_required_query_ok(f):
    """JWT decorator that accepts ?token= in the query string. Use ONLY for
    GET endpoints that must be opened via window.open / direct browser URL
    (e.g. CSV/PDF downloads). Every other endpoint should use token_required."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '') \
            or request.args.get('token', '')
        user_id, err = _decode_jwt_or_401(token)
        if err:
            return err
        request.user_id = user_id
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
    if not check_rate_limit(f"signup:ip:{client_ip()}", _SIGNUP_PER_IP_MIN, _SIGNUP_PER_IP_HOUR):
        return rate_limited_response()
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
        if _password_too_long(password):
            return jsonify({"error": f"Mot de passe trop long (max {MAX_PASSWORD_BYTES} octets UTF-8)"}), 400

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
            # v6.2 Erreur #3 fix: resolve lat/lng on signup too, same reason
            # as in PUT /api/profile (see auth.py:~460).
            from scoring_engine import resolve_zone_coords
            if zones and isinstance(zones, list):
                for z in zones:
                    city = z.get('city', '')
                    canton = z.get('canton', '')
                    radius = z.get('radius_km', 3.0)
                    if city:
                        resolve_zone_coords(z, conn=conn)
                        cur.execute("""
                            INSERT INTO search_zones (profile_id, city, canton, latitude, longitude, radius_km)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (profile_id, city, canton, z.get('latitude'), z.get('longitude'), radius))
            else:
                city = criteria.get('city', '')
                canton = criteria.get('canton', '')
                if city:
                    zone_tmp = {'city': city, 'canton': canton}
                    resolve_zone_coords(zone_tmp, conn=conn)
                    cur.execute("""
                        INSERT INTO search_zones (profile_id, city, canton, latitude, longitude, radius_km)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (profile_id, city, canton, zone_tmp.get('latitude'), zone_tmp.get('longitude'), 3.0))

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
    # IP throttle first — caps credential stuffing from one source.
    if not check_rate_limit(f"login:ip:{client_ip()}", _LOGIN_PER_IP_MIN, _LOGIN_PER_IP_HOUR):
        return rate_limited_response()

    req, err = validate_json(LoginRequest)
    if err:
        return err
    email = getattr(req, 'email', '') or ''
    password = getattr(req, 'password', '') or ''
    if not _HAS_PYDANTIC:
        email = email.strip().lower()
    else:
        email = email.strip().lower()

    # Per-email throttle — caps targeted brute force on one account from
    # rotating IPs. Recorded BEFORE bcrypt so the timing is consistent
    # whether the email exists or not.
    if email and not check_rate_limit(f"login:email:{email}", _LOGIN_PER_EMAIL_MIN, _LOGIN_PER_EMAIL_HOUR):
        return rate_limited_response()

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE email = %s AND is_active = TRUE", (email,))
        user = cur.fetchone()
        # v6.3: reject > 72 bytes with the generic auth error (no oracle) — bcrypt
        # would otherwise truncate and potentially accept a crafted prefix.
        if _password_too_long(password) or not user or not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
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
                'radius_km', sz.radius_km, 'latitude', sz.latitude, 'longitude', sz.longitude,
                'postal_code', sz.postal_code
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
            # v6.3.2 Bug #2: validate all zones resolve to GPS BEFORE any delete,
            # so a bad payload doesn't wipe out existing zones.
            from scoring_engine import resolve_zone_coords
            for z in zones:
                resolve_zone_coords(z, conn=conn)
                if not z.get('latitude') or not z.get('longitude'):
                    conn.rollback()
                    return jsonify({
                        "error": f"Commune non reconnue : « {z.get('city', '?')} ». Veuillez sélectionner une suggestion dans la liste.",
                        "unresolved_zone": z.get('city', '')
                    }), 400
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
                from scoring_engine import upsert_scored_properties
                # v6.3.2 Bug #5: lors d'un redéploiement, le pool de connexions
                # peut être fermé pendant que ce thread démarre. get_db() ou
                # .cursor() throw InterfaceError/OperationalError et le thread
                # mourait silencieusement. On catch explicitement et on bail
                # proprement : le user garde son profil, juste pas de rescore
                # immédiat (le prochain POST /api/score le fera).
                import psycopg2
                try:
                    bg_conn = get_db()
                    bg_cur = bg_conn.cursor()
                except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                    log.warning(f"bg_scrape_and_score: DB pool unavailable ({e}), "
                                f"skipping rescore for profile {pid} (likely deploy restart)")
                    return
                except Exception as e:
                    log.error(f"bg_scrape_and_score: get_db failed: {e}", exc_info=True)
                    return
                conn_broken = False
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
                    scored = upsert_scored_properties(bg_cur, properties, profile, zones_data, user_id=uid)
                    bg_conn.commit()
                    log.info(f"Profile update scoring: {scored} properties scored for profile {pid}")
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    # Conn TLS pourrie (SSL bad record mac, etc.) → force close
                    # pour ne pas empoisonner le pool.
                    conn_broken = True
                    log.error(f"Profile update bg DB transport error: {e}", exc_info=True)
                except Exception as e:
                    log.error(f"Profile update bg error: {e}", exc_info=True)
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass
                finally:
                    try: bg_cur.close()
                    except Exception: pass
                    try: return_db(bg_conn, close=conn_broken)
                    except Exception: pass

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
