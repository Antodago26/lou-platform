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
import signal
import threading
import logging

# ----------------------------------------------------------------------
# v6.3.3 (DB4) — shutdown event partagé. Les threads daemon de longue
# durée (boot rescore, bg scrapes) checkent ce flag entre les itérations
# pour exit proprement au SIGTERM Render (redeploy) plutôt que d'être
# tués mid-transaction.
# ----------------------------------------------------------------------
SHUTDOWN_EVENT = threading.Event()


def _install_shutdown_handler():
    def _handler(signum, _frame):
        log = logging.getLogger('lou-app')
        log.warning(f"Shutdown signal {signum} received, setting SHUTDOWN_EVENT")
        SHUTDOWN_EVENT.set()
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        # Sous gunicorn les handlers master/worker peuvent déjà être set ;
        # on n'écrase pas s'ils existent. L'event restera False → pas de
        # régression.
        pass


_install_shutdown_handler()

# ----------------------------------------------------------------------
# Sentry init (v6.3.3). MUST run before any other import that may raise,
# for capture-at-import to work. No-op si SENTRY_DSN pas set (dev local).
# ----------------------------------------------------------------------
_SENTRY_DSN = os.environ.get('SENTRY_DSN', '').strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[
                FlaskIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            environment=os.environ.get('FLASK_ENV', 'development'),
            release=os.environ.get('RENDER_GIT_COMMIT', 'unknown')[:8],
            # Beta : 100% des erreurs, 10% des traces (sampler cost-sensitive).
            traces_sample_rate=0.1,
            send_default_pii=False,  # RGPD : pas d'IP/user dans les events.
        )
    except Exception as _se:
        # Import/init error ne doit jamais bloquer le boot.
        logging.getLogger('lou-app').error(f"Sentry init failed: {_se}")

from flask import Flask, request, jsonify
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
from routes_stats import stats_bp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('lou-app')


# ============================================================
# Neon cold-start warmup (v6.3.4.1)
# ============================================================
def _neon_warmup(max_attempts=3, sleep_s=2.0):
    """Chauffe le pool DB avant les gros jobs de boot (rescore).

    Neon (Postgres serverless) peut être en cold-start au boot de l'app : les
    premières vraies requêtes sur des conns fraîchement ouvertes tombent sur
    des erreurs de transport TLS transitoires pendant la fenêtre ~1-3s où le
    backend se réveille. Boucler un SELECT 1 avec retry (a) laisse à Neon le
    temps de se réveiller, et (b) évince via close=True toute conn formée
    durant la fenêtre cold-start — filet complémentaire au fix conn_broken
    dans _rescore_all_on_boot.

    VOLONTAIREMENT les messages ne contiennent PAS l'exception brute : le
    watchdog scripts/monitor_p0.py grep le log Render pour 'bad record mac',
    'OperationalError', 'ssl error', etc. et déclencherait un FAIL rouge si
    on loggait l'erreur textuelle ici (ce sont des erreurs attendues pendant
    le cold-start, pas une régression P0). On garde les exceptions pour
    Sentry via exc_info=False mais label neutre 'transient'.

    Returns: True si un SELECT 1 a réussi dans max_attempts, False sinon.
    En cas de False, l'appelant SKIPPE le travail lourd — la prochaine
    requête utilisateur réveillera la DB naturellement."""
    import time as _time
    import psycopg2 as _pg
    for attempt in range(1, max_attempts + 1):
        conn = None
        ok = False
        conn_broken = False
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT 1')
            cur.fetchone()
            cur.close()
            ok = True
        except (_pg.OperationalError, _pg.InterfaceError):
            conn_broken = True
        except Exception:
            # Erreur inconnue : on considère aussi la conn suspecte
            conn_broken = True
        finally:
            if conn is not None:
                try:
                    return_db(conn, close=conn_broken)
                except Exception:
                    pass
        if ok:
            log.info(f"Neon warmup: OK on attempt {attempt}/{max_attempts}")
            return True
        if attempt < max_attempts:
            log.info(f"Neon warmup: attempt {attempt}/{max_attempts} transient, retry in {sleep_s}s")
            _time.sleep(sleep_s)
    log.warning(
        f"Neon warmup: {max_attempts} attempts did not succeed, skipping boot rescore "
        "(first user request will wake the DB)"
    )
    return False


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
                resolve_zone_coords(zone, conn=db)
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
    flask_app = Flask(__name__, static_folder='static', template_folder='templates')

    # static_url(path) Jinja helper — auto-bumps cache buster from file mtime.
    # Closes the manual ?v=20260420b trap (memory note 2026-04-14): if you
    # forget to bump the version after editing brand.css, users keep seeing
    # the old file. With static_url() the path becomes /static/brand.css?v=N
    # where N is the file's mtime as int — changes the moment you save.
    # Pair with the long-cache header below: ?v= present → 1-year cache.
    _static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

    def static_url(path):
        full = os.path.join(_static_dir, path)
        try:
            mtime = int(os.path.getmtime(full))
        except OSError:
            log.warning(f"static_url: missing file {path}")
            return f"/static/{path}"
        return f"/static/{path}?v={mtime}"

    flask_app.jinja_env.globals['static_url'] = static_url

    # CORS: restrict to known domains.
    # v6.3 security: in production, require ALLOWED_ORIGINS to be explicitly set
    # so a missing env var can't silently whitelist http://localhost:5000.
    _is_prod = os.environ.get('FLASK_ENV') == 'production'
    _default_origins = '' if _is_prod else (
        'https://bonhome.ch,https://www.bonhome.ch,'
        'https://garou.ch,https://www.garou.ch,'
        'https://lou-platform.onrender.com,http://localhost:5000'
    )
    allowed_origins = [o.strip() for o in os.environ.get('ALLOWED_ORIGINS', _default_origins).split(',') if o.strip()]
    if _is_prod and not allowed_origins:
        raise RuntimeError("ALLOWED_ORIGINS env var is required in production")
    CORS(flask_app, resources={r"/api/*": {"origins": allowed_origins}})

    # Security headers on every response.
    # CSP allowlist (audit M1, 2026-05-04): allows hCaptcha, Google Fonts,
    # Leaflet via unpkg, Swiss federal geo API. 'unsafe-inline' is kept for
    # script-src and style-src because the HTML pages embed inline blocks
    # (TODO: move to nonces or external files, then drop unsafe-inline).
    # X-XSS-Protection dropped — deprecated and can introduce XS-Leak issues.
    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://js.hcaptcha.com https://*.hcaptcha.com https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self' https://api.hcaptcha.com https://*.hcaptcha.com https://api3.geo.admin.ch; "
        "frame-src https://*.hcaptcha.com https://newassets.hcaptcha.com; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    _PERMISSIONS_POLICY = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "magnetometer=(), gyroscope=(), accelerometer=(), interest-cohort=()"
    )

    @flask_app.after_request
    def _add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = _CSP
        response.headers['Permissions-Policy'] = _PERMISSIONS_POLICY
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

        # Cache-Control for static assets: long-cache + immutable when the
        # request carries a ?v= cache buster (auto-injected by static_url),
        # no-cache otherwise. This makes the cache buster actually do something
        # — Flask's default `no-cache` for sent files would otherwise force
        # every visit to re-download brand.css/components.css/app.js.
        if request.path.startswith('/static/'):
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'no-cache'

        return response

    # Audit H6 (2026-05) : routes admin/properties qui n'ont pas leur propre
    # try/except retournaient un 500 HTML générique sur erreur DB, cassant
    # les consumers JSON. Ces handlers globaux interceptent et renvoient du
    # JSON propre. Sentry capture quand même la stack via app.logger.
    import psycopg2 as _pg_for_handlers

    @flask_app.errorhandler(_pg_for_handlers.Error)
    def _handle_psycopg2_error(e):
        log.exception(f"Unhandled DB error on {request.path}")
        return jsonify({"error": "Erreur serveur (DB)"}), 500

    @flask_app.errorhandler(500)
    def _handle_500(e):
        # Catch-all pour les routes /api/* qui n'ont pas de try/except.
        # Les routes pages (/, /faq, etc.) gardent la 500 HTML par défaut.
        if request.path.startswith('/api/'):
            log.exception(f"Unhandled exception on {request.path}")
            return jsonify({"error": "Erreur serveur"}), 500
        return e

    # Register blueprints
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(properties_bp)
    flask_app.register_blueprint(chat_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(alerts_bp)
    flask_app.register_blueprint(scraping_bp)
    flask_app.register_blueprint(pages_bp)
    flask_app.register_blueprint(stats_bp)

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

    # v6.3.2 schema: migrations_applied registry + properties.gps_source column.
    # Idempotent (IF NOT EXISTS). Safe à chaque boot.
    try:
        from migrations.schema_v632 import run_schema_v632
        conn = get_db()
        try:
            stats = run_schema_v632(conn)
            log.info(f"v6.3.2 schema stats: {stats}")
        finally:
            return_db(conn)
    except Exception as e:
        log.exception(f"v6.3.2 schema error: {e}")

    # v6.4.0 schema: tables QA (qa_recall_snapshots, qa_link_checks,
    # qa_field_validations, qa_runs + ENUM qa_run_type). Idempotent
    # (CREATE ... IF NOT EXISTS + bloc DO anonyme pour l'ENUM).
    #
    # Pattern conn_broken : si Neon timeout / SSL bad record mac pendant
    # la création, on détruit la conn via return_db(close=True) pour ne
    # pas empoisonner le pool. WARNING plutôt que EXCEPTION : si la
    # migration échoue, le boot continue et les endpoints/workers QA
    # lèveront proprement au premier accès aux tables manquantes (le
    # cron next day retentera la migration sur le boot suivant).
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v640 import run_schema_v640
        conn = get_db()
        stats = run_schema_v640(conn)
        log.info(f"v6.4.0 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.0 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.4.1 schema : widen qa_recall_snapshots.recall_pct NUMERIC(5,2)
    # → NUMERIC(7,2) pour accepter les valeurs > 100% (DB plus riche que
    # live scraping = signal diagnostique qu'on préserve, pas un bug).
    # Idempotent via lookup information_schema.columns avant l'ALTER.
    # Doit tourner APRÈS run_schema_v640 (widen une colonne créée par v640).
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v641 import run_schema_v641
        conn = get_db()
        stats = run_schema_v641(conn)
        log.info(f"v6.4.1 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.1 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.4.2 schema : extensions pour la Phase 2 (link health check) du
    # cron lou-qa-recall. Ajoute properties.last_checked_at + index partiel,
    # et qa_link_checks.{status, final_url, error_msg}. Tout via ALTER ADD
    # COLUMN IF NOT EXISTS — idempotent. Doit tourner APRÈS run_schema_v640
    # (qa_link_checks doit déjà exister).
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v642 import run_schema_v642
        conn = get_db()
        stats = run_schema_v642(conn)
        log.info(f"v6.4.2 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.2 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.4.3 schema : backfill correctif des classifications 5xx mal
    # étiquetées en 'broken' au run d8fb6e2 (cf. commit v6.4.6 fix). One-shot,
    # gate idempotent via migrations_applied. Doit tourner APRÈS v642 (qui
    # garantit que la colonne `status` existe).
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v643 import run_schema_v643
        conn = get_db()
        stats = run_schema_v643(conn)
        log.info(f"v6.4.3 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.3 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.4.4 schema : table qa_source_health (repurpose Phase 1 du cron
    # lou-qa-recall après drop Homegate + ImmoScout24, décision CEO 30/04).
    # CREATE TABLE IF NOT EXISTS — idempotent.
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v644 import run_schema_v644
        conn = get_db()
        stats = run_schema_v644(conn)
        log.info(f"v6.4.4 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.4 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.4.5 schema : 4 indexes manquants identifiés par l'audit 2026-05-04.
    # Cross-portal dedup, link-health select, first-seen-active, scored-zone.
    # CREATE INDEX IF NOT EXISTS — idempotent.
    conn = None
    conn_broken = False
    try:
        import psycopg2 as _pg
        from migrations.schema_v645 import run_schema_v645
        conn = get_db()
        stats = run_schema_v645(conn)
        log.info(f"v6.4.5 schema stats: {stats}")
    except Exception as e:
        try:
            import psycopg2 as _pg
            if isinstance(e, (_pg.OperationalError, _pg.InterfaceError)):
                conn_broken = True
        except Exception:
            pass
        log.warning(f"v6.4.5 schema error (boot continues, will retry next boot): {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # v6.3 backfills: rooms (NULL/0), Homegate titles (.â mojibake),
    # addresses (leading 'CH '/NPA/dots), properties GPS.
    # Doit tourner APRÈS _run_migrations() (qui backfill les zones GPS) et
    # AVANT _rescore_all_on_boot (qui a besoin des GPS pour haversine).
    try:
        from migrations.backfill_v63 import run_all as run_v63_backfills
        from scoring_engine import _lookup_city_coords
        conn = get_db()
        try:
            stats = run_v63_backfills(conn, _lookup_city_coords)
            log.info(f"v6.3 backfill stats: {stats}")
        finally:
            return_db(conn)
    except Exception as e:
        log.exception(f"v6.3 backfill error: {e}")

    # v6.3.2 refix GPS NPA-fallback — gardé par env var V632_REFIX_MODE
    # (skip | dry-run | apply). skip par défaut pour safety.
    # Auto-skip si déjà appliqué (migrations_applied).
    # Doit tourner AVANT _rescore_all_on_boot pour que le rescore utilise
    # les coords corrigées.
    try:
        from migrations.refix_gps_npa_fallback import run_at_boot as refix_run_at_boot
        from scoring_engine import _lookup_city_coords
        conn = get_db()
        try:
            stats = refix_run_at_boot(conn, _lookup_city_coords)
            log.info(f"v6.3.2 refix GPS stats: {stats.get('mode')} detected={stats.get('detected')} "
                     f"updated={stats.get('updated')} status={stats.get('status')}")
        finally:
            return_db(conn)
    except Exception as e:
        log.exception(f"v6.3.2 refix GPS error: {e}")

    # One-time rescore on deploy (v6.3.3 DB1 : protégé par advisory lock).
    # Avant : chaque worker gunicorn relançait le rescore → 2× travail à
    # chaque redeploy (~150k UPSERTs inutiles avec 15 testeurs). Maintenant
    # un seul worker acquiert pg_try_advisory_lock(BOOT_RESCORE_LOCK_KEY),
    # les autres voient le lock occupé et skippent proprement.
    #
    # Clé fixe arbitraire (bigint). pg_try_advisory_lock est non-bloquant :
    # il renvoie false si déjà pris. Auto-release à la fermeture de la
    # session Postgres si le worker crash sans unlock explicite.
    BOOT_RESCORE_LOCK_KEY = 7463092017042019  # "rescore" hash-style + date

    def _rescore_all_on_boot():
        import time
        import psycopg2 as _pg
        time.sleep(5)  # let gunicorn finish booting
        # v6.3.4.1 : warm-up Neon (cold-start) AVANT d'attaquer le rescore.
        # Si Neon n'est pas prêt après 3× SELECT 1 espacés de 2s, on skip
        # proprement — pas de crash, pas de conn pourrie dans le pool. La
        # prochaine requête utilisateur réveillera la DB.
        if not _neon_warmup():
            return
        db = None
        lock_acquired = False
        conn_broken = False
        try:
            from scoring_engine import upsert_scored_properties
            db = get_db()
            cur = db.cursor()

            cur.execute("SELECT pg_try_advisory_lock(%s)", (BOOT_RESCORE_LOCK_KEY,))
            row = cur.fetchone()
            got = bool(row and (row[0] if not isinstance(row, dict) else list(row.values())[0]))
            if not got:
                log.info("Boot rescore: another worker holds the lock, skipping")
                cur.close()
                return_db(db)
                return
            lock_acquired = True
            log.info("Boot rescore: acquired leader lock, proceeding")
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
                # v6.3.3 (DB4) : check SIGTERM entre chaque profil pour
                # sortir proprement d'un redeploy Render plutôt que d'être
                # tué mid-INSERT (risque incohérence scored_properties).
                if SHUTDOWN_EVENT.is_set():
                    log.warning("Boot rescore: SHUTDOWN_EVENT set, exiting loop cleanly")
                    break
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
                props = cur.fetchall()
                upsert_scored_properties(cur, props, prof, zones, user_id=prof['user_id'])
                db.commit()
                log.info(f"Boot rescore: {len(props)} properties for profile {prof['id']}")
            log.info("Boot rescore complete")
        except (_pg.OperationalError, _pg.InterfaceError) as e:
            # SSL bad record mac, server closed connection, etc. → conn pourrie.
            # On flag pour forcer close=True : sinon return_db la remet dans le
            # pool et le prochain thread qui l'emprunte retombe sur la même erreur.
            conn_broken = True
            log.error(f"Boot rescore DB transport error (connection will be closed): {e}", exc_info=True)
        except Exception as e:
            log.error(f"Boot rescore error: {e}", exc_info=True)
        finally:
            # Release advisory lock (auto-released sinon à la fin de session PG).
            # Skip si la conn est cassée : l'unlock va juste re-échouer.
            if lock_acquired and db is not None and not conn_broken:
                try:
                    cur2 = db.cursor()
                    cur2.execute("SELECT pg_advisory_unlock(%s)", (BOOT_RESCORE_LOCK_KEY,))
                    cur2.close()
                    db.commit()
                except Exception:
                    pass
            if db is not None:
                try:
                    return_db(db, close=conn_broken)
                except Exception:
                    pass

    import threading
    threading.Thread(target=_rescore_all_on_boot, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') != 'production')
