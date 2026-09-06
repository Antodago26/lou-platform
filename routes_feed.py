"""Bon Home : feed mobile (un bien par ecran) et gestes de swipe.

GET  /api/feed          biens scores pour l'utilisateur, non encore swipes
POST /api/swipe         {property_id, action: like|pass|skip}
POST /api/swipe/undo    annule le dernier geste

Le feed reutilise la fusion multi-portails et le format JSON de
routes_properties : un bien a la meme forme partout dans l'app.
"""
import logging

from flask import Blueprint, jsonify, request

from db import get_db, return_db
from auth import token_required
from routes_properties import (
    _merge_cross_portal, _enrich_with_property_sources, _format_property,
)

log = logging.getLogger('lou-app')
feed_bp = Blueprint('feed', __name__)

_ACTIONS = ('like', 'pass', 'skip')
_SKIP_TTL = "7 days"        # un bien skippe revient apres une semaine
_FEED_MAX = 30


# ------------------------------------------------------------------
# Lou : une phrase par bien, sans appel API. Construite depuis les
# sous-scores et les equipements. Le ton est celui de Lou : direct,
# une qualite, puis le bemol s'il y en a un.
# ------------------------------------------------------------------
_FEATURE_LABELS = [
    ('balcon', 'Balcon'), ('terrasse', 'Terrasse'), ('jardin', 'Jardin'),
    ('vue', 'Belle vue'), ('parking', 'Parking inclus'), ('garage', 'Garage'),
    ('ascenseur', 'Ascenseur'), ('lave-vaisselle', 'Lave-vaisselle'),
    ('cheminée', 'Cheminée'), ('cheminee', 'Cheminée'), ('meublé', 'Meublé'),
    ('meuble', 'Meublé'), ('minergie', 'Minergie'),
]


def _first_feature(features):
    feats = [str(f).lower() for f in (features or [])]
    for key, label in _FEATURE_LABELS:
        if any(key in f for f in feats):
            return label
    return None


def lou_note(p):
    """p : dict au format _format_property (score_detail, distance_km,
    features, days_online). Renvoie une phrase courte."""
    d = p.get('score_detail') or {}
    budget = d.get('budget')          # None = inconnu, pas 0
    zone = d.get('zone') or 0
    surface = d.get('surface') or 0
    dist = p.get('distance_km')
    days = p.get('days_online')

    good = []
    feat = _first_feature(p.get('features'))
    if feat:
        good.append(feat.lower() if good else feat)
    if budget is not None and budget >= 90:
        good.append('dans ton budget')
    elif budget is not None and budget >= 70:
        good.append('un peu au-dessus de ton budget')
    if zone >= 95:
        good.append('en plein dans ta zone')
    elif dist is not None and dist > 0:
        good.append(f"à {dist:.0f} km de ta zone")

    if not good:
        good.append('correspond à tes critères')

    first = good[0]
    first = first[0].upper() + first[1:]
    sentence = first + (', ' + ', '.join(good[1:]) if len(good) > 1 else '') + '.'

    bemol = None
    if budget is not None and budget < 70:
        bemol = 'nettement au-dessus de ton budget'
    elif p.get('surface') and surface and surface < 45:
        # 50 = score neutre quand le profil n'a pas de critere de surface :
        # on ne parle de surface que si elle est vraiment en dessous.
        bemol = 'surface un peu juste'
    elif dist is not None and dist > 8:
        bemol = f"{dist:.0f} km de trajet"
    if bemol:
        sentence += f" Seul bémol : {bemol}."
    elif days == 0:
        sentence += ' Publié aujourd\'hui, sois rapide.'
    elif days == 1:
        sentence += ' Publié hier.'
    return sentence


# ------------------------------------------------------------------
# GET /api/feed
# ------------------------------------------------------------------
@feed_bp.route('/api/feed', methods=['GET'])
@token_required
def get_feed():
    user_id = request.user_id
    try:
        limit = max(1, min(int(request.args.get('limit', 10)), _FEED_MAX))
    except ValueError:
        limit = 10
    include_nearby = request.args.get('include_nearby', '').lower() in ('true', '1', 'yes')
    zone_threshold = 40 if include_nearby else 80

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, transaction FROM search_profiles
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        profile = cur.fetchone()
        if not profile:
            return jsonify({"items": [], "remaining": 0, "new_today": 0,
                            "seen_today": 0, "has_profile": False})

        params = [user_id, user_id, profile['id'], user_id]
        tx_filter = ""
        if profile['transaction']:
            tx_filter = " AND p.transaction = %s"
            params.append(profile['transaction'])

        # Exclusion : like/pass pour toujours, skip pendant _SKIP_TTL.
        cur.execute(f"""
            SELECT p.*, sp.total_score, sp.grade, sp.distance_km,
                   sp.score_zone, sp.score_budget, sp.score_type,
                   sp.score_surface, sp.score_equipment, sp.score_freshness,
                   EXISTS(SELECT 1 FROM favorites f WHERE f.user_id = %s AND f.property_id = p.id) AS is_favorite,
                   p.first_seen_at,
                   NULL::json AS price_changes
            FROM scored_properties sp
            JOIN properties p ON p.id = sp.property_id
            WHERE sp.user_id = %s AND sp.profile_id = %s
                  AND p.is_active = TRUE AND p.price > 0
                  AND sp.score_zone >= {zone_threshold}
                  AND NOT EXISTS (
                      SELECT 1 FROM swipes s
                      WHERE s.user_id = %s AND s.property_id = p.id
                        AND (s.action <> 'skip' OR s.created_at > NOW() - INTERVAL '{_SKIP_TTL}')
                  )
                  {tx_filter}
            ORDER BY sp.total_score DESC, p.first_seen_at DESC NULLS LAST
        """, params)
        rows = [dict(r) for r in cur.fetchall()]

        merged = _merge_cross_portal(rows)
        _enrich_with_property_sources(cur, merged)
        results = [_format_property(p) for p in merged.values()]
        for r in results:
            r['lou_note'] = lou_note(r)
            r['is_new'] = (r.get('days_online') is not None and r['days_online'] <= 1)

        cur.execute("""
            SELECT COUNT(*) AS n FROM swipes
            WHERE user_id = %s AND created_at > NOW() - INTERVAL '24 hours'
        """, (user_id,))
        seen_today = int(cur.fetchone()['n'])

        return jsonify({
            "items": results[:limit],
            "remaining": len(results),
            "new_today": sum(1 for r in results if r['is_new']),
            "seen_today": seen_today,
            "has_profile": True,
            "include_nearby": include_nearby,
        })
    finally:
        cur.close()
        return_db(conn)


# ------------------------------------------------------------------
# POST /api/swipe
# ------------------------------------------------------------------
def validate_swipe(data):
    """Renvoie (property_id, action) ou (None, message d'erreur)."""
    if not isinstance(data, dict):
        return None, "Corps JSON attendu"
    action = str(data.get('action') or '').strip().lower()
    if action not in _ACTIONS:
        return None, "action doit être like, pass ou skip"
    try:
        property_id = int(data.get('property_id'))
    except (TypeError, ValueError):
        return None, "property_id invalide"
    if property_id <= 0:
        return None, "property_id invalide"
    return (property_id, action), None


@feed_bp.route('/api/swipe', methods=['POST'])
@token_required
def post_swipe():
    user_id = request.user_id
    parsed, err = validate_swipe(request.get_json(silent=True))
    if err:
        return jsonify({"error": err}), 400
    property_id, action = parsed

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM properties WHERE id = %s", (property_id,))
        if not cur.fetchone():
            return jsonify({"error": "Bien introuvable"}), 404

        cur.execute("""
            INSERT INTO swipes (user_id, property_id, action)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, property_id)
            DO UPDATE SET action = EXCLUDED.action, created_at = NOW()
        """, (user_id, property_id, action))

        favorite = False
        if action == 'like':
            cur.execute("""
                INSERT INTO favorites (user_id, property_id) VALUES (%s, %s)
                ON CONFLICT (user_id, property_id) DO NOTHING
            """, (user_id, property_id))
            favorite = True
        elif action == 'pass':
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND property_id = %s",
                        (user_id, property_id))
        conn.commit()
        return jsonify({"ok": True, "property_id": property_id,
                        "action": action, "favorite": favorite})
    except Exception as e:
        conn.rollback()
        log.error(f"swipe failed user={user_id} prop={property_id}: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)


@feed_bp.route('/api/swipe/undo', methods=['POST'])
@token_required
def undo_swipe():
    user_id = request.user_id
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM swipes
            WHERE id = (SELECT id FROM swipes WHERE user_id = %s
                        ORDER BY created_at DESC, id DESC LIMIT 1)
            RETURNING property_id, action
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            conn.commit()
            return jsonify({"ok": False, "error": "Rien à annuler"}), 404
        if row['action'] == 'like':
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND property_id = %s",
                        (user_id, row['property_id']))
        conn.commit()
        return jsonify({"ok": True, "property_id": row['property_id'],
                        "action": row['action']})
    except Exception as e:
        conn.rollback()
        log.error(f"undo swipe failed user={user_id}: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)
