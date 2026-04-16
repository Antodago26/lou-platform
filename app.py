"""
Bon Home — Backend V2 (Flask app factory).

Environment variables (production):
  DATABASE_URL=postgresql://...
  ANTHROPIC_API_KEY=sk-ant-...
  JWT_SECRET=your-secret-key
  FLASK_ENV=production
  ALLOWED_ORIGINS=https://bonhome.ch,https://www.bonhome.ch
  HCAPTCHA_SECRET=..., HCAPTCHA_SITEKEY=..., RESEND_API_KEY=..., CRON_SECRET=...
  POOL_MIN=2, POOL_MAX=10, POOL_DISABLE=0
  CLAUDE_CHAT_MODEL=claude-opus-4-5-20250929

Entry points:
  gunicorn app:app          (production)
  python app.py             (local dev)
"""
import os
import logging

from flask import Flask, request
from flask_cors import CORS

from db import get_db, return_db, init_db

# Blueprint modules
from auth import auth_bp
from routes_properties import properties_bp
from routes_chat import chat_bp
from routes_admin import admin_bp
from routes_alerts import alerts_bp
from routes_scraping import scraping_bp
from routes_pages import pages_bp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-app')


# ============================================================
# MIGRATIONS (idempotent)
# ============================================================
def _run_migrations():
    """Run schema migrations on startup (idempotent)."""
    try:
        db = get_db()
        cur = db.cursor()
        # properties.first_seen_at
        cur.execute("""
            ALTER TABLE properties ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT NOW()
        """)
        # price_history table
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_hist_prop ON price_history(property_id, detected_at DESC)"
        )
        # Backfill first_seen_at from scraped_at for existing rows
        cur.execute("UPDATE properties SET first_seen_at = scraped_at WHERE first_seen_at IS NULL")

        # Pricing / plan infrastructure (C3.6)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(100)")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS chat_messages_today INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS chat_messages_date DATE")

        # v6.1 Bug 3 fix: rewrite IS24 URLs to /fr/d/{listingId}.
        # Previously we fell back to /real-estate/buy/city-X?pn=1 (a SEARCH page),
        # which was bad UX — the user had to find the listing again manually.
        # /fr/d/{id} redirects to the canonical SEO URL when the id is valid.
        # external_id format in our DB is "is24-{lid}" (scrapers.py line 819/929),
        # so we strip the "is24-" prefix to get the raw listing id.
        cur.execute("""
            UPDATE properties p
            SET source_url = 'https://www.immoscout24.ch/fr/d/' ||
                REPLACE(p.external_id, 'is24-', '')
            WHERE p.source = 'ImmoScout24'
              AND p.external_id LIKE 'is24-%'
              AND (p.source_url LIKE '%/en/d/%'
                   OR p.source_url LIKE '%/real-estate/%/detail/%'
                   OR p.source_url LIKE '%/city-%'
                   OR p.source_url LIKE '%?pn=%')
        """)
        cur.execute("""
            UPDATE property_sources ps
            SET source_url = 'https://www.immoscout24.ch/fr/d/' ||
                REPLACE(
                    (SELECT external_id FROM properties WHERE id = ps.property_id),
                    'is24-', ''
                )
            WHERE ps.source = 'ImmoScout24'
              AND EXISTS (
                  SELECT 1 FROM properties p
                  WHERE p.id = ps.property_id
                    AND p.external_id LIKE 'is24-%'
              )
              AND (ps.source_url LIKE '%/en/d/%'
                   OR ps.source_url LIKE '%/real-estate/%/detail/%'
                   OR ps.source_url LIKE '%/city-%'
                   OR ps.source_url LIKE '%?pn=%')
        """)
        # Normalize any remaining /en/d/{id} to /fr/d/{id}
        cur.execute("""
            UPDATE properties SET source_url = REPLACE(source_url, '/en/d/', '/fr/d/')
            WHERE source = 'ImmoScout24' AND source_url LIKE '%/en/d/%'
        """)
        cur.execute("""
            UPDATE property_sources SET source_url = REPLACE(source_url, '/en/d/', '/fr/d/')
            WHERE source = 'ImmoScout24' AND source_url LIKE '%/en/d/%'
        """)

        # v6.2 Erreur #3 fix: backfill lat/lng on existing zones that were
        # saved before resolve_zone_coords() was wired into the save path.
        # Without GPS, score_zone() can't compute distances (haversine needs
        # both endpoints) and falls back to canton match → stale/wrong scores.
        try:
            from scoring_engine import resolve_zone_coords
            cur.execute("""
                SELECT id, city, canton FROM search_zones
                WHERE latitude IS NULL OR longitude IS NULL
            """)
            missing_coords = cur.fetchall()
            fixed = 0
            for row in missing_coords:
                zone = {'city': row['city'], 'canton': row['canton']}
                resolve_zone_coords(zone)
                if zone.get('latitude') and zone.get('longitude'):
                    cur.execute(
                        "UPDATE search_zones SET latitude=%s, longitude=%s WHERE id=%s",
                        (zone['latitude'], zone['longitude'], row['id'])
                    )
                    fixed += 1
            if missing_coords:
                log.info(f"Zone GPS backfill: resolved {fixed}/{len(missing_coords)} zones")
        except Exception as e:
            log.warning(f"Zone GPS backfill failed: {e}")

        # Auto-create alert rows for active profiles without alerts
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


# ============================================================
# APP FACTORY
# ============================================================
def create_app():
    flask_app = Flask(__name__, static_folder='static')

    # CORS: restrict to known domains
    allowed_origins = os.environ.get(
        'ALLOWED_ORIGINS',
        'https://bonhome.ch,https://www.bonhome.ch,'
        'https://garou.ch,https://www.garou.ch,'
        'https://lou-platform.onrender.com,http://localhost:5000'
    ).split(',')
    CORS(flask_app, resources={r"/api/*": {"origins": allowed_origins}})

    # Security headers on every response
    @flask_app.after_request
    def _add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    # Register blueprints
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(properties_bp)
    flask_app.register_blueprint(chat_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(alerts_bp)
    flask_app.register_blueprint(scraping_bp)
    flask_app.register_blueprint(pages_bp)

    return flask_app


# Create the app at module level so `gunicorn app:app` works
app = create_app()

# Initialize DB + run migrations on module load (so gunicorn workers see schema ready).
# Errors here don't crash the app — the first request will retry.
if os.environ.get('DATABASE_URL', ''):
    try:
        init_db()
    except Exception as e:
        log.warning(f"DB init error (will retry on first request): {e}")
    try:
        _run_migrations()
    except Exception as e:
        log.warning(f"Migrations error on boot: {e}")

    # One-time rescore on deploy: the equipment synonyms changed (terrasse ≠ balcon)
    # so all scored_properties rows are stale.  Run in a background thread so boot
    # isn't blocked.
    def _rescore_all_on_boot():
        import time
        time.sleep(5)  # let gunicorn finish booting
        try:
            from scoring_engine import score_property
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                SELECT sp.*, sz_agg.zones
                FROM search_profiles sp
                LEFT JOIN LATERAL (
                    SELECT json_agg(row_to_json(sz)) AS zones
                    FROM search_zones sz WHERE sz.profile_id = sp.id
                ) sz_agg ON TRUE
                WHERE sp.is_active = TRUE
            """)
            profiles = [dict(r) for r in cur.fetchall()]
            for prof in profiles:
                import json as _json
                zones = prof.pop('zones', None)
                if isinstance(zones, str):
                    zones = _json.loads(zones)
                zones = zones or []
                zones = [dict(z) if not isinstance(z, dict) else z for z in zones]

                q = "SELECT * FROM properties WHERE is_active = TRUE"
                params = []
                if prof.get('transaction'):
                    q += " AND transaction = %s"
                    params.append(prof['transaction'])
                if prof.get('budget_max'):
                    q += " AND (price IS NULL OR price <= %s)"
                    params.append(int(float(prof['budget_max']) * 1.3))
                cur.execute(q, params)
                props = [dict(r) for r in cur.fetchall()]
                for prop in props:
                    result = score_property(prop, prof, zones)
                    cur.execute("""
                        INSERT INTO scored_properties
                            (property_id, profile_id, user_id, total_score, grade,
                             score_zone, score_budget, score_type, score_surface,
                             score_equipment, score_freshness, distance_km)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (property_id, profile_id)
                        DO UPDATE SET
                            total_score=EXCLUDED.total_score, grade=EXCLUDED.grade,
                            score_zone=EXCLUDED.score_zone, score_budget=EXCLUDED.score_budget,
                            score_type=EXCLUDED.score_type, score_surface=EXCLUDED.score_surface,
                            score_equipment=EXCLUDED.score_equipment, score_freshness=EXCLUDED.score_freshness,
                            distance_km=EXCLUDED.distance_km, scored_at=NOW()
                    """, (
                        prop['id'], prof['id'], prof['user_id'],
                        result['total_score'], result['grade'],
                        result['score_zone'], result['score_budget'],
                        result['score_type'], result['score_surface'],
                        result['score_equipment'], result['score_freshness'],
                        result['distance_km']
                    ))
                db.commit()
                log.info(f"Boot rescore: {len(props)} properties for profile {prof['id']}")
            cur.close()
            return_db(db)
            log.info("Boot rescore complete")
        except Exception as e:
            log.error(f"Boot rescore error: {e}", exc_info=True)

    import threading
    threading.Thread(target=_rescore_all_on_boot, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') != 'production')
