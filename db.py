"""
Bon Home — Database connection pool.
Extracted from app.py: ThreadedConnectionPool + pre-ping guard,
with a POOL_DISABLE kill-switch that falls back to one-shot connections.
"""
import os
import logging
import threading

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

log = logging.getLogger('lou-app')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
POOL_MIN = int(os.environ.get('POOL_MIN', '2'))
POOL_MAX = int(os.environ.get('POOL_MAX', '10'))
POOL_DISABLE = os.environ.get('POOL_DISABLE', '').lower() in ('1', 'true', 'yes')

_db_pool = None
_db_pool_lock = threading.Lock()


def _init_pool():
    """Lazy pool init so it happens AFTER gunicorn fork (each worker gets its own)."""
    global _db_pool
    if _db_pool is not None or POOL_DISABLE:
        return _db_pool
    with _db_pool_lock:
        if _db_pool is None and not POOL_DISABLE:
            try:
                _db_pool = pg_pool.ThreadedConnectionPool(
                    minconn=POOL_MIN,
                    maxconn=POOL_MAX,
                    dsn=DATABASE_URL,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
                log.info(f"DB pool initialized (min={POOL_MIN}, max={POOL_MAX})")
            except Exception as e:
                log.error(f"DB pool init failed, falling back to one-shot connections: {e}")
                _db_pool = None
    return _db_pool


def _ping(conn):
    """Return True if the connection is alive (SELECT 1 succeeds)."""
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        return True
    except Exception:
        return False


def get_db():
    """Get a DB connection. Tries the pool first (with pre-ping to skip stale
    SSL sockets), falls back to a fresh psycopg2.connect() on any pool error."""
    pool = _init_pool()
    if pool is not None:
        for _ in range(max(1, POOL_MAX - 1)):
            try:
                conn = pool.getconn()
            except Exception as e:
                log.warning(f"pool.getconn() failed, creating fresh connection: {e}")
                break
            if _ping(conn):
                return conn
            try:
                pool.putconn(conn, close=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def return_db(conn, close=False):
    """Return a connection to the pool (or close it if pooling disabled / broken).

    v6.3.4 (P0 SSL-poisoning fix) : l'appelant peut forcer `close=True` quand
    il a intercepté une OperationalError/InterfaceError — sinon une conn avec
    un stream TLS pourri (ex. "SSL bad record mac") re-rentre dans le pool
    (conn.closed reste parfois 0 après l'erreur) et empoisonne le prochain
    thread qui la récupère. On auto-force aussi `close=True` si le rollback
    lui-même lève : c'est la signature d'une conn cassée côté transport."""
    if conn is None:
        return
    if not close:
        try:
            if conn.closed:
                close = True
            else:
                conn.rollback()
        except Exception:
            # Rollback KO = conn cassée (SSL/socket). On la détruit.
            close = True
    pool = _db_pool
    if pool is None:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        broken = close or conn.closed or getattr(conn, 'status', None) == psycopg2.extensions.STATUS_IN_TRANSACTION
        pool.putconn(conn, close=bool(broken))
    except Exception as e:
        log.debug(f"pool.putconn() failed, closing connection: {e}")
        try:
            conn.close()
        except Exception:
            pass


def pool_stats():
    """
    Lightweight introspection for /health. Uses private psycopg2 attrs
    (_pool = free conns list, _used = dict of in-use conns) — stable in
    psycopg2 2.9.x. Never raises.
    """
    pool = _db_pool
    if pool is None:
        return {"enabled": False, "used": 0, "free": 0, "max": POOL_MAX}
    try:
        used = len(getattr(pool, '_used', {}) or {})
        free = len(getattr(pool, '_pool', []) or [])
        return {"enabled": True, "used": used, "free": free, "max": POOL_MAX}
    except Exception:
        return {"enabled": True, "used": -1, "free": -1, "max": POOL_MAX}


def init_db():
    """Create all tables if they don't exist (from schema.sql)."""
    sql_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    if os.path.exists(sql_path):
        conn = get_db()
        cur = conn.cursor()
        with open(sql_path, 'r') as f:
            cur.execute(f.read())
        conn.commit()
        cur.close()
        return_db(conn)
        log.info("Database initialized")
