"""Bon Home — Properties, favorites, stats Blueprint."""
import re
import io
import csv
import json
import logging

from flask import Blueprint, jsonify, request, make_response

from db import get_db, return_db
from auth import token_required, token_required_query_ok, plan_feature
from helpers import days_since

log = logging.getLogger('lou-app')
properties_bp = Blueprint('properties', __name__)


@properties_bp.route('/api/properties', methods=['GET'])
@token_required
def get_properties():
    user_id = request.user_id
    sort = request.args.get('sort', 'score')
    min_score = int(request.args.get('min_score', 0))
    new_only = request.args.get('new_only', '').lower() in ('true', '1', 'yes')
    include_no_price = request.args.get('include_no_price', '').lower() in ('true', '1', 'yes')
    include_nearby = request.args.get('include_nearby', '').lower() in ('true', '1', 'yes')
    page = int(request.args.get('page', 1))
    # The map view needs all matching properties in one page (up to 500).
    # Normal list views are capped at 50 per page to keep responses fast.
    max_per_page = 500 if request.args.get('view') == 'map' else 50
    per_page = min(int(request.args.get('per_page', 20)), max_per_page)
    offset = (page - 1) * per_page

    # Tri par proximité: zone match first, then distance, then score
    proximity_order = (
        "CASE WHEN sp.score_zone >= 80 THEN 0 ELSE 1 END, "
        "sp.distance_km ASC NULLS LAST, sp.total_score DESC"
    )
    # Default sort: pure total_score descending, then distance as tiebreaker.
    # This ensures a 94A always appears before an 85A regardless of distance.
    order_map = {
        'score': (
            "sp.total_score DESC, sp.distance_km ASC NULLS LAST"
        ),
        'proximity': proximity_order,
        'price_asc': 'p.price ASC',
        'price_desc': 'p.price DESC',
        'newest': 'p.published_at DESC NULLS LAST',
        'surface': 'p.surface DESC NULLS LAST',
    }
    order = order_map.get(sort) or order_map['score']

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, transaction FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        profile_row = cur.fetchone()
        user_transaction = profile_row['transaction'] if profile_row else None
        active_profile_id = profile_row['id'] if profile_row else None

        # Diagnostic: log the user's search zones so we can debug geo filter issues
        if active_profile_id:
            cur.execute("SELECT city, canton, radius_km, latitude, longitude FROM search_zones WHERE profile_id = %s", (active_profile_id,))
            _debug_zones = [dict(z) for z in cur.fetchall()]
            log.info(f"User {user_id} profile {active_profile_id} zones: {_debug_zones}")

        tx_filter = ""
        tx_params = [user_id, user_id, min_score]
        if active_profile_id:
            tx_filter += " AND sp.profile_id = %s"
            tx_params.append(active_profile_id)
        if user_transaction:
            tx_filter += " AND p.transaction = %s"
            tx_params.append(user_transaction)
        if new_only:
            tx_filter += " AND p.first_seen_at > NOW() - INTERVAL '24 hours'"

        # Filtrage prix : par défaut on ignore price <= 0 / NULL (liens cassés, annonces bizarres).
        # Seuil historique 1000 CHF conservé en mode strict.
        if include_no_price:
            price_filter = "(p.price IS NULL OR p.price > 0)"
        else:
            price_filter = "p.price > 1000"

        # TODO: activer quand PRICING_ENABLED = True
        # from auth import _get_user_plan
        # from plans import check_limit
        # user_plan = _get_user_plan(user_id)
        # if not check_limit(user_plan, 'properties_visible', offset):
        #     return jsonify({"limited": True, "total": total, "upgrade_url": "/pricing"}), 200

        # Strict zone filter: only properties within the user's requested radius.
        # score_zone >= 80 corresponds to "inside target_radius" in score_zone()
        # (progressive scoring 100→80 within radius; outside tops out at 70).
        # Also keeps exact city matches with no GPS (score=90) and city+city_match bonus.
        # When include_nearby=true, relax to score_zone >= 40 to surface properties
        # just outside the radius (scored 40–79 by score_zone).
        zone_threshold = 40 if include_nearby else 80
        cur.execute(f"""
            SELECT p.*, sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   EXISTS(SELECT 1 FROM favorites f WHERE f.user_id = %s AND f.property_id = p.id) as is_favorite,
                   p.first_seen_at,
                   (SELECT json_agg(json_build_object('old_price', ph.old_price, 'new_price', ph.new_price, 'change_pct', ph.change_pct, 'detected_at', ph.detected_at) ORDER BY ph.detected_at DESC) FROM price_history ph WHERE ph.property_id = p.id) as price_changes
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND sp.total_score >= %s AND p.is_active = TRUE
                  AND sp.score_zone >= {zone_threshold}
                  AND {price_filter}{tx_filter}
            ORDER BY {order}
        """, tx_params)
        properties = [dict(r) for r in cur.fetchall()]

        # Diagnostic: log cities and their score_zone values
        _city_zones = {}
        for _p in properties:
            _c = _p.get('city', '?')
            _sz = _p.get('score_zone', 0)
            if _c not in _city_zones:
                _city_zones[_c] = []
            _city_zones[_c].append(_sz)
        log.info(f"Geo distribution (score_zone): {({c: f'{len(v)} props, zone={min(v)}-{max(v)}' for c, v in sorted(_city_zones.items())})}")

        # Count "nearby" properties (just outside radius) so the frontend can
        # offer an "élargir la zone" action when the strict count is small.
        # tx_params starts with [user_id(favorites), user_id(WHERE), min_score, ...]
        # but this query has no favorites sub-select, so we skip the first element.
        nearby_available = 0
        if not include_nearby:
            nearby_params = tx_params[1:]  # drop the favorites user_id
            cur.execute(f"""
                SELECT COUNT(*) AS n
                FROM scored_properties sp
                JOIN properties p ON p.id = sp.property_id
                WHERE sp.user_id = %s AND sp.total_score >= %s AND p.is_active = TRUE
                      AND sp.score_zone >= 40 AND sp.score_zone < 80
                      AND {price_filter}{tx_filter}
            """, nearby_params)
            row = cur.fetchone()
            nearby_available = int(row['n']) if row and row.get('n') is not None else 0

        # Cross-portal merge (kept identical to legacy logic)
        def _street_tokens(addr):
            if not addr:
                return set()
            tokens = re.findall(r'[A-Za-zÀ-ÿ]{4,}', str(addr).lower())
            stop = {
                'rue', 'avenue', 'route', 'chemin', 'place', 'boulevard', 'quai',
                'strasse', 'gasse', 'weg', 'platz', 'allee',
                'suisse', 'svizzera', 'canton', 'neuchatel', 'lausanne', 'geneve',
                'appartement', 'maison', 'immeuble', 'villa', 'studio',
            }
            return set(t for t in tokens if t not in stop)

        def _haversine_km(lat1, lng1, lat2, lng2):
            try:
                import math as _m
                R = 6371
                la1, lo1, la2, lo2 = float(lat1), float(lng1), float(lat2), float(lng2)
                dla = _m.radians(la2 - la1); dlo = _m.radians(lo2 - lo1)
                a = _m.sin(dla / 2) ** 2 + _m.cos(_m.radians(la1)) * _m.cos(_m.radians(la2)) * _m.sin(dlo / 2) ** 2
                return R * 2 * _m.atan2(_m.sqrt(a), _m.sqrt(1 - a))
            except Exception:
                return None

        def _can_merge(existing, new):
            lat1, lng1 = existing.get('latitude'), existing.get('longitude')
            lat2, lng2 = new.get('latitude'), new.get('longitude')
            if lat1 and lng1 and lat2 and lng2:
                d = _haversine_km(lat1, lng1, lat2, lng2)
                if d is not None:
                    if d <= 0.3:
                        return True
                    if d > 1.0:
                        return False
            pc1 = (existing.get('postal_code') or '').strip()
            pc2 = (new.get('postal_code') or '').strip()
            if pc1 and pc2:
                if pc1 != pc2:
                    return False
                return True
            t1 = _street_tokens(existing.get('address', ''))
            t2 = _street_tokens(new.get('address', ''))
            if t1 and t2:
                return len(t1 & t2) >= 1
            return False

        def _merge_keys(p):
            keys = []
            postal = (p.get('postal_code') or '').strip()
            price = int(p.get('price') or 0)
            rooms = p.get('rooms')
            rooms_norm = str(round(float(rooms) * 2) / 2) if rooms else ''
            surface = p.get('surface') or 0
            surface_bucket = round(surface / 5) * 5 if surface else 0
            city = (p.get('city') or '').lower().strip()

            if price and price < 10000:
                price_bucket = round(price / 50) * 50
            elif price:
                step = max(5000, int(price * 0.02))
                price_bucket = round(price / step) * step
            else:
                price_bucket = 0

            if postal and price_bucket and rooms_norm and surface_bucket:
                keys.append(f"npa:{postal}:{price_bucket}:{rooms_norm}:{surface_bucket}")
            if city and price_bucket and rooms_norm and surface_bucket:
                keys.append(f"city:{city}:{price_bucket}:{rooms_norm}:{surface_bucket}")
            if postal and price_bucket and rooms_norm:
                keys.append(f"nparooms:{postal}:{price_bucket}:{rooms_norm}")
            if city and price_bucket and rooms_norm:
                keys.append(f"cityrooms:{city}:{price_bucket}:{rooms_norm}")
            if postal and price_bucket and surface_bucket and not rooms_norm:
                keys.append(f"npasurf:{postal}:{price_bucket}:{surface_bucket}")
            if city and price_bucket and surface_bucket and not rooms_norm:
                keys.append(f"citysurf:{city}:{price_bucket}:{surface_bucket}")

            if not keys:
                title_norm = re.sub(r'[^a-z0-9]', '', (p.get('title') or '').lower())[:30]
                if title_norm:
                    keys.append(f"title:{city}:{price_bucket}:{title_norm}")
                elif price_bucket:
                    keys.append(f"priceonly:{city}:{price_bucket}")
            return keys

        merged = {}
        key_to_group = {}
        merge_debug = {}
        for p in properties:
            keys = _merge_keys(p)
            src = p.get('source') or 'unknown'

            group_id = None
            for k in keys:
                if k in key_to_group:
                    candidate = key_to_group[k]
                    if candidate in merged and _can_merge(merged[candidate], p):
                        group_id = candidate
                        break

            if group_id is None:
                group_id = p['id']
                merged[group_id] = p
                merged[group_id]['_all_sources'] = [{'source': p['source'] or '', 'url': p['source_url'] or ''}]
                merged[group_id]['_best_images'] = p['images'] or []
                for k in keys:
                    if k not in key_to_group:
                        key_to_group[k] = group_id
                merge_debug[group_id] = [f"{src}(id={p['id']},price={p.get('price')},rooms={p.get('rooms')},postal={p.get('postal_code')},city={p.get('city')})"]
            else:
                for k in keys:
                    if k not in key_to_group:
                        key_to_group[k] = group_id
                merge_debug[group_id].append(f"{src}(id={p['id']},price={p.get('price')},rooms={p.get('rooms')},postal={p.get('postal_code')},city={p.get('city')})")
                new_src = p['source'] or ''
                existing_sources = {s['source'] for s in merged[group_id]['_all_sources']}
                if new_src not in existing_sources:
                    merged[group_id]['_all_sources'].append({'source': new_src, 'url': p['source_url'] or ''})
                if p['images']:
                    existing_imgs = set(merged[group_id]['_best_images'])
                    for img in p['images']:
                        if img and img not in existing_imgs:
                            merged[group_id]['_best_images'].append(img)
                            existing_imgs.add(img)
                if (p['total_score'] or 0) > (merged[group_id]['total_score'] or 0):
                    old_sources = merged[group_id]['_all_sources']
                    old_images = merged[group_id]['_best_images']
                    merged[group_id] = p
                    merged[group_id]['_all_sources'] = old_sources
                    merged[group_id]['_best_images'] = old_images

        multi_source = {k: v for k, v in merge_debug.items() if len(v) > 1}
        if multi_source:
            log.info(f"Merge: {len(multi_source)} groups merged from {sum(len(v) for v in multi_source.values())} properties")
            for gid, sources in list(multi_source.items())[:5]:
                log.info(f"  Merged group {gid}: {sources}")
        log.info(f"Merge total: {len(properties)} properties -> {len(merged)} unique results")

        # Enrich with cross-portal sources stored in property_sources table.
        # These are recorded during scraping when a duplicate listing on another
        # portal is detected.  The in-memory merge above only sees rows in
        # scored_properties (one per property), so secondary portals would be
        # missing from all_sources without this step.
        merged_ids = [pid for pid in merged]
        if merged_ids:
            placeholders = ','.join(['%s'] * len(merged_ids))
            cur.execute(f"""
                SELECT property_id, source, source_url
                FROM property_sources
                WHERE property_id IN ({placeholders})
            """, merged_ids)
            for row in cur.fetchall():
                pid = row['property_id']
                if pid in merged:
                    existing = {s['source'] for s in merged[pid].get('_all_sources', [])}
                    if row['source'] and row['source'] not in existing:
                        merged[pid]['_all_sources'].append({
                            'source': row['source'],
                            'url': row['source_url'] or ''
                        })

        results = []
        for p in merged.values():
            all_sources = p.pop('_all_sources', [])
            best_images = p.pop('_best_images', [])
            results.append({
                'id': p['id'],
                'title': (p['title'] or '').strip() or (
                    f"{float(p['rooms'])} pcs, {p['surface']} m2" if p.get('rooms') and p.get('surface')
                    else f"{p.get('city') or 'Bien immobilier'}"
                ),
                'address': (p['address'] or '').strip(),
                'price': p['price'] or 0,
                'unit': f"{(p['currency'] or 'CHF').encode('ascii', 'ignore').decode()}/{(p['price_unit'] or 'mois').encode('ascii', 'ignore').decode()}",
                'rooms': float(p['rooms']) if p['rooms'] else 0,
                'surface': p['surface'] or 0,
                'floor': p['floor'],
                'features': p['features'] or [],
                'source': all_sources[0]['source'] if all_sources else '',
                'source_url': all_sources[0]['url'] if all_sources else '',
                'all_sources': all_sources,
                'description': (p.get('description') or '').strip(),
                'contact_name': p['contact_name'] or '',
                'contact_phone': p['contact_phone'] or '',
                'contact_email': p['contact_email'] or '',
                'images': best_images or p['images'] or [],
                'score': p['total_score'],
                'grade': p['grade'],
                'latitude': p.get('latitude'),
                'longitude': p.get('longitude'),
                'city': p.get('city'),
                'property_type': p.get('property_type') or '',
                'transaction': p.get('transaction') or '',
                'distance_km': float(p['distance_km']) if p['distance_km'] else None,
                'score_detail': {
                    'zone': p['score_zone'],
                    'budget': p['score_budget'],
                    'type': p['score_type'],
                    'surface': p['score_surface'],
                    'equipment': p['score_equipment'],
                    'freshness': p['score_freshness']
                },
                'published_at': p['published_at'].isoformat() if p['published_at'] else None,
                'is_favorite': p['is_favorite'],
                'first_seen_at': p['first_seen_at'].isoformat() if p.get('first_seen_at') else p['scraped_at'].isoformat() if p.get('scraped_at') else None,
                'days_online': days_since(p.get('first_seen_at') or p.get('scraped_at')),
                'price_drop': None,
            })

            price_changes = p.get('price_changes')
            if price_changes and isinstance(price_changes, list) and len(price_changes) > 0:
                latest = price_changes[0]
                if latest.get('change_pct') and latest['change_pct'] < 0:
                    results[-1]['price_drop'] = {
                        'old_price': latest['old_price'],
                        'new_price': latest['new_price'],
                        'change_pct': float(latest['change_pct']),
                        'detected_at': latest['detected_at'].isoformat() if hasattr(latest['detected_at'], 'isoformat') else str(latest['detected_at'])
                    }

        total = len(results)
        page_results = results[offset:offset + per_page]
        return jsonify({
            "properties": page_results,
            "total": total,
            "page": page,
            "per_page": per_page,
            "nearby_available": nearby_available,
            "include_nearby": include_nearby,
        })
    finally:
        cur.close()
        return_db(conn)


@properties_bp.route('/api/stats', methods=['GET'])
@token_required
def get_stats():
    user_id = request.user_id
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, transaction FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        prof = cur.fetchone()
        user_tx = prof['transaction'] if prof else None
        active_pid = prof['id'] if prof else None

        tx_filter = ""
        extra_params = []
        if active_pid:
            tx_filter += " AND sp.profile_id = %s"
            extra_params.append(active_pid)
        if user_tx:
            tx_filter += " AND p.transaction = %s"
            extra_params.append(user_tx)

        # Same strict zone filter as /api/properties so stats stay coherent
        # with the list shown on the dashboard.
        cur.execute(f"""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE p.first_seen_at > NOW() - INTERVAL '24 hours') as new_count,
                (SELECT COUNT(*) FROM favorites WHERE user_id = %s) as favorites
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id AND p.price > 1000
            WHERE sp.user_id = %s AND p.is_active = TRUE
                  AND sp.score_zone >= 80{tx_filter}
        """, [user_id, user_id] + extra_params)
        stats = dict(cur.fetchone())

        # v6.3.1 Bug #2 refinement: expose last_scored_at so the frontend can
        # distinguish "scoring in progress" (recent, <3 min) from "scoring done
        # but 0 match" (stale, older). Without this, users with overly narrow
        # criteria see "Lou est en chasse 1-3 min" forever.
        cur.execute("""
            SELECT MAX(scored_at) AS last_scored_at
            FROM scored_properties
            WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        last = row['last_scored_at'] if row else None
        stats['last_scored_at'] = last.isoformat() if last else None
        stats['has_profile'] = bool(active_pid)
        return jsonify(stats)
    finally:
        cur.close()
        return_db(conn)


@properties_bp.route('/api/favorite/<int:property_id>', methods=['POST'])
@token_required
def toggle_favorite(property_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM favorites WHERE user_id = %s AND property_id = %s",
            (request.user_id, property_id)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute("DELETE FROM favorites WHERE id = %s", (existing['id'],))
            action = 'removed'
        else:
            cur.execute(
                "INSERT INTO favorites (user_id, property_id) VALUES (%s, %s)",
                (request.user_id, property_id)
            )
            action = 'added'
        conn.commit()
        return jsonify({"ok": True, "action": action})
    finally:
        cur.close()
        return_db(conn)


@properties_bp.route('/api/favorite/<int:property_id>/note', methods=['PUT'])
@token_required
def update_favorite_note(property_id):
    """Update the note on a favorited property."""
    data = request.json or {}
    note = (data.get('note') or '').strip()[:500]
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE favorites SET notes = %s WHERE user_id = %s AND property_id = %s RETURNING id",
            (note or None, request.user_id, property_id)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Favori non trouvé"}), 404
        conn.commit()
        return jsonify({"ok": True})
    finally:
        cur.close()
        return_db(conn)


@properties_bp.route('/api/favorites', methods=['GET'])
@token_required
def get_favorites():
    """Get all favorites with full property data, scores, and notes."""
    user_id = request.user_id
    sort = request.args.get('sort', 'date')
    conn = get_db()
    cur = conn.cursor()
    try:
        order_map = {
            'date': 'f.created_at DESC',
            'score': 'sp.total_score DESC',
            'price_asc': 'p.price ASC NULLS LAST',
            'price_desc': 'p.price DESC NULLS LAST',
        }
        order = order_map.get(sort, 'f.created_at DESC')

        cur.execute(f"""
            SELECT p.*, f.notes as fav_note, f.created_at as fav_date,
                   sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   p.first_seen_at,
                   (SELECT json_agg(json_build_object('old_price', ph.old_price, 'new_price', ph.new_price, 'change_pct', ph.change_pct, 'detected_at', ph.detected_at) ORDER BY ph.detected_at DESC) FROM price_history ph WHERE ph.property_id = p.id) as price_changes
            FROM favorites f
            JOIN properties p ON p.id = f.property_id
            LEFT JOIN scored_properties sp ON sp.property_id = p.id AND sp.user_id = %s
            WHERE f.user_id = %s AND p.is_active = TRUE
            ORDER BY {order}
        """, (user_id, user_id))
        rows = [dict(r) for r in cur.fetchall()]

        formatted = []
        for p in rows:
            images = p.get('images') or []
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except Exception:
                    images = [images] if images else []

            title = (p.get('title') or '').strip()
            address = (p.get('address') or '').strip()

            d_online = 0
            if p.get('first_seen_at'):
                d_online = days_since(p['first_seen_at'])

            price_drop = None
            changes = p.get('price_changes')
            if changes and isinstance(changes, list) and len(changes) > 0:
                latest = changes[0]
                if latest.get('change_pct') and latest['change_pct'] < 0:
                    price_drop = {
                        'old_price': latest['old_price'],
                        'new_price': latest['new_price'],
                        'change_pct': latest['change_pct']
                    }

            item = {
                'id': p['id'],
                'title': title,
                'address': address,
                'city': p.get('city'),
                'price': p.get('price'),
                'unit': p.get('unit'),
                'rooms': p.get('rooms'),
                'surface': p.get('surface'),
                'floor': p.get('floor'),
                'images': images,
                'source': p.get('source'),
                'source_url': p.get('source_url'),
                'transaction': p.get('transaction'),
                'description': p.get('description'),
                'features': p.get('features'),
                'score': p.get('total_score') or 0,
                'grade': p.get('grade') or 'D',
                'latitude': p.get('latitude'),
                'longitude': p.get('longitude'),
                'distance_km': p.get('distance_km'),
                'is_favorite': True,
                'fav_note': p.get('fav_note') or '',
                'fav_date': p.get('fav_date').isoformat() if p.get('fav_date') else None,
                'days_online': d_online,
                'price_drop': price_drop,
                'score_detail': {
                    'zone': p.get('score_zone') or 0,
                    'budget': p.get('score_budget') or 0,
                    'type': p.get('score_type') or 0,
                    'surface': p.get('score_surface') or 0,
                    'equipment': p.get('score_equipment') or 0,
                    'freshness': p.get('score_freshness') or 0,
                },
            }
            formatted.append(item)

        return jsonify({"favorites": formatted, "total": len(formatted)})
    finally:
        cur.close()
        return_db(conn)


@properties_bp.route('/api/favorites/export', methods=['GET'])
@token_required_query_ok
@plan_feature('export_csv')
def export_favorites():
    """Export favorites as CSV (plan-gated when PRICING_ENABLED=True).
    Uses token_required_query_ok because the frontend opens this via
    window.open(...) which can't set Authorization headers."""
    user_id = request.user_id
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.title, p.address, p.city, p.price, p.unit, p.rooms, p.surface,
                   p.source, p.source_url, p.transaction,
                   sp.total_score, sp.grade, f.notes, f.created_at as fav_date
            FROM favorites f
            JOIN properties p ON p.id = f.property_id
            LEFT JOIN scored_properties sp ON sp.property_id = p.id AND sp.user_id = %s
            WHERE f.user_id = %s AND p.is_active = TRUE
            ORDER BY f.created_at DESC
        """, (user_id, user_id))
        rows = cur.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Titre', 'Adresse', 'Ville', 'Prix', 'Unite', 'Pieces', 'Surface m2',
                         'Source', 'URL', 'Transaction', 'Score', 'Grade', 'Notes', 'Date favori'])
        for r in rows:
            title = (r.get('title') or '').strip()
            writer.writerow([
                title, r.get('address'), r.get('city'), r.get('price'), r.get('unit'),
                r.get('rooms'), r.get('surface'), r.get('source'), r.get('source_url'),
                r.get('transaction'), r.get('total_score'), r.get('grade'),
                r.get('notes') or '', r.get('fav_date', '')
            ])

        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename=favoris-bonhome.csv'
        return response
    finally:
        cur.close()
        return_db(conn)
