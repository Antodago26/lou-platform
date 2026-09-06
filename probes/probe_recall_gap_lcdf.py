"""
Probe ad-hoc — diagnostic du gap recall Homegate vente la-chaux-de-fonds
(snapshot 2026-04-30, recall 70.54%, 19 IDs manquants).

NE PAS MERGER. Exécution one-shot pour rapporter l'état DB des 5 IDs cités
dans le brief, puis comparer avec ce que verrait le filtre du recall worker.

Usage (en local avec le DATABASE_URL du Render dashboard exporté) :
    cd backend-v2
    DATABASE_URL='...' python3 probes/probe_recall_gap_lcdf.py
"""
import os
import sys
import psycopg2
import psycopg2.extras

IDS = [
    '3003150296', '4000178225', '4000243889', '4001678850', '4002237144',
]

QUERY_PROPERTIES = """
    SELECT id, external_id, source, source_url, scraped_at, is_active,
           postal_code AS npa, city, transaction
    FROM properties
    WHERE external_id = ANY(%s)
       OR source_url ILIKE ANY(%s)
"""

QUERY_PROPERTY_SOURCES = """
    SELECT ps.property_id, ps.source, ps.external_id, ps.source_url,
           p.is_active, p.city, p.transaction, p.scraped_at
    FROM property_sources ps
    JOIN properties p ON p.id = ps.property_id
    WHERE ps.external_id = ANY(%s)
       OR ps.source_url ILIKE ANY(%s)
"""


def main():
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    hg_eids = [f"hg-{i}" for i in IDS]
    url_patterns = [f"%{i}%" for i in IDS]

    with psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor) as conn:
        cur = conn.cursor()
        cur.execute(QUERY_PROPERTIES, (hg_eids, url_patterns))
        props = cur.fetchall()

        cur.execute(QUERY_PROPERTY_SOURCES, (hg_eids, url_patterns))
        psrcs = cur.fetchall()

        # Quel est le filtre exact du recall pour Homegate vente LCdF ?
        # Reproduit `_fetch_our_ids('la-chaux-de-fonds', 'La Chaux-de-Fonds',
        # 'homegate', 'achat')` du qa_recall_worker.
        cur.execute("""
            SELECT DISTINCT eid FROM (
                SELECT p.external_id AS eid
                FROM properties p
                WHERE p.is_active = TRUE
                  AND LOWER(COALESCE(p.city, '')) IN ('la chaux-de-fonds', 'la-chaux-de-fonds')
                  AND p.transaction = 'achat'
                  AND LOWER(COALESCE(p.source, '')) = 'homegate'
                  AND p.external_id IS NOT NULL
                UNION ALL
                SELECT ps.external_id AS eid
                FROM property_sources ps
                JOIN properties p ON p.id = ps.property_id
                WHERE p.is_active = TRUE
                  AND LOWER(COALESCE(p.city, '')) IN ('la chaux-de-fonds', 'la-chaux-de-fonds')
                  AND p.transaction = 'achat'
                  AND LOWER(COALESCE(ps.source, '')) = 'homegate'
                  AND ps.external_id IS NOT NULL
            ) x
        """)
        recall_visible = {r['eid'] for r in cur.fetchall()}

    print("=" * 70)
    print("PROPERTIES match")
    print("=" * 70)
    if not props:
        print("(aucune ligne)")
    for r in props:
        print(
            f"  id={r['id']} eid={r['external_id']} src={r['source']} "
            f"is_active={r['is_active']} city={r['city']!r} "
            f"tx={r['transaction']} npa={r['npa']} "
            f"scraped_at={r['scraped_at']}"
        )
        print(f"    url={r['source_url']}")

    print()
    print("=" * 70)
    print("PROPERTY_SOURCES match")
    print("=" * 70)
    if not psrcs:
        print("(aucune ligne)")
    for r in psrcs:
        print(
            f"  prop_id={r['property_id']} src={r['source']} eid={r['external_id']} "
            f"is_active={r['is_active']} city={r['city']!r} tx={r['transaction']} "
            f"scraped_at={r['scraped_at']}"
        )

    print()
    print("=" * 70)
    print("Verdict par ID — present-in-DB-but-excluded-from-recall ?")
    print("=" * 70)
    for i in IDS:
        eid = f"hg-{i}"
        in_props_active = any(
            (p['external_id'] == eid) and p['is_active'] for p in props
        )
        in_props_inactive = any(
            (p['external_id'] == eid) and not p['is_active'] for p in props
        )
        in_psrcs = any(p['external_id'] == eid for p in psrcs)
        seen_by_recall = eid in recall_visible

        if not props_match(props, eid) and not in_psrcs:
            verdict = "ABSENT (jamais scrappé)"
        elif seen_by_recall:
            verdict = "PRESENT et visible recall (anomalie : pourquoi est-il dans missing ?)"
        elif in_props_inactive:
            verdict = "PRESENT mais is_active=FALSE (deactivated par 21d rule)"
        elif in_props_active:
            verdict = "PRESENT actif mais filtré (city ou tx ne match pas)"
        elif in_psrcs:
            verdict = "PRESENT seulement via property_sources sur autre property"
        else:
            verdict = "?"

        print(f"  {eid}: {verdict}")


def props_match(props, eid):
    return any(p['external_id'] == eid for p in props)


if __name__ == '__main__':
    main()
