"""
v6.3.2 étape 5 — Suggestions de communes sur résolution échouée.

Logique pure (pas de DB, pas d'API) : quand `resolve_zone_coords` n'arrive
pas à résoudre une query, on appelle `suggest_similar_cities(query)` pour
proposer les 2-3 communes les plus proches de CITY_COORDS+NPA_COORDS par
proximité lexicale. Le chat UX les rend en boutons cliquables.

Stratégie de matching (2 passes, plan validé) :
  1. Prefix match bidirectionnel sur les noms normalisés.
     Ex : 'corta' → 'cortaillod' (prefix hit immédiat).
  2. Fuzzy difflib cutoff=0.5 sur le reste.
     Ex : 'neufchatel' → 'neuchatel' (ratio ~0.89).

Branche NPA (4 chiffres) : retourne [] — le caller doit demander le nom
de la commune, pas proposer d'autres NPA numériques (les chiffres ne se
corrigent pas par fuzzy matching utile).

Helper de normalisation `norm_city_query` exposé publiquement — il
délègue à `scoring_engine._norm_city_name` pour garantir que la clé
utilisée ici est IDENTIQUE à celle de resolve_zone_coords/geo_cache.
"""
import difflib
import json
import logging

from scoring_engine import CITY_COORDS, NPA_COORDS, _norm_city_name

log = logging.getLogger('lou-app')


# Index construit une fois au premier appel — (norm, display, lat, lng) tuples.
_SUGGESTION_INDEX = None


def norm_city_query(s):
    """
    Normalisation publique partagée entre resolve_zone_coords et ce module.
    Délègue à scoring_engine._norm_city_name pour cohérence stricte.
    """
    return _norm_city_name(s or '')


def is_npa(q):
    """True if query is a 4-digit Swiss postal code."""
    if q is None:
        return False
    s = str(q).strip()
    return s.isdigit() and len(s) == 4


def _has_accents(s):
    return any(ord(c) > 127 for c in (s or ''))


def _build_index():
    """Builds the (norm → display, lat, lng) index from CITY_COORDS + NPA_COORDS."""
    global _SUGGESTION_INDEX
    seen = {}

    # CITY_COORDS : clés sont des noms de villes (parfois avec accents, parfois sans).
    # On préfère garder le display avec accents si disponible.
    for name, coords in CITY_COORDS.items():
        if not name or not coords:
            continue
        lat, lng = coords[0], coords[1]
        norm = _norm_city_name(name)
        if not norm:
            continue
        prev = seen.get(norm)
        if prev is None:
            seen[norm] = (norm, name, lat, lng)
        elif _has_accents(name) and not _has_accents(prev[1]):
            # Remplace le display sans accents par la version accentuée
            seen[norm] = (norm, name, lat, lng)

    # NPA_COORDS : values = (lat, lng, canonical_name)
    for npa, tup in NPA_COORDS.items():
        if not tup or len(tup) < 3:
            continue
        lat, lng, cname = tup[0], tup[1], tup[2]
        if not cname:
            continue
        norm = _norm_city_name(cname)
        if not norm or norm in seen:
            continue
        seen[norm] = (norm, cname, lat, lng)

    _SUGGESTION_INDEX = list(seen.values())
    log.info(f"geo_suggestions index built: {len(_SUGGESTION_INDEX)} unique communes")


def _ensure_index():
    if _SUGGESTION_INDEX is None:
        _build_index()


def suggest_similar_cities(query, limit=3):
    """
    Retourne une liste ordonnée (meilleur match en premier) de dicts :
      [{'city': 'Cortaillod', 'latitude': 46.93, 'longitude': 6.85}, ...]

    Contrat :
      - query vide / None  → []
      - query NPA 4 digits → [] (caller demande le nom de commune)
      - sinon : prefix match puis fuzzy cutoff=0.5, max `limit` résultats

    N'appelle ni la DB ni l'API : pure logique in-memory sur CITY_COORDS+NPA.
    """
    if not query:
        return []
    q = str(query).strip()
    if not q:
        return []
    if is_npa(q):
        return []
    q_norm = _norm_city_name(q)
    if not q_norm:
        return []

    _ensure_index()

    # Pass 1 : prefix bidirectionnel. Score = diff de longueur (plus petit = mieux).
    prefix_hits = []
    seen_norms = set()
    for entry in _SUGGESTION_INDEX:
        norm, display, lat, lng = entry
        if norm.startswith(q_norm) or q_norm.startswith(norm):
            prefix_hits.append((abs(len(norm) - len(q_norm)), display, lat, lng, norm))
            seen_norms.add(norm)
    prefix_hits.sort(key=lambda x: x[0])
    prefix_hits = prefix_hits[:limit]

    out = [{'city': h[1], 'latitude': h[2], 'longitude': h[3]} for h in prefix_hits]
    if len(out) >= limit:
        return out

    # Pass 2 : fuzzy difflib cutoff=0.5 sur les entries non déjà prises.
    remaining = limit - len(out)
    candidates = [e[0] for e in _SUGGESTION_INDEX if e[0] not in seen_norms]
    fuzzy_norms = difflib.get_close_matches(q_norm, candidates, n=remaining, cutoff=0.5)
    norm_to_entry = {e[0]: e for e in _SUGGESTION_INDEX}
    for m in fuzzy_norms:
        e = norm_to_entry[m]
        out.append({'city': e[1], 'latitude': e[2], 'longitude': e[3]})

    return out


def log_unresolved(conn, query, suggestions, user_id=None, anon_session_id=None, chosen=None):
    """
    Enregistre une résolution échouée dans unresolved_locations.
    Tolérant : ne throw jamais (audit-only, pas critique).

    - user_id XOR anon_session_id (au moins un des deux, sinon log anonyme pur).
    - suggestions : liste de dicts (sérialisée en JSONB).
    - chosen : peut être set plus tard via update_unresolved_choice.

    Retourne l'id du row créé, ou None si échec.
    """
    if conn is None or not query:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO unresolved_locations
                (user_id, anon_session_id, query, suggestions_json, chosen)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, anon_session_id, query,
             json.dumps(suggestions) if suggestions else None,
             chosen),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row:
            return row['id'] if isinstance(row, dict) else row[0]
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        log.warning(f"unresolved_locations log failed for {query!r}: {e}")
    return None


def update_unresolved_choice(conn, row_id, chosen):
    """Met à jour le champ chosen d'une row unresolved_locations existante."""
    if conn is None or not row_id:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE unresolved_locations SET chosen = %s WHERE id = %s",
            (chosen, row_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        log.warning(f"unresolved_locations update failed for id={row_id}: {e}")
