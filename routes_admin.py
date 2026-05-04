"""Bon Home — Admin Blueprint."""
import logging

from flask import Blueprint, jsonify, request

from db import get_db, return_db
from auth import token_required, admin_required, ADMIN_EMAIL

log = logging.getLogger('lou-app')
admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    """List all registered users with stats."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.email, u.name, u.created_at, u.last_login, u.is_active, u.plan,
                   COUNT(DISTINCT sp.id) AS profiles_count,
                   COUNT(DISTINCT f.property_id) AS favorites_count
            FROM users u
            LEFT JOIN search_profiles sp ON sp.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created_at DESC
        """)
        users = []
        for row in cur.fetchall():
            users.append({
                'id': row['id'],
                'email': row['email'],
                'name': row['name'] or '',
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'last_login': row['last_login'].isoformat() if row['last_login'] else None,
                'is_active': row['is_active'],
                'plan': row['plan'] or 'free',
                'profiles_count': row['profiles_count'],
                'favorites_count': row['favorites_count'],
            })
        return jsonify({"users": users, "total": len(users)})
    finally:
        cur.close()
        return_db(conn)


@admin_bp.route('/api/admin/check', methods=['GET'])
@token_required
def admin_check():
    """Check if current user is admin."""
    if not ADMIN_EMAIL:
        return jsonify({"is_admin": False})
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT email FROM users WHERE id = %s", (request.user_id,))
        user = cur.fetchone()
        is_admin = user and user['email'] == ADMIN_EMAIL.lower().strip()
        return jsonify({"is_admin": is_admin})
    finally:
        cur.close()
        return_db(conn)
