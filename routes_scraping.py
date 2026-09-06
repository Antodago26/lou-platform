"""Bon Home — Scraping / scoring / cron Blueprint."""
import os
import re
import hmac
import logging
import threading

from flask import Blueprint, jsonify, request

from db import get_db, return_db
from auth import token_required, admin_required, ingest_or_admin_required
from rate_limit import check_rate_limit, rate_limited_response

log = logging.getLogger('lou-app')
scraping_bp = Blueprint('scraping', __name__)

CRON_SECRET = os.environ.get('CRON_SECRET', '')
if not CRON_SECRET:
    log.warning("CRON_SECRET not configured — /api/cron/scrape and /api/scrape/{debug,test} will reject all requests")

# Caps for /api/import payload sanitization
_IMPORT_MAX_TITLE_LEN = 300
_IMPORT_MAX_DESC_LEN = 10000
_IMPORT_MAX_URL_LEN = 500
_IMPORT_MAX_IMAGES = 50
_IMPORT_URL_RE = re.compile(r'^https?://', re.IGNORECASE)

# Per-user throttles — heavy operations that burn ScrapingBee credits.
_SCRAPE_PER_USER_MIN = 1
_SCRAPE_PER_USER_HOUR = 5
_IMPORT_PER_USER_MIN = 5
_IMPORT_PER_USER_HOUR = 60


def _safe_http_url(value):
    """Return value if it's an http(s) URL within size limits, else None."""
    if not isinstance(value, str):
        return None
    if len(value) > _IMPORT_MAX_URL_LEN:
        return None
    if not _IMPORT_URL_RE.match(value):
        return None
    return value


def _require_cron_secret():
    """Header-only since audit H2 (2026-05-04) — query-string fallback dropped
    to prevent the secret leaking into Cloudflare/Render access logs and
    Referer headers. Constant-time comparison to defeat timing oracles."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return False
    if not secret:
        return False
    return hmac.compare_digest(secret, CRON_SECRET)


@scraping_bp.route('/api/scrape', methods=['POST'])
@token_required
def api_scrape():
    """Trigger scraping based on user's search profile zones. Background-threaded."""
    user_id = request.user_id
    if not check_rate_limit(f"scrape:user:{user_id}", _SCRAPE_PER_USER_MIN, _SCRAPE_PER_USER_HOUR):
        return rate_limited_response(retry_after_seconds=60)

    data = request.get_json(silent=True) or {}
    city = data.get('city')
    transaction = data.get('transaction', 'location')

    if not city:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT sz.city FROM search_zones sz
                JOIN search_profiles sp ON sp.id = sz.profile_id
                WHERE sp.user_id = %s AND sp.is_active = TRUE
            """, (user_id,))
            rows = cur.fetchall()
            cities = [r['city'] for r in rows if r['city']]
            if not cities:
                return jsonify({"error": "Aucune zone configurée. Ajoutez des zones dans votre profil."}), 400

            cur.execute("""
                SELECT transaction FROM search_profiles
                WHERE user_id = %s AND is_active = TRUE
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            prof = cur.fetchone()
            if prof and prof['transaction']:
                transaction = prof['transaction']
        finally:
            cur.close()
            return_db(conn)
    else:
        cities = [city]

    def _bg_scrape(city_list, tx, uid):
        from scrapers import scrape_all, save_to_db
        from scoring_engine import upsert_scored_properties
        import psycopg2 as _pg
        bg_conn = get_db()
        bg_cur = bg_conn.cursor()
        conn_broken = False
        try:
            total_saved = 0
            for c in city_list:
                try:
                    listings = scrape_all(city=c, transaction=tx)
                    if listings:
                        saved = save_to_db(bg_conn, listings)
                        total_saved += (saved or 0)
                        log.info(f"User scrape: saved {saved} for {c} ({tx})")
                except Exception as e:
                    log.error(f"User scrape failed for {c}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass
            log.info(f"User scrape complete: {total_saved} saved for {len(city_list)} cities")

            try:
                bg_cur.execute("""
                    SELECT * FROM search_profiles
                    WHERE user_id = %s AND is_active = TRUE
                    ORDER BY created_at DESC LIMIT 1
                """, (uid,))
                profile = bg_cur.fetchone()
                if profile:
                    profile = dict(profile)
                    bg_cur.execute("SELECT * FROM search_zones WHERE profile_id = %s", (profile['id'],))
                    zones = [dict(z) for z in bg_cur.fetchall()]

                    score_query = "SELECT * FROM properties WHERE is_active = TRUE"
                    score_params = []
                    if profile.get('transaction'):
                        score_query += " AND transaction = %s"
                        score_params.append(profile['transaction'])
                    if profile.get('budget_max'):
                        score_query += " AND (price IS NULL OR price <= %s)"
                        score_params.append(int(float(profile['budget_max']) * 1.3))
                    bg_cur.execute(score_query, score_params)
                    properties = bg_cur.fetchall()
                    scored = upsert_scored_properties(bg_cur, properties, profile, zones, user_id=uid)
                    bg_conn.commit()
                    log.info(f"User scrape scoring: {scored} properties scored for user {uid}")
            except Exception as e:
                log.error(f"User scrape scoring error: {e}", exc_info=True)
                try:
                    bg_conn.rollback()
                except Exception:
                    pass
        except (_pg.OperationalError, _pg.InterfaceError) as e:
            # Conn TLS pourrie → force close pour ne pas empoisonner le pool
            conn_broken = True
            log.error(f"User scrape bg DB transport error: {e}", exc_info=True)
        except Exception as e:
            log.error(f"User scrape bg error: {e}", exc_info=True)
        finally:
            try: bg_cur.close()
            except Exception: pass
            return_db(bg_conn, close=conn_broken)

    thread = threading.Thread(target=_bg_scrape, args=(cities, transaction, user_id))
    thread.daemon = True
    thread.start()

    return jsonify({
        "ok": True,
        "message": f"Scraping lancé en arrière-plan pour {len(cities)} ville(s)",
        "cities": cities
    })


@scraping_bp.route('/api/import', methods=['POST'])
@ingest_or_admin_required
def api_import():
    """Import scraped listings from local_scraper. Admin-only.

    Used by the operator-run local_scraper.py workflow which logs in as the
    admin to obtain a JWT, then POSTs scraped listings here. NOT a general
    user endpoint — see audit C1 (2026-05-04): a non-admin user posting
    listings would inject content into the global catalog visible to everyone.
    """
    key_auth = bool(getattr(request, 'ingest_key_auth', False))
    if not key_auth and not check_rate_limit(f"import:user:{request.user_id}", _IMPORT_PER_USER_MIN, _IMPORT_PER_USER_HOUR):
        return rate_limited_response()

    from scrapers import save_to_db

    data = request.json or {}
    listings = data.get('listings', [])
    # Options du script local (phase 2) : desactiver les annonces d'une
    # source/canton absentes de seen_ids, et rescorer les profils ensuite.
    deactivate = data.get('deactivate') if isinstance(data.get('deactivate'), dict) else None
    want_rescore = bool(data.get('rescore'))

    if not listings and not deactivate and not want_rescore:
        return jsonify({"error": "No listings provided"}), 400
    if not isinstance(listings, list) or len(listings) > 500:
        return jsonify({"error": "Listings invalides (max 500)"}), 400

    required_fields = ['title', 'source', 'source_url']
    for i, listing in enumerate(listings):
        if not isinstance(listing, dict):
            return jsonify({"error": f"Listing {i} invalide"}), 400
        for field in required_fields:
            if not listing.get(field):
                return jsonify({"error": f"Listing {i}: champ '{field}' requis"}), 400

        # URL scheme + length validation (defense in depth, even though admin-only)
        source_url = _safe_http_url(listing.get('source_url'))
        if not source_url:
            return jsonify({"error": f"Listing {i}: source_url doit être http(s) (max {_IMPORT_MAX_URL_LEN} chars)"}), 400
        listing['source_url'] = source_url

        # Cap free-text fields to schema-friendly lengths
        title = listing.get('title') or ''
        if not isinstance(title, str) or len(title) > _IMPORT_MAX_TITLE_LEN:
            return jsonify({"error": f"Listing {i}: title invalide (max {_IMPORT_MAX_TITLE_LEN} chars)"}), 400
        desc = listing.get('description')
        if isinstance(desc, str) and len(desc) > _IMPORT_MAX_DESC_LEN:
            listing['description'] = desc[:_IMPORT_MAX_DESC_LEN]

        # Filter images: only http(s), capped count
        raw_images = listing.get('images') or []
        if not isinstance(raw_images, list):
            return jsonify({"error": f"Listing {i}: images doit être une liste"}), 400
        clean_images = [u for u in (_safe_http_url(img) for img in raw_images) if u]
        listing['images'] = clean_images[:_IMPORT_MAX_IMAGES]

    conn = get_db()
    deactivated = 0
    try:
        saved = save_to_db(conn, listings) if listings else 0
        if deactivate:
            src = str(deactivate.get('source') or '')[:50]
            canton = str(deactivate.get('canton') or '')[:5].upper()
            seen_ids = [str(x)[:255] for x in (deactivate.get('seen_ids') or [])][:20000]
            if src and canton and seen_ids:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE properties SET is_active = FALSE
                    WHERE source = %s AND is_active = TRUE AND canton = %s
                      AND NOT (external_id = ANY(%s))
                """, (src, canton, seen_ids))
                deactivated = cur.rowcount
                conn.commit()
                cur.close()
                log.info(f"import: {deactivated} annonces {src}/{canton} desactivees")
    finally:
        return_db(conn)

    if want_rescore:
        def _rescore():
            c = None
            try:
                from scoring_engine import score_all_for_profile
                c = get_db()
                cur = c.cursor()
                cur.execute("SELECT id FROM search_profiles WHERE is_active = TRUE")
                ids = [r['id'] for r in cur.fetchall()]
                cur.close()
                for pid in ids:
                    try:
                        score_all_for_profile(c, pid)
                    except Exception as e:
                        log.error(f"import rescore profil {pid}: {e}")
                        c.rollback()
                log.info(f"import: {len(ids)} profils rescores")
            except Exception as e:
                log.error(f"import rescore: {e}")
            finally:
                if c is not None:
                    try:
                        return_db(c)
                    except Exception:
                        pass
        threading.Thread(target=_rescore, daemon=True).start()

    return jsonify({
        "ok": True,
        "received": len(listings),
        "saved": saved,
        "deactivated": deactivated,
        "rescore_started": want_rescore,
    })


@scraping_bp.route('/api/score', methods=['POST'])
@token_required
def api_score():
    """Score all properties for the current user's profile."""
    from scoring_engine import upsert_scored_properties
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT * FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (request.user_id,))
        profile = cur.fetchone()
        if not profile:
            return jsonify({"error": "Aucun profil trouvé"}), 400

        profile = dict(profile)

        cur.execute("SELECT * FROM search_zones WHERE profile_id = %s", (profile['id'],))
        zones = [dict(z) for z in cur.fetchall()]

        score_query = "SELECT * FROM properties WHERE is_active = TRUE"
        score_params = []
        if profile.get('transaction'):
            score_query += " AND transaction = %s"
            score_params.append(profile['transaction'])
        zone_cantons = [z.get('canton', '').upper() for z in zones if z.get('canton')]
        if zone_cantons:
            placeholders = ','.join(['%s'] * len(zone_cantons))
            score_query += f" AND (canton IN ({placeholders}) OR canton IS NULL OR canton = '')"
            score_params.extend(zone_cantons)
        if profile.get('budget_max'):
            score_query += " AND (price IS NULL OR price <= %s)"
            score_params.append(int(float(profile['budget_max']) * 1.3))
        cur.execute(score_query, score_params)
        properties = cur.fetchall()
        scored = upsert_scored_properties(cur, properties, profile, zones, user_id=request.user_id)

        conn.commit()
        return jsonify({"ok": True, "scored": scored, "profile_id": profile['id']})
    except Exception as e:
        log.error(f"Score error: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Erreur lors du scoring. Vérifiez votre profil."}), 500
    finally:
        cur.close()
        return_db(conn)


@scraping_bp.route('/api/scrape/debug', methods=['GET'])
def api_scrape_debug():
    """Test ScrapingBee with a single Homegate request. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from scrapers import _sb_get, SCRAPINGBEE_KEY

        city = request.args.get('city', 'Lausanne')
        sb_key = os.environ.get('SCRAPINGBEE_API_KEY', 'NOT SET')

        results = {
            "scrapingbee_key_set": bool(sb_key and sb_key != 'NOT SET'),
            "key_from_scrapers_set": bool(SCRAPINGBEE_KEY),
        }

        try:
            status_test, html_test = _sb_get("https://httpbin.org/get", render_js=False)
            results["httpbin_test"] = {
                "http_status": status_test,
                "html_size": len(html_test) if html_test else 0,
                "html_start": html_test[:200] if html_test else '',
            }
        except Exception as e:
            log.error(f"httpbin test error: {e}")
            results["httpbin_test"] = {"error": type(e).__name__}

        try:
            from scrapers import scrape_homegate
            listings = scrape_homegate(city=city, transaction="location", max_pages=1)
            results["homegate_scraper"] = {
                "total_listings": len(listings),
                "sample": [
                    {
                        "id": l["external_id"],
                        "title": l["title"][:80],
                        "price": l["price"],
                        "rooms": l["rooms"],
                        "surface": l["surface"],
                        "address": l["address"][:60],
                        "url": l["source_url"],
                    }
                    for l in listings[:5]
                ]
            }
        except Exception as e:
            log.error(f"Homegate scraper error: {e}", exc_info=True)
            results["homegate_scraper"] = {"error": type(e).__name__}

        return jsonify(results)

    except Exception as e:
        log.error(f"Scrape debug error: {e}", exc_info=True)
        return jsonify({"error": "Internal error"}), 500


@scraping_bp.route('/api/scrape/test', methods=['GET'])
def api_scrape_test():
    """Debug endpoint: test raw HTTP responses from each portal. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403
    import requests as req

    city = request.args.get('city', 'Lausanne')
    tx = request.args.get('transaction', 'location')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'fr-CH,fr;q=0.9',
    }
    results = {}

    for endpoint_name, url in [
        ('Flatfox_v1_flat', 'https://flatfox.ch/api/v1/flat/'),
        ('Flatfox_v1_public', 'https://flatfox.ch/api/v1/public/listings/'),
        ('Flatfox_search', 'https://flatfox.ch/api/v1/public/search/listings/'),
    ]:
        try:
            r = req.get(url, headers=headers,
                        params={'city': city, 'offer_type': 'RENT', 'ordering': '-created', 'limit': 3},
                        timeout=15)
            body = r.text[:500]
            is_json = r.headers.get('content-type', '').startswith('application/json')
            results[endpoint_name] = {"http": r.status_code, "is_json": is_json, "body_preview": body}
        except Exception as e:
            log.error(f"{endpoint_name} test error: {e}")
            results[endpoint_name] = {"error": type(e).__name__}

    try:
        r = req.get(f'https://www.homegate.ch/api/search/rent',
                     headers={**headers, 'Referer': 'https://www.homegate.ch/'},
                     params={'loc': city, 'ag': 3, 'o': 'dateCreated-desc'},
                     timeout=15)
        body = r.text[:500]
        results['Homegate_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        log.error(f"Homegate API test error: {e}")
        results['Homegate_API'] = {"error": type(e).__name__}

    try:
        slug = city.lower().replace(' ', '-')
        r = req.get(f'https://www.homegate.ch/rent/real-estate/city-{slug}/matching-list',
                     headers=headers, timeout=15)
        has_next = '__NEXT_DATA__' in r.text
        next_snippet = ''
        if has_next:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.{0,300})', r.text)
            next_snippet = m.group(1) if m else ''
        results['Homegate_Page'] = {"http": r.status_code, "has_NEXT_DATA": has_next, "snippet": next_snippet[:300], "page_size": len(r.text)}
    except Exception as e:
        log.error(f"Homegate Page test error: {e}")
        results['Homegate_Page'] = {"error": type(e).__name__}

    try:
        slug = city.lower().replace(' ', '-')
        r = req.get(f'https://www.immoscout24.ch/en/real-estate/rent/city-{slug}',
                     headers=headers, timeout=15)
        has_initial = '__INITIAL_STATE__' in r.text
        results['ImmoScout24'] = {"http": r.status_code, "has_INITIAL_STATE": has_initial, "page_size": len(r.text)}
    except Exception as e:
        log.error(f"ImmoScout24 test error: {e}")
        results['ImmoScout24'] = {"error": type(e).__name__}

    try:
        payload = {'DealType': 10, 'Keyword': city, 'LocationSearchString': city, 'Sort': 4, 'Page': 1, 'PageSize': 3, 'RootPropertyTypes': [1]}
        r = req.post('https://api.comparis.ch/realestate/v1/search/list',
                      headers={**headers, 'Content-Type': 'application/json'},
                      json=payload, timeout=15)
        body = r.text[:500]
        results['Comparis_API'] = {"http": r.status_code, "body_preview": body}
    except Exception as e:
        log.error(f"Comparis API test error: {e}")
        results['Comparis_API'] = {"error": type(e).__name__}

    return jsonify({"city": city, "transaction": tx, "results": results})


@scraping_bp.route('/api/cron/scrape', methods=['POST', 'GET'])
def api_cron_scrape():
    """Trigger scraping via cron. Protected by CRON_SECRET."""
    if not _require_cron_secret():
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT sp.*, u.email,
                   json_agg(json_build_object(
                       'city', sz.city, 'canton', sz.canton,
                       'radius_km', sz.radius_km
                   )) as zones
            FROM search_profiles sp
            JOIN users u ON u.id = sp.user_id
            LEFT JOIN search_zones sz ON sz.profile_id = sp.id
            WHERE sp.is_active = TRUE AND u.is_active = TRUE
            GROUP BY sp.id, u.email
        """)
        profiles = [dict(p) for p in cur.fetchall()]

        if not profiles:
            return jsonify({"ok": True, "message": "No active profiles"})

        CANTON_CITIES = {
            'NE': [
                'Neuchâtel', 'La Chaux-de-Fonds', 'Le Locle',
                'Peseux', 'Boudry', 'Cortaillod', 'Colombier',
                'Val-de-Travers', 'Milvignes', 'Val-de-Ruz',
                'Hauterive', 'Saint-Blaise', 'Corcelles-Cormondrèche',
                'La Tène', 'Le Landeron', 'Bevaix', 'Marin-Epagnier',
                'Fleurier', 'Couvet', 'Cernier', 'Fontainemelon',
            ],
            'VD': [
                'Lausanne', 'Montreux', 'Vevey', 'Nyon', 'Morges',
                'Yverdon-les-Bains', 'Renens', 'Prilly', 'Pully',
                'Ecublens', 'Lutry', 'Savigny',
            ],
            'GE': [
                'Genève', 'Carouge', 'Meyrin', 'Lancy',
                'Vernier', 'Onex', 'Thônex',
            ],
            'VS': ['Sion', 'Sierre', 'Martigny', 'Monthey'],
            'FR': ['Fribourg', 'Bulle'],
            'BE': ['Berne', 'Bienne'],
            'JU': ['Delémont', 'Porrentruy'],
        }

        scrape_targets = set()
        transactions_needed = set()
        cantons_needed = set()
        for p in profiles:
            zones = p.get('zones', [])
            tx = p.get('transaction', 'location')
            transactions_needed.add(tx)
            if zones:
                for z in zones:
                    if isinstance(z, dict) and z.get('city'):
                        city_norm = z['city'].strip().title()
                        scrape_targets.add((city_norm, tx))
                        ct = (z.get('canton') or '').upper()
                        if ct:
                            cantons_needed.add(ct)
                        else:
                            from scrapers import CITY_CANTONS
                            ct = CITY_CANTONS.get(city_norm.lower(), '')
                            if ct:
                                cantons_needed.add(ct.upper())

        for tx in transactions_needed:
            for canton in cantons_needed:
                for city in CANTON_CITIES.get(canton, []):
                    scrape_targets.add((city, tx))

        targets_list = [{"city": c, "transaction": t} for c, t in scrape_targets]

    finally:
        cur.close()
        return_db(conn)

    def _background_scrape(targets, profiles_data):
        from scrapers import scrape_all, save_to_db
        from scoring_engine import upsert_scored_properties
        import psycopg2 as _pg

        bg_conn = get_db()
        bg_cur = bg_conn.cursor()
        conn_broken = False
        try:
            total_saved = 0
            for city, transaction in targets:
                try:
                    listings = scrape_all(city=city, transaction=transaction)
                    if listings:
                        saved = save_to_db(bg_conn, listings)
                        total_saved += (saved or 0)
                        log.info(f"Cron: saved {saved} for {city} ({transaction})")
                except Exception as e:
                    log.error(f"Cron: scrape failed for {city}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass

            scored_total = 0
            for p in profiles_data:
                try:
                    zones_data = [dict(z) for z in (p.get('zones') or []) if isinstance(z, dict) and z.get('city')]

                    bg_cur.execute("SELECT * FROM properties WHERE is_active = TRUE AND transaction = %s",
                                (p.get('transaction', 'location'),))
                    properties = bg_cur.fetchall()
                    scored_total += upsert_scored_properties(bg_cur, properties, p, zones_data, user_id=p['user_id'])
                    bg_conn.commit()
                except Exception as e:
                    log.error(f"Cron: scoring failed for profile {p.get('id')}: {e}")
                    try:
                        bg_conn.rollback()
                    except Exception:
                        pass

            bg_cur.execute("""
                UPDATE properties SET is_active = FALSE
                WHERE scraped_at < NOW() - INTERVAL '30 days' AND is_active = TRUE
            """)
            bg_conn.commit()

            log.info(f"Cron complete: saved={total_saved}, scored={scored_total}")

        except (_pg.OperationalError, _pg.InterfaceError) as e:
            conn_broken = True
            log.error(f"Cron background DB transport error: {e}", exc_info=True)
        except Exception as e:
            log.error(f"Cron background error: {e}", exc_info=True)
            try:
                bg_conn.rollback()
            except Exception:
                pass
        finally:
            try: bg_cur.close()
            except Exception: pass
            return_db(bg_conn, close=conn_broken)

    thread = threading.Thread(target=_background_scrape, args=(scrape_targets, profiles))
    thread.daemon = True
    thread.start()

    return jsonify({
        "ok": True,
        "message": f"Scraping lancé en arrière-plan pour {len(scrape_targets)} ville(s)",
        "targets": targets_list
    })
