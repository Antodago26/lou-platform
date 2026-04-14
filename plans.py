"""
Bon Home — Pricing plans (C3.1).

Single source of truth for tiered-plan limits and feature flags. While
PRICING_ENABLED is False the helpers are effectively no-ops so no existing
endpoint is gated — the wiring is in place so that flipping the flag to True
(after Stripe checkout + webhooks are live) immediately enforces limits.
"""
import os

# Master kill-switch. Flip to True only when Stripe integration is ready.
PRICING_ENABLED = os.environ.get('PRICING_ENABLED', '').lower() in ('1', 'true', 'yes')


# Plan catalog. Limits default to None = unlimited.
PLANS = {
    'free': {
        'id': 'free',
        'name': 'Gratuit',
        'price_chf': 0,
        'period': 'month',
        'limits': {
            'active_profiles':          1,
            'zones_per_profile':        2,
            'favorites':                10,
            'chat_messages_per_day':    20,
            'alerts_frequency':         ['daily', 'weekly'],
            'alerts_instant':           False,
        },
        'features': {
            'export_csv':        False,
            'price_history':     False,
            'custom_weights':    False,
            'priority_support':  False,
            'no_ads':            False,
        },
    },
    'premium': {
        'id': 'premium',
        'name': 'Premium',
        'price_chf': 9,
        'period': 'month',
        'highlight': True,
        'limits': {
            'active_profiles':          3,
            'zones_per_profile':        5,
            'favorites':                100,
            'chat_messages_per_day':    100,
            'alerts_frequency':         ['instant', 'daily', 'weekly'],
            'alerts_instant':           True,
        },
        'features': {
            'export_csv':        True,
            'price_history':     True,
            'custom_weights':    True,
            'priority_support':  False,
            'no_ads':            True,
        },
    },
    'pro': {
        'id': 'pro',
        'name': 'Pro',
        'price_chf': 29,
        'period': 'month',
        'limits': {
            'active_profiles':          None,
            'zones_per_profile':        None,
            'favorites':                None,
            'chat_messages_per_day':    None,
            'alerts_frequency':         ['instant', 'daily', 'weekly'],
            'alerts_instant':           True,
        },
        'features': {
            'export_csv':        True,
            'price_history':     True,
            'custom_weights':    True,
            'priority_support':  True,
            'no_ads':            True,
        },
    },
}


def get_plan(plan_id):
    """Return the plan dict for a given id, or the free plan as fallback."""
    return PLANS.get((plan_id or 'free').lower(), PLANS['free'])


def get_limit(plan_id, key):
    """Return the numeric/structured limit for a given plan and key.
    None means unlimited."""
    return get_plan(plan_id).get('limits', {}).get(key)


def check_limit(plan_id, key, current_usage):
    """True if the user is still under their quota for a given limit.
    While PRICING_ENABLED is False, always returns True (no-op)."""
    if not PRICING_ENABLED:
        return True
    limit = get_limit(plan_id, key)
    if limit is None:
        return True  # unlimited
    try:
        return int(current_usage) < int(limit)
    except (TypeError, ValueError):
        return True


def is_feature_allowed(plan_id, feature):
    """True if the feature is enabled on this plan. While PRICING_ENABLED is
    False, always returns True (no-op so nothing blocks the UI today)."""
    if not PRICING_ENABLED:
        return True
    return bool(get_plan(plan_id).get('features', {}).get(feature, False))


def public_catalog():
    """Serializable plan catalog for the public GET /api/plans endpoint."""
    return {
        'pricing_enabled': PRICING_ENABLED,
        'plans': [
            {
                'id': p['id'],
                'name': p['name'],
                'price_chf': p['price_chf'],
                'period': p['period'],
                'highlight': p.get('highlight', False),
                'limits': p['limits'],
                'features': p['features'],
            }
            for p in PLANS.values()
        ],
    }
