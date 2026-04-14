"""
Bon Home — Cron Job
Scrape → Save → Score → Alert

Run once per day via Render Cron Job:
  Command: python cron_job.py
  Schedule: 0 16 * * *  (16h UTC = 18h CEST été / 17h CET hiver)
  Env: DATABASE_URL, ANTHROPIC_API_KEY (optional for alerts)
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
import psycopg2
import psycopg2.extras
import requests as http_requests

from scrapers import scrape_all, save_to_db
from scoring_engine import score_all_for_profile

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
log = logging.getLogger('lou-cron')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')


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


def _format_price(price):
    """Format price with apostrophes (Swiss style): 1250000 -> 1'250'000."""
    if not price:
        return '—'
    s = str(int(price))
    parts = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    return "'".join(reversed(parts))


def _grade_color(grade):
    """Return badge color for a grade."""
    return {
        'A': '#16a34a', 'B': '#65a30d', 'C': '#ca8a04', 'D': '#dc2626'
    }.get(grade, '#64748b')


def _build_alert_email(properties, count_total):
    """Build HTML email for property alerts."""
    rows_html = ''
    for p in properties:
        grade = p.get('grade', '?')
        score = p.get('total_score', 0)
        title = p.get('title') or 'Bien immobilier'
        price = _format_price(p.get('price'))
        unit = p.get('unit') or 'CHF'
        address = p.get('address') or p.get('city') or ''
        source_url = p.get('source_url') or 'https://www.bonhome.ch'
        rooms = p.get('rooms')
        surface = p.get('surface')
        details = []
        if rooms:
            details.append(f"{rooms} pcs")
        if surface:
            details.append(f"{int(surface)} m²")
        details_str = ' · '.join(details)

        rows_html += f'''
        <tr>
          <td style="padding:16px;border-bottom:1px solid #e2e8f0">
            <div style="display:flex;justify-content:space-between;align-items:start">
              <div style="flex:1">
                <a href="{source_url}" style="color:#0369a1;text-decoration:none;font-weight:600;font-size:15px">{title}</a>
                <div style="color:#64748b;font-size:13px;margin-top:4px">{address}</div>
                <div style="color:#334155;font-size:14px;margin-top:4px;font-weight:500">{price} {unit}</div>
                <div style="color:#64748b;font-size:13px;margin-top:2px">{details_str}</div>
              </div>
              <div style="text-align:center;margin-left:16px">
                <span style="display:inline-block;background:{_grade_color(grade)};color:#fff;font-weight:700;font-size:14px;width:36px;height:36px;line-height:36px;border-radius:50%;text-align:center">{grade}</span>
                <div style="color:#64748b;font-size:12px;margin-top:2px">{score}/100</div>
              </div>
            </div>
          </td>
        </tr>'''

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px">
    <!-- Header -->
    <div style="background:#0369a1;border-radius:12px 12px 0 0;padding:24px;text-align:center">
      <h1 style="margin:0;color:#ffffff;font-size:24px;letter-spacing:0.5px">Bon Home</h1>
      <p style="margin:8px 0 0;color:#bae6fd;font-size:14px">Votre chasseur immobilier digital</p>
    </div>
    <!-- Body -->
    <div style="background:#ffffff;padding:24px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0">
      <h2 style="margin:0 0 8px;color:#0f172a;font-size:18px">{count_total} nouveau{"x" if count_total > 1 else ""} bien{"s" if count_total > 1 else ""} correspond{"ent" if count_total > 1 else ""} a vos criteres</h2>
      <p style="margin:0 0 20px;color:#64748b;font-size:14px">Voici les meilleurs resultats depuis notre derniere alerte :</p>
      <table style="width:100%;border-collapse:collapse">
        {rows_html}
      </table>
    </div>
    <!-- CTA -->
    <div style="background:#ffffff;padding:0 24px 24px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;text-align:center">
      <a href="https://www.bonhome.ch" style="display:inline-block;background:#0369a1;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:600;font-size:15px;margin-top:16px">Voir tous mes resultats</a>
    </div>
    <!-- Footer -->
    <div style="background:#f8fafc;border-radius:0 0 12px 12px;padding:20px 24px;text-align:center;border:1px solid #e2e8f0;border-top:none">
      <p style="margin:0;color:#94a3b8;font-size:12px">Vous recevez cet email car vous avez active les alertes sur Bon Home.</p>
      <p style="margin:8px 0 0;color:#94a3b8;font-size:12px">Pour modifier la frequence ou desactiver les alertes, rendez-vous dans vos <a href="https://www.bonhome.ch" style="color:#0369a1">parametres</a>.</p>
    </div>
  </div>
</body>
</html>'''


def _send_alert_email(email, html, count):
    """Send a single alert email via Resend."""
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set, skipping alert email")
        return False
    try:
        subject = f"{count} nouveau{'x' if count > 1 else ''} bien{'s' if count > 1 else ''} — Bon Home"
        resp = http_requests.post('https://api.resend.com/emails', json={
            'from': 'Bon Home <noreply@bonhome.ch>',
            'to': [email],
            'subject': subject,
            'html': html,
        }, headers={
            'Authorization': f'Bearer {RESEND_API_KEY}',
            'Content-Type': 'application/json',
        }, timeout=10)
        if resp.status_code >= 400:
            log.error(f"Resend API error ({resp.status_code}): {resp.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Failed to send alert email to {email}: {e}")
        return False


def _send_alerts(db):
    """Check all active alerts and send emails for users with new matches."""
    cur = db.cursor()
    try:
        now = datetime.now(timezone.utc)
        # Fetch alerts that are due to be sent
        cur.execute("""
            SELECT a.id, a.user_id, a.profile_id, a.frequency, a.min_score, a.last_sent,
                   u.email
            FROM alerts a
            JOIN users u ON u.id = a.user_id AND u.is_active = TRUE
            WHERE a.is_active = TRUE
              AND a.channel = 'email'
              AND (
                  a.frequency = 'instant'
                  OR (a.frequency = 'daily' AND (a.last_sent IS NULL OR a.last_sent < NOW() - INTERVAL '24 hours'))
                  OR (a.frequency = 'weekly' AND (a.last_sent IS NULL OR a.last_sent < NOW() - INTERVAL '7 days'))
              )
        """)
        alerts = cur.fetchall()
        log.info(f"Found {len(alerts)} alerts due for sending")

        for alert in alerts:
            try:
                since = alert['last_sent'] or (now - timedelta(days=30))
                # Get new scored properties since last_sent meeting min_score threshold
                cur.execute("""
                    SELECT sp.total_score, sp.grade, sp.scored_at,
                           p.title, p.price, p.unit, p.address, p.city, p.rooms, p.surface,
                           p.source_url, p.source
                    FROM scored_properties sp
                    JOIN properties p ON p.id = sp.property_id
                    WHERE sp.profile_id = %s
                      AND sp.scored_at > %s
                      AND sp.total_score >= %s
                      AND p.is_active = TRUE
                    ORDER BY sp.total_score DESC
                """, (alert['profile_id'], since, alert['min_score']))
                properties = cur.fetchall()

                if not properties:
                    log.info(f"Alert {alert['id']} ({alert['email']}): no new matches")
                    continue

                count_total = len(properties)
                top_5 = properties[:5]

                html = _build_alert_email(top_5, count_total)
                success = _send_alert_email(alert['email'], html, count_total)

                if success:
                    cur.execute("UPDATE alerts SET last_sent = NOW() WHERE id = %s", (alert['id'],))
                    db.commit()
                    log.info(f"Alert sent to {alert['email']}: {count_total} matches")
                else:
                    log.warning(f"Alert email failed for {alert['email']}, will retry next run")

            except Exception as e:
                log.error(f"Error processing alert {alert['id']}: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

    except Exception as e:
        log.error(f"Alert sending failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        cur.close()


def run():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    log.info("=" * 50)
    log.info("Bon Home Cron Job Start")
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
    # Include all major cities in canton Neuchâtel for comprehensive coverage
    NE_CITIES = [
        'Neuchâtel', 'La Chaux-de-Fonds', 'Le Locle', 'Cortaillod',
        'Peseux', 'Boudry', 'Val-de-Travers', 'Milvignes',
        'Hauterive', 'Saint-Blaise', 'Colombier', 'Corcelles-Cormondrèche',
        'La Tène', 'Le Landeron', 'Bevaix', 'Val-de-Ruz'
    ]

    scrape_targets = set()
    transactions_needed = set()
    for p in profiles:
        zones = p.get('zones', [])
        tx = p.get('transaction', 'location')
        transactions_needed.add(tx)
        if zones:
            for z in zones:
                if isinstance(z, dict) and z.get('city'):
                    scrape_targets.add((z['city'], tx))

    # Add all Neuchâtel canton cities for each transaction type needed
    for tx in transactions_needed:
        for city in NE_CITIES:
            scrape_targets.add((city, tx))

    if not scrape_targets:
        log.info("No scrape targets found (profiles have no zones)")
        db.close()
        return

    log.info(f"Scraping {len(scrape_targets)} city/transaction combos (canton NE)")

    # Step 3: Scrape all targets (commit after each city to avoid losing data)
    # skip_nearby=True: cron already includes all NE main cities explicitly,
    # so don't re-scrape Neuchâtel 12× via NEARBY_MAIN_CITY expansion.
    total_scraped = 0
    for city, transaction in scrape_targets:
        log.info(f"--- Scraping: {city} ({transaction}) ---")
        try:
            listings = scrape_all(city=city, transaction=transaction, skip_nearby=True)
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

    # Step 6: Send email alerts for new high-score matches
    _send_alerts(db)

    db.close()
    log.info("=" * 50)
    log.info("Cron Job Complete")
    log.info("=" * 50)


if __name__ == '__main__':
    run()
