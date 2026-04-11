-- ============================================
-- Bon Home — Database Setup (PostgreSQL)
-- Run this on Render PostgreSQL to create all tables
-- ============================================

-- Users
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    name            VARCHAR(100),
    phone           VARCHAR(20),
    created_at      TIMESTAMP DEFAULT NOW(),
    last_login      TIMESTAMP,
    is_active       BOOLEAN DEFAULT TRUE,
    plan            VARCHAR(20) DEFAULT 'free'
);

-- Search profiles (critères de recherche)
CREATE TABLE IF NOT EXISTS search_profiles (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(100) DEFAULT 'Ma recherche',
    property_types  TEXT[],
    transaction     VARCHAR(20),
    budget_min      INTEGER,
    budget_max      INTEGER,
    currency        VARCHAR(3) DEFAULT 'CHF',
    rooms_min       DECIMAL(3,1),
    rooms_max       DECIMAL(3,1),
    surface_min     INTEGER,
    surface_max     INTEGER,
    floor_min       INTEGER,
    floor_max       INTEGER,
    priorities      TEXT[],
    move_date       DATE,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    w_zone          INTEGER DEFAULT 30,
    w_budget        INTEGER DEFAULT 25,
    w_type          INTEGER DEFAULT 20,
    w_surface       INTEGER DEFAULT 10,
    w_equipment     INTEGER DEFAULT 10,
    w_freshness     INTEGER DEFAULT 5
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON search_profiles(user_id);

-- Search zones (zones géographiques multiples par profil)
CREATE TABLE IF NOT EXISTS search_zones (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER REFERENCES search_profiles(id) ON DELETE CASCADE,
    city            VARCHAR(100) NOT NULL,
    canton          VARCHAR(5),
    postal_code     VARCHAR(10),
    latitude        DECIMAL(9,6),
    longitude       DECIMAL(9,6),
    radius_km       DECIMAL(4,1) DEFAULT 3.0,
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zones_profile ON search_zones(profile_id);
CREATE INDEX IF NOT EXISTS idx_zones_geo ON search_zones(latitude, longitude);

-- Properties (annonces brutes scrapées)
CREATE TABLE IF NOT EXISTS properties (
    id              SERIAL PRIMARY KEY,
    external_id     VARCHAR(255),
    source          VARCHAR(50) NOT NULL,
    source_url      VARCHAR(500),
    title           VARCHAR(300),
    description     TEXT,
    property_type   VARCHAR(50),
    transaction     VARCHAR(20),
    price           INTEGER,
    currency        VARCHAR(3) DEFAULT 'CHF',
    price_unit      VARCHAR(20),
    rooms           DECIMAL(3,1),
    surface         INTEGER,
    floor           INTEGER,
    address         VARCHAR(300),
    city            VARCHAR(100),
    canton          VARCHAR(5),
    postal_code     VARCHAR(10),
    latitude        DECIMAL(9,6),
    longitude       DECIMAL(9,6),
    features        TEXT[],
    images          TEXT[],
    contact_name    VARCHAR(200),
    contact_phone   VARCHAR(50),
    contact_email   VARCHAR(200),
    published_at    TIMESTAMP,
    scraped_at      TIMESTAMP DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE,
    UNIQUE(external_id, source)
);
CREATE INDEX IF NOT EXISTS idx_prop_city ON properties(city);
CREATE INDEX IF NOT EXISTS idx_prop_canton ON properties(canton);
CREATE INDEX IF NOT EXISTS idx_prop_type ON properties(property_type, transaction);
CREATE INDEX IF NOT EXISTS idx_prop_price ON properties(price);
CREATE INDEX IF NOT EXISTS idx_prop_geo ON properties(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_prop_active ON properties(is_active, scraped_at);

-- Scored properties (score annonce × profil utilisateur)
CREATE TABLE IF NOT EXISTS scored_properties (
    id              SERIAL PRIMARY KEY,
    property_id     INTEGER REFERENCES properties(id) ON DELETE CASCADE,
    profile_id      INTEGER REFERENCES search_profiles(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    total_score     INTEGER NOT NULL,
    grade           VARCHAR(1),
    score_zone      INTEGER,
    score_budget    INTEGER,
    score_type      INTEGER,
    score_surface   INTEGER,
    score_equipment INTEGER,
    score_freshness INTEGER,
    distance_km     DECIMAL(5,1),
    scored_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE(property_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_scored_user ON scored_properties(user_id, total_score DESC);
CREATE INDEX IF NOT EXISTS idx_scored_profile ON scored_properties(profile_id, total_score DESC);

-- Favorites
CREATE TABLE IF NOT EXISTS favorites (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    property_id     INTEGER REFERENCES properties(id) ON DELETE CASCADE,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, property_id)
);

-- Alerts
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    profile_id      INTEGER REFERENCES search_profiles(id) ON DELETE CASCADE,
    channel         VARCHAR(20) DEFAULT 'email',
    frequency       VARCHAR(20) DEFAULT 'daily',
    min_score       INTEGER DEFAULT 70,
    is_active       BOOLEAN DEFAULT TRUE,
    last_sent       TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Conversations (historique chatbot IA)
CREATE TABLE IF NOT EXISTS conversations (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,
    session_id      VARCHAR(100),
    role            VARCHAR(10),
    content         TEXT NOT NULL,
    criteria_json   JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, created_at);

-- ============================================
-- Done! Tables created for Bon Home platform
-- ============================================
