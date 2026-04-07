"""
Lou Garou — Cron Job
Scrape → Save → Score → Alert

Run every 2 hours via Render Cron Job:
  Command: python cron_job.py
  Schedule: 0 */2 * * *
  Env: DATABASE_URL, ANTHROPIC_API_KEY (optional for alerts)
"""

import os
import sys
import logging
import psycopg2
import psycopg2.extras

from scrapers import scrape_all, save_to_db
from scoring_engine import score_all_for_profile

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
log = logging.getLogger('lou-cron')

DATABASE_URL = os.environ.get('DATABASE_URL', '')


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def get_active_profiles(db):
    """Get all active search profiles with their zones."""
    cur = db.cursor()
    cur.execute("""
        SELECT sp.*, u.email,
               json_agg(json_build_object(
                   'city', sz.city, 'canton', sz.canton,
                   'radius_km', sz.radius_km, 'latitude', sz.latitude, 'longitude', sz.longitude
               )) as zones
        FROM search_profiles sp
        JOIN users u ON u.id = sp.user_id
        LEFT JOIN search_zones sz ON sz.profile_id = sp.id
        WHERE sp.is_active = TRUE AND u.is_active = TRUE
        GROUP BY sp.id, u.email
    """)
    profiles = cur.fetchall()
    cur.close()
    return profiles


def run():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    log.info("=" * 50)
    log.info("Lou Garou Cron Job Start")
    log.info("=" * 50)

    db = get_db()

    # Step 1: Get all active profiles and their cities
    profiles = get_active_profiles(db)
    log.info(f"Found {len(profiles)} active search profiles")

    if not profiles:
        log.info("No active profiles, nothing to do")
        db.close()
        return

    # Step 2: Collect unique city + transaction combos to scrape
    scrape_targets = set()
    for p in profiles:
        zones = p.get('zones', [])
        tx = p.get('transaction', 'location')
        if zones:
            for z in zones:
                if isinstance(z, dict) and z.get('city'):
                    scrape_targets.add((z['city'], tx))
        # No fallback — skip profiles without zones

    if not scrape_targets:
        log.info("No scrape targets found (profiles have no zones)")
        db.close()
        return

    log.info(f"Scraping {len(scrape_targets)} city/transaction combos")

    # Step 3: Scrape all targets (commit after each city to avoid losing data)
    total_scraped = 0
    for city, transaction in scrape_targets:
        log.info(f"--- Scraping: {city} ({transaction}) ---")
        try:
            listings = scrape_all(city=city, transaction=transaction)
            if listings:
                saved = save_to_db(db, listings)
                total_scraped += saved
                log.info(f"Saved {saved} listings for {city}")
        except Exception as e:
            log.error(f"Scrape failed for {city}: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    log.info(f"Total scraped and saved: {total_scraped}")

    # Step 4: Deactivate old listings (> 30 days without update)
    cur = db.cursor()
    cur.execute("""
        UPDATE properties SET is_active = FALSE
        WHERE scraped_at < NOW() - INTERVAL '30 days' AND is_active = TRUE
    """)
    deactivated = cur.rowcount
    db.commit()
    if deactivated:
        log.info(f"Deactivated {deactivated} old listings")

    # Step 5: Score all profiles
    log.info("--- Scoring ---")
    for p in profiles:
        try:
            scored = score_all_for_profile(db, p['id'])
            log.info(f"Profile {p['id']} ({p['email']}): scored {scored} properties")
        except Exception as e:
            log.error(f"Scoring failed for profile {p['id']}: {e}")
            db.rollback()

    # Step 6: TODO - Send alerts for new high-score matches
    # This would check alerts table and send emails/SMS
    # For now, just log new A/B matches
    cur = db.cursor()
    cur.execute("""
        SELECT sp.user_id, u.email, COUNT(*) as new_matches
        FROM scored_properties sp
        JOIN users u ON u.id = sp.user_id
        WHERE sp.scored_at > NOW() - INTERVAL '2 hours'
        AND sp.grade IN ('A', 'B')
        GROUP BY sp.user_id, u.email
    """)
    for row in cur.fetchall():
        log.info(f"New matches for {row['email']}: {row['new_matches']} (A/B grade)")
    cur.close()

    db.close()
    log.info("=" * 50)
    log.info("Cron Job Complete")
    log.info("=" * 50)


if __name__ == '__main__':
    run()
