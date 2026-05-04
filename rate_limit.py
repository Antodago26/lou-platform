"""In-memory per-worker rate limiter with minute + hour buckets.

Originally lived inside routes_chat.py. Extracted here in 2026-05 so it can
also gate /api/login, /api/signup, /api/scrape, /api/import (audit C2/H3).

Limitation: in-memory means each gunicorn worker has its own counters, so
the effective rate is N×worker. Acceptable for landing-page-scale brute force
but not for distributed attacks. TODO post-beta: back with Redis/flask-limiter
to aggregate across workers.
"""
import time
import logging
from collections import defaultdict

from flask import request

log = logging.getLogger('lou-app')

# Two buckets per key: minute and hour. Both must be under their cap.
_buckets_min = defaultdict(list)
_buckets_hour = defaultdict(list)


def client_ip():
    """Real client IP. Render/Cloudflare inject X-Forwarded-For; first hop
    is the original client, the rest are proxies in the chain."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        first = xff.split(',')[0].strip()
        if first:
            return first
    return request.remote_addr or 'unknown'


def check_rate_limit(key, per_min, per_hour):
    """Returns True if the request is allowed and records it.
    Returns False if either bucket is exhausted (no record made)."""
    now = time.time()
    _buckets_min[key] = [t for t in _buckets_min[key] if now - t < 60]
    _buckets_hour[key] = [t for t in _buckets_hour[key] if now - t < 3600]
    if len(_buckets_min[key]) >= per_min:
        return False
    if len(_buckets_hour[key]) >= per_hour:
        return False
    _buckets_min[key].append(now)
    _buckets_hour[key].append(now)
    return True


def rate_limited_response(retry_after_seconds=60):
    """Helper for the standard 429 response."""
    from flask import jsonify
    return jsonify({
        "error": "Trop de requêtes. Réessaie dans quelques minutes."
    }), 429, {'Retry-After': str(retry_after_seconds)}
