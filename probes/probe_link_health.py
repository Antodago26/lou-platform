"""
Probe link health — diagnostic standalone, à lancer une fois en Render shell
avant de coder le worker link health (Phase 2 du cron `lou-qa-recall`).

Usage Render shell :
    python probes/probe_link_health.py

Sortie console (4 sections) :
  1. VOLUMÉTRIE — COUNT total / actives / avec source_url / Homegate.
     Détermine la stratégie de cyclage du worker (1000/run, ordre par
     last_checked_at, etc.) selon la taille du stock.
  2. ÉCHANTILLON — 5 URLs Homegate les plus récentes (ORDER BY first_seen_at DESC).
  3. PROBE STEALTH — pour chaque URL, `_sb_get(url, render_js=False)`
     en mode stealth_proxy (~1 crédit ScrapingBee), avec sb_bypass_cache
     activé pour forcer l'appel réel (pas de cache hit DB de SCRAPE_CACHE_TTL).
     Affiche status + temps + premiers 200 chars du body (pour repérer
     une page DataDome HTML vs vraie page Homegate).
  4. VERDICT — STEALTH OK / BLOCKED / MIXED selon ≥3 réponses réelles ou
     ≥3 blocages 403.

Coût total : 5 crédits ScrapingBee (1 par URL, stealth, render_js=False).
Effets de bord : aucune écriture DB ; possible write-through dans la table
SCRAPE_CACHE de _sb_get sur les calls réussis (acceptable, ça réduit la
charge des calls suivants pour ces URLs).

Pré-requis env :
  - DATABASE_URL  (sinon les SELECTs échouent au démarrage)
  - SCRAPINGBEE_API_KEY (sinon _sb_get tombe en fallback direct → 403 garanti
    qui faussent le verdict — on abort plutôt que de polluer le résultat)
"""
import logging
import os
import sys
import time

# `python probes/probe_link_health.py` lance le script avec sys.path[0] =
# probes/ → `from db import ...` échoue. On insère le parent (backend-v2/)
# pour que les imports trouvent les modules au même niveau.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
)
log = logging.getLogger('lou-probe')

# Pré-flight env checks AVANT les imports lourds (pour fail-fast clair).
if not os.environ.get('DATABASE_URL'):
    print("ERROR: DATABASE_URL not set — abort.", file=sys.stderr)
    sys.exit(1)
if not os.environ.get('SCRAPINGBEE_API_KEY'):
    print(
        "ERROR: SCRAPINGBEE_API_KEY not set. Sans la clé, _sb_get tombe en\n"
        "fallback direct (requests.get) qui produit 403 systématique côté\n"
        "Homegate (cf. probe curl_cffi du 26/04) — verdict faussé. Abort.",
        file=sys.stderr,
    )
    sys.exit(1)

from db import get_db, return_db                           # noqa: E402
from scrapers import _sb_get, sb_bypass_cache              # noqa: E402


def _row_get(row, key, idx):
    """Tolère RealDictCursor (dict) ET cursor brut (tuple)."""
    if isinstance(row, dict):
        return row[key]
    return row[idx]


# ============================================================
# 1) VOLUMÉTRIE
# ============================================================
print("=" * 80)
print("1. VOLUMÉTRIE — table `properties`")
print("=" * 80)

actives_homegate = 0
actives_with_url = 0

conn = get_db()
try:
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE is_active = TRUE) AS actives,
               COUNT(*) FILTER (WHERE source_url IS NOT NULL) AS with_url,
               COUNT(*) FILTER (WHERE is_active = TRUE AND source_url IS NOT NULL) AS actives_with_url,
               COUNT(*) FILTER (WHERE is_active = TRUE AND source_url IS NOT NULL
                                AND LOWER(source) = 'homegate') AS actives_homegate
        FROM properties
    """)
    row = cur.fetchone()
    cur.close()

    total            = _row_get(row, 'total', 0)
    actives          = _row_get(row, 'actives', 1)
    with_url         = _row_get(row, 'with_url', 2)
    actives_with_url = _row_get(row, 'actives_with_url', 3)
    actives_homegate = _row_get(row, 'actives_homegate', 4)

    print(f"  total rows                    : {total:>8}")
    print(f"  is_active = TRUE              : {actives:>8}")
    print(f"  source_url IS NOT NULL        : {with_url:>8}")
    print(f"  active + source_url           : {actives_with_url:>8}   ← scope worker link health")
    print(f"  active + source_url + Homegate: {actives_homegate:>8}   ← part qui demande ScrapingBee")

    if actives_with_url > 0:
        homegate_pct = round(100 * actives_homegate / actives_with_url, 1)
        non_hg = actives_with_url - actives_homegate
        print()
        print(f"  → Homegate     = {homegate_pct:>5}% des liens checkables ({actives_homegate} URLs)")
        print(f"  → non-Homegate = {100 - homegate_pct:>5}% des liens checkables ({non_hg} URLs, HEAD direct gratuit)")
finally:
    return_db(conn)

print()

# ============================================================
# 2) ÉCHANTILLON 5 URLs Homegate récentes
# ============================================================
print("=" * 80)
print("2. ÉCHANTILLON — 5 URLs Homegate les plus récentes")
print("=" * 80)

urls = []
conn = get_db()
try:
    cur = conn.cursor()
    cur.execute("""
        SELECT source_url
        FROM properties
        WHERE is_active = TRUE
          AND source_url IS NOT NULL
          AND LOWER(source) = 'homegate'
        ORDER BY first_seen_at DESC NULLS LAST
        LIMIT 5
    """)
    rows = cur.fetchall()
    cur.close()
    for r in rows:
        u = _row_get(r, 'source_url', 0)
        if u:
            urls.append(u)
            print(f"  {u}")
finally:
    return_db(conn)

if not urls:
    print("  ❌ Aucune URL Homegate trouvée en DB — abort probe ScrapingBee.")
    print("  Possible cause : 0 row Homegate active, ou source_url systématiquement NULL.")
    sys.exit(1)

print()

# ============================================================
# 3) PROBE ScrapingBee STEALTH
# ============================================================
print("=" * 80)
print(f"3. PROBE STEALTH — _sb_get(url, render_js=False) × {len(urls)} URLs")
print(f"   coût attendu : ~{len(urls)} crédits ScrapingBee (stealth_proxy)")
print("=" * 80)

results = []
with sb_bypass_cache():
    for i, url in enumerate(urls, 1):
        print(f"\n  [{i}/{len(urls)}] {url}")
        t0 = time.time()
        try:
            status, html = _sb_get(url, render_js=False)
        except Exception as e:
            status, html = -1, f"EXCEPTION: {type(e).__name__}: {e}"
        elapsed = round(time.time() - t0, 2)

        snippet = (html or '')[:200].replace('\n', ' ').replace('\r', ' ')
        # Détection heuristique d'une page DataDome / anti-bot (au cas où SB
        # renvoie 200 avec une page de challenge plutôt qu'un 403 propre).
        antibot_marker = ''
        low = (html or '').lower()[:5000]
        if 'datadome' in low or 'captcha-delivery' in low:
            antibot_marker = ' [⚠ DataDome challenge dans le body]'
        elif 'access denied' in low or 'cf-ray' in low and 'attention required' in low:
            antibot_marker = ' [⚠ Cloudflare block dans le body]'

        print(f"      status={status}  time={elapsed}s  body_len={len(html or '')}{antibot_marker}")
        print(f"      body[:200]: {snippet[:200]}")
        results.append((url, status, elapsed, snippet, antibot_marker))

print()

# ============================================================
# 4) VERDICT
# ============================================================
print("=" * 80)
print("4. VERDICT")
print("=" * 80)

# 304 exclu de "réelles" (= cache hit local, pas réponse HTTP réelle).
# Avec sb_bypass_cache actif on ne devrait jamais voir 304, mais double safety.
REAL_HTTP = {200, 301, 302, 404, 410}
real_count    = sum(1 for _, s, _, _, m in results if s in REAL_HTTP and not m)
real_with_marker = sum(1 for _, s, _, _, m in results if s in REAL_HTTP and m)
blocked_count = sum(1 for _, s, _, _, _ in results if s == 403)
error_count   = sum(1 for _, s, _, _, _ in results if s in (-1, 0) or (isinstance(s, int) and s >= 500))
n = len(results)

print(f"  Réponses réelles (2xx/3xx/404/410, body propre)  : {real_count}/{n}")
print(f"  Réponses 200 mais body anti-bot (DataDome/CF)    : {real_with_marker}/{n}")
print(f"  Bloquées (403)                                   : {blocked_count}/{n}")
print(f"  Erreurs (timeout/5xx/exception)                  : {error_count}/{n}")
print()

# Décision : on ne compte PAS les 200-avec-DataDome comme "réelles" — c'est
# un bypass anti-bot raté qui a renvoyé 200 + page de challenge à la place
# du contenu attendu (status 200 trompeur).
if real_count >= 3:
    cred_per_run_homegate = actives_homegate  # 1 crédit/URL stealth
    print(f"  → ✅ STEALTH OK. ScrapingBee stealth (1 crédit) passe Homegate.")
    print(f"     Option (c) viable. Estimation : ~{cred_per_run_homegate} crédits/run pour")
    print(f"     vérifier toutes les URLs Homegate actives en une passe.")
elif blocked_count + real_with_marker >= 3:
    print(f"  → ❌ STEALTH BLOCKED. Stealth ne passe pas mieux que curl_cffi.")
    print(f"     Décision CEO : (a) marquer Homegate `unreachable` permanent, ou")
    print(f"     (b) Premium (~5 crédits/URL = {actives_homegate * 5} crédits/run).")
else:
    print(f"  → ⚠ MIXED ({real_count} réelles / {blocked_count} bloquées / {real_with_marker} 200-trompeurs).")
    print(f"     Comportement inconsistant. Hypothèses : rate-limit Homegate,")
    print(f"     geo-IP variable du stealth pool, ou différence /buy vs /rent.")
    print(f"     Décision CEO : retry premium sur fail uniquement, ou échantillon")
    print(f"     plus large (50+ URLs) pour stats plus fiables.")

print()
print("=" * 80)
print("FIN — colle ce résultat à Claude Code pour qu'il choisisse l'archi worker.")
print("=" * 80)
