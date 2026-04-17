"""
Pour les NPA génériques des grandes villes (1200 Genève, 8000 Zürich...),
le SearchServer zipcode ne renvoie pas de résultat propre.
On récupère les coords de la commune via l'endpoint gg25.

Usage: python3 migrations/fetch_gg25_communes.py
"""
import urllib.parse
import urllib.request
import json
import time
import re

GEO_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"

# NPA génériques à résoudre par nom de commune
GENERIC_NPA = {
    '1200': 'Genève',
    '2002': 'Neuchâtel',
    '2301': 'La Chaux-de-Fonds',
    '2500': 'Bienne',
    '3000': 'Bern',          # on essaie "Bern" (label officiel)
    '4000': 'Basel',
    '6000': 'Luzern',
    '8000': 'Zürich',
}


def query_gg25(city):
    params = {
        'sr': '4326',
        'lang': 'fr',
        'limit': '5',
        'type': 'locations',
        'searchText': city,
    }
    url = GEO_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'bonhome-audit/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode('utf-8'))
    # Cherche origin=gg25 (commune officielle)
    for item in data.get('results', []):
        attrs = item.get('attrs', {})
        if attrs.get('origin') == 'gg25':
            lat = attrs.get('lat')
            lng = attrs.get('lon')
            label = re.sub(r'<[^>]+>', '', attrs.get('label', ''))
            return lat, lng, label
    # Fallback: premier résultat
    for item in data.get('results', []):
        attrs = item.get('attrs', {})
        lat = attrs.get('lat')
        lng = attrs.get('lon')
        label = re.sub(r'<[^>]+>', '', attrs.get('label', ''))
        return lat, lng, label
    return None, None, None


def main():
    print("GG25 fetch pour NPA génériques")
    print("=" * 70)
    for npa, city in GENERIC_NPA.items():
        try:
            lat, lng, label = query_gg25(city)
            print(f"  '{npa}': ({lat}, {lng}, '{label}')")
        except Exception as e:
            print(f"  {npa} ({city}) ERR: {e}")
        time.sleep(0.2)


if __name__ == '__main__':
    main()
