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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') != 'production')
