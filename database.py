"""
Lou Garou — Database Layer (SQLite, zero dependencies)
Tables: users, search_profiles, properties, alerts
"""
import sqlite3
import os
import json
import hashlib
import secrets
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "lou.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        name TEXT,
        phone TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        last_login TEXT
    );

    CREATE TABLE IF NOT EXISTS search_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        canton TEXT,
        city TEXT,
        property_type TEXT,
        transaction_type TEXT,
        budget TEXT,
        rooms TEXT,
        priorities TEXT,  -- JSON array
        lou_score INTEGER,
        lou_grade TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL,
        source TEXT NOT NULL,           -- homegate, immoscout24, comparis...
        source_id TEXT,                 -- ID on the portal
        source_url TEXT,
        title TEXT,
        price INTEGER,
        price_unit TEXT,                -- CHF, CHF/mois
        rooms REAL,
        surface_m2 REAL,
        address TEXT,
        city TEXT,
        canton TEXT,
        description TEXT,
        images TEXT,                    -- JSON array of URLs
        lou_score INTEGER,
        lou_grade TEXT,
        score_details TEXT,             -- JSON: {correspondance, preferences, qualite, lifestyle}
        is_new INTEGER DEFAULT 1,
        is_favorite INTEGER DEFAULT 0,
        is_dismissed INTEGER DEFAULT 0,
        found_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (profile_id) REFERENCES search_profiles(id)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        profile_id INTEGER NOT NULL,
        type TEXT DEFAULT 'email',      -- email, push
        frequency TEXT DEFAULT 'daily', -- instant, daily, weekly
        is_active INTEGER DEFAULT 1,
        last_sent TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (profile_id) REFERENCES search_profiles(id)
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE INDEX IF NOT EXISTS idx_properties_profile ON properties(profile_id);
    CREATE INDEX IF NOT EXISTS idx_properties_score ON properties(lou_score DESC);
    CREATE INDEX IF NOT EXISTS idx_search_user ON search_profiles(user_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
    """)
    conn.commit()
    conn.close()


# ─── Auth helpers ───

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return h.hex(), salt


def create_user(email, password, name=None, phone=None):
    conn = get_db()
    pw_hash, salt = hash_password(password)
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, salt, name, phone) VALUES (?,?,?,?,?)",
            (email.lower().strip(), pw_hash, salt, name, phone)
        )
        conn.commit()
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return {"ok": True, "user_id": user_id}
    except sqlite3.IntegrityError:
        conn.close()
        return {"ok": False, "error": "Email déjà utilisé"}


def authenticate(email, password):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    if not row:
        conn.close()
        return None
    pw_hash, _ = hash_password(password, row["salt"])
    if pw_hash != row["password_hash"]:
        conn.close()
        return None
    # Update last login
    conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return dict(row)


def create_session(user_id, hours=72):
    conn = get_db()
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                 (token, user_id, expires))
    conn.commit()
    conn.close()
    return token


def get_user_from_token(token):
    conn = get_db()
    row = conn.execute("""
        SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id
        WHERE s.token=? AND s.expires_at > datetime('now')
    """, (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── Search Profiles ───

def create_search_profile(user_id, criteria):
    conn = get_db()
    conn.execute("""
        INSERT INTO search_profiles
        (user_id, canton, city, property_type, transaction_type, budget, rooms, priorities, lou_score, lou_grade)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id,
        criteria.get("canton"),
        criteria.get("city"),
        criteria.get("property_type"),
        criteria.get("transaction_type"),
        criteria.get("budget"),
        criteria.get("rooms"),
        json.dumps(criteria.get("priorities", [])),
        criteria.get("lou_score"),
        criteria.get("lou_grade"),
    ))
    conn.commit()
    profile_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Auto-create daily alert
    conn.execute(
        "INSERT INTO alerts (user_id, profile_id) VALUES (?,?)",
        (user_id, profile_id)
    )
    conn.commit()
    conn.close()
    return profile_id


def get_user_profiles(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM search_profiles WHERE user_id=? AND is_active=1 ORDER BY created_at DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Properties ───

def add_property(profile_id, prop):
    conn = get_db()
    conn.execute("""
        INSERT OR IGNORE INTO properties
        (profile_id, source, source_id, source_url, title, price, price_unit,
         rooms, surface_m2, address, city, canton, description, images,
         lou_score, lou_grade, score_details)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        profile_id,
        prop.get("source"),
        prop.get("source_id"),
        prop.get("source_url"),
        prop.get("title"),
        prop.get("price"),
        prop.get("price_unit"),
        prop.get("rooms"),
        prop.get("surface_m2"),
        prop.get("address"),
        prop.get("city"),
        prop.get("canton"),
        prop.get("description"),
        json.dumps(prop.get("images", [])),
        prop.get("lou_score"),
        prop.get("lou_grade"),
        json.dumps(prop.get("score_details", {})),
    ))
    conn.commit()
    conn.close()


def get_properties(profile_id, limit=50, offset=0, sort="lou_score DESC"):
    conn = get_db()
    rows = conn.execute(f"""
        SELECT * FROM properties
        WHERE profile_id=? AND is_dismissed=0
        ORDER BY {sort}
        LIMIT ? OFFSET ?
    """, (profile_id, limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_property_stats(profile_id):
    conn = get_db()
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN is_new=1 THEN 1 END) as new_count,
            COUNT(CASE WHEN is_favorite=1 THEN 1 END) as favorites,
            AVG(lou_score) as avg_score,
            COUNT(DISTINCT source) as sources
        FROM properties WHERE profile_id=? AND is_dismissed=0
    """, (profile_id,)).fetchone()
    conn.close()
    return dict(stats)


def toggle_favorite(property_id, user_id):
    conn = get_db()
    conn.execute("""
        UPDATE properties SET is_favorite = 1 - is_favorite
        WHERE id=? AND profile_id IN (SELECT id FROM search_profiles WHERE user_id=?)
    """, (property_id, user_id))
    conn.commit()
    conn.close()


def dismiss_property(property_id, user_id):
    conn = get_db()
    conn.execute("""
        UPDATE properties SET is_dismissed=1
        WHERE id=? AND profile_id IN (SELECT id FROM search_profiles WHERE user_id=?)
    """, (property_id, user_id))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized:", DB_PATH)
