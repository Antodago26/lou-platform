"""Bon Home — Static pages + health endpoint Blueprint."""
from flask import Blueprint, jsonify, send_from_directory

from plans import public_catalog

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    return send_from_directory('static', 'index.html')


@pages_bp.route('/manifest.json')
def manifest_json():
    return send_from_directory('static', 'manifest.json')


@pages_bp.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'dashboard.html')


@pages_bp.route('/privacy')
def privacy():
    return send_from_directory('static', 'privacy.html')


@pages_bp.route('/terms')
def terms():
    return send_from_directory('static', 'terms.html')


@pages_bp.route('/profil')
def profil():
    return send_from_directory('static', 'profil.html')


@pages_bp.route('/faq')
def faq():
    return send_from_directory('static', 'faq.html')


@pages_bp.route('/pricing')
def pricing():
    return send_from_directory('static', 'pricing.html')


@pages_bp.route('/health')
def health():
    return jsonify({"status": "ok"})


@pages_bp.route('/api/plans')
def api_plans():
    """Public pricing catalog (C3.4)."""
    return jsonify(public_catalog())
