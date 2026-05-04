"""Bon Home — Static pages + health endpoint Blueprint."""
import time
import logging
from datetime import date

from flask import Blueprint, jsonify, send_from_directory, render_template, abort, Response

from plans import public_catalog
from db import get_db, return_db, pool_stats

log = logging.getLogger('lou-app')

pages_bp = Blueprint('pages', __name__)

# Whitelist of templates served via the public router. Adding a new public
# page = adding it here. Anything else returns 404.
_PUBLIC_PAGES = {
    'dashboard': 'dashboard.html',
    'privacy':   'privacy.html',
    'terms':     'terms.html',
    'profil':    'profil.html',
    'faq':       'faq.html',
    'pricing':   'pricing.html',
}


@pages_bp.route('/')
def index():
    return render_template('index.html')


@pages_bp.route('/manifest.json')
def manifest_json():
    return send_from_directory('static', 'manifest.json')


@pages_bp.route('/robots.txt')
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard\n"
        "Disallow: /profil\n"
        "\n"
        "Sitemap: https://bonhome.ch/sitemap.xml\n"
    )
    return Response(body, mimetype='text/plain')


# Public URLs to expose in the sitemap. Order matters less than completeness.
_SITEMAP_URLS = ['/', '/pricing', '/faq', '/privacy', '/terms']


@pages_bp.route('/sitemap.xml')
def sitemap_xml():
    today = date.today().isoformat()
    urls = ''.join(
        f'<url><loc>https://bonhome.ch{path}</loc><lastmod>{today}</lastmod></url>'
        for path in _SITEMAP_URLS
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'{urls}'
        '</urlset>'
    )
    return Response(body, mimetype='application/xml')


@pages_bp.route('/<page>')
def public_page(page):
    template = _PUBLIC_PAGES.get(page)
    if not template:
        abort(404)
    return render_template(template)


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


@pages_bp.route('/api/plans')
def api_plans():
    """Public pricing catalog (C3.4)."""
    return jsonify(public_catalog())
