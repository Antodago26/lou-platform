"""Bon Home — Static pages + health endpoint Blueprint."""
import os
import time
import logging

from flask import Blueprint, jsonify, send_from_directory, request

from plans import public_catalog
from db import get_db, return_db, pool_stats

log = logging.getLogger('lou-app')

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    return send_from_directory('static', 'index.html')


@pages_bp.route('/manifest.json')
def manifest_json():
    return send_from_directory('static', 'manifest.json')


@pages_bp.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'dashboard.html')


@pages_bp.route('/privacy')
def privacy():
    return send_from_directory('static', 'privacy.html')


@pages_bp.route('/terms')
def terms():
    return send_from_directory('static', 'terms.html')


@pages_bp.route('/profil')
def profil():
    return send_from_directory('static', 'profil.html')


@pages_bp.route('/faq')
def faq():
    return send_from_directory('static', 'faq.html')


@pages_bp.route('/pricing')
def pricing():
    return send_from_directory('static', 'pricing.html')


@pages_bp.route('/health')
def health():
    """
    Deep health check (v6.3.3 O2). Teste DB (SELECT 1) + expose stats pool.
    Renvoie 503 si DB down pour que Render health checks / load balancer
    sortent l'instance du pool. Pas d'auth (public) — on ne leak rien de
    sensible (pas de version, pas de hostnames, pas de secrets).
    """
    db_status = "ok"
    db_latency_ms = None
    conn = None
    t0 = time.time()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        db_latency_ms = round((time.time() - t0) * 1000, 1)
    except Exception as e:
        db_status = "down"
        log.error(f"/health DB check failed: {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn)
            except Exception:
                pass

    overall = "ok" if db_status == "ok" else "degraded"
    payload = {
        "status": overall,
        "db": db_status,
        "db_latency_ms": db_latency_ms,
        "pool": pool_stats(),
    }
    return jsonify(payload), (200 if db_status == "ok" else 503)


@pages_bp.route('/debug/sentry-ping')
def debug_sentry_ping():
    """
    TEMPORAIRE — à retirer après validation Sentry en prod.
    Protégé par un token simple pour éviter le trigger par bots.
    Usage : GET /debug/sentry-ping?token=<SENTRY_PING_TOKEN>
    """
    expected = os.environ.get('SENTRY_PING_TOKEN', '')
    if not expected or request.args.get('token') != expected:
        return jsonify({"error": "forbidden"}), 403
    try:
        raise RuntimeError("Sentry ping — test event, safe to ignore")
    except Exception:
        log.exception("Sentry ping triggered")
    return jsonify({"ok": True, "sent": "check Sentry dashboard"}), 200


@pages_bp.route('/api/plans')
def api_plans():
    """Public pricing catalog (C3.4)."""
    return jsonify(public_catalog())
