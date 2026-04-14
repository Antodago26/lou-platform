"""Bon Home — Alerts settings Blueprint."""
import logging

from flask import Blueprint, jsonify, request

from db import get_db, return_db
from auth import token_required

log = logging.getLogger('lou-app')
alerts_bp = Blueprint('alerts', __name__)


@alerts_bp.route('/api/alerts', methods=['GET'])
@token_required
def get_alerts():
    """Get user's alert settings (auto-create if none exists)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, frequency, min_score, is_active, channel, last_sent, created_at FROM alerts WHERE user_id = %s LIMIT 1",
            (request.user_id,)
        )
        alert = cur.fetchone()
        if not alert:
            cur.execute(
                "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at LIMIT 1",
                (request.user_id,)
            )
            profile = cur.fetchone()
            profile_id = profile['id'] if profile else None
            cur.execute("""
                INSERT INTO alerts (user_id, profile_id, channel, frequency, min_score, is_active)
                VALUES (%s, %s, 'email', 'daily', 70, TRUE)
                RETURNING id, frequency, min_score, is_active, channel, last_sent, created_at
            """, (request.user_id, profile_id))
            alert = cur.fetchone()
            conn.commit()
        return jsonify({
            "id": alert['id'],
            "frequency": alert['frequency'] if alert['is_active'] else 'off',
            "min_score": alert['min_score'],
            "is_active": alert['is_active'],
            "channel": alert['channel'],
            "last_sent": alert['last_sent'].isoformat() if alert['last_sent'] else None,
        })
    except Exception as e:
        conn.rollback()
        log.error(f"GET /api/alerts error: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)


@alerts_bp.route('/api/alerts', methods=['PUT'])
@token_required
def update_alerts():
    """Update alert settings."""
    data = request.json or {}
    frequency = data.get('frequency')
    min_score = data.get('min_score')

    if frequency and frequency not in ('daily', 'instant', 'weekly', 'off'):
        return jsonify({"error": "Fréquence invalide"}), 400
    if min_score is not None:
        try:
            min_score = int(min_score)
            if min_score < 0 or min_score > 100:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "Score minimum invalide"}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM alerts WHERE user_id = %s LIMIT 1", (request.user_id,))
        alert = cur.fetchone()
        if not alert:
            cur.execute(
                "SELECT id FROM search_profiles WHERE user_id = %s AND is_active = TRUE ORDER BY created_at LIMIT 1",
                (request.user_id,)
            )
            profile = cur.fetchone()
            profile_id = profile['id'] if profile else None
            cur.execute("""
                INSERT INTO alerts (user_id, profile_id, channel, frequency, min_score, is_active)
                VALUES (%s, %s, 'email', 'daily', 70, TRUE)
                RETURNING id
            """, (request.user_id, profile_id))
            alert = cur.fetchone()
            conn.commit()

        updates = []
        params = []
        if frequency == 'off':
            updates.append("is_active = FALSE")
        elif frequency:
            updates.append("frequency = %s")
            updates.append("is_active = TRUE")
            params.append(frequency)
        if min_score is not None:
            updates.append("min_score = %s")
            params.append(min_score)

        if updates:
            params.append(alert['id'])
            cur.execute(f"UPDATE alerts SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()

        cur.execute(
            "SELECT id, frequency, min_score, is_active, channel, last_sent FROM alerts WHERE id = %s",
            (alert['id'],)
        )
        updated = cur.fetchone()
        return jsonify({
            "ok": True,
            "frequency": updated['frequency'] if updated['is_active'] else 'off',
            "min_score": updated['min_score'],
            "is_active": updated['is_active'],
        })
    except Exception as e:
        conn.rollback()
        log.error(f"PUT /api/alerts error: {e}")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        cur.close()
        return_db(conn)
