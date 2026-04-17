"""
Audit NPA_COORDS vs geo.admin.ch SearchServer.
Pour chaque NPA dans scoring_engine.NPA_COORDS, interroge l'API officielle
Swisstopo et compare (haversine). Rapport des discordances > 500m.

Usage: python3 migrations/audit_npa_coords.py
"""
import math
import sys
import time
import urllib.parse
import urllib.request
import json
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoring_engine import NPA_COORDS

GEO_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
THRESHOLD_M = 500  # discordance significative


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def query_geo(npa):
    """
    Interroge geo.admin.ch SearchServer et filtre STRICTEMENT les résultats
    dont le label matche le pattern zipcode "<b>NNNN - Ville</b>".
    Les autres types (gg25, parcel, address) ne sont pas fiables pour le NPA.
    """
    import re as _re
    params = {
        'sr': '4326',
        'lang': 'fr',
        'limit': '10',
        'type': 'locations',
        'searchText': str(npa),
    }
    url = GEO_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'bonhome-audit/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode('utf-8'))
    results = data.get('results', [])
    zip_pattern = _re.compile(r'^<b>\s*' + _re.escape(str(npa)) + r'\s*-\s*(.+?)</b>')
    for item in results:
        attrs = item.get('attrs', {})
        label = attrs.get('label', '') or ''
        m = zip_pattern.match(label)
        if m:
            return attrs.get('lat'), attrs.get('lon'), label
    return None, None, None


def main():
    discrepancies = []
    ok = []
    errors = []
    for npa, (lat, lng, name) in sorted(NPA_COORDS.items()):
        try:
            g_lat, g_lng, g_label = query_geo(npa)
            if g_lat is None:
                errors.append((npa, name, 'geo.admin.ch no result'))
                continue
            d = haversine(lat, lng, g_lat, g_lng)
            entry = (npa, name, (lat, lng), (g_lat, g_lng), d, g_label)
            if d > THRESHOLD_M:
                discrepancies.append(entry)
            else:
                ok.append(entry)
        except Exception as e:
            errors.append((npa, name, str(e)))
        time.sleep(0.15)  # polite rate limit

    print("=" * 80)
    print(f"AUDIT NPA_COORDS — {len(NPA_COORDS)} entrées")
    print("=" * 80)
    print(f"\nOK (<{THRESHOLD_M}m): {len(ok)}")
    print(f"DISCORDANCES (>{THRESHOLD_M}m): {len(discrepancies)}")
    print(f"ERREURS: {len(errors)}")

    if discrepancies:
        print("\n--- DISCORDANCES ---")
        for npa, name, local, geo, d, label in sorted(discrepancies, key=lambda x: -x[4]):
            print(f"  NPA {npa} '{name}': local={local} vs geo={geo} (delta={d:.0f}m) -- geo label: {label}")

    if errors:
        print("\n--- ERREURS ---")
        for npa, name, err in errors:
            print(f"  NPA {npa} '{name}': {err}")


if __name__ == '__main__':
    main()
