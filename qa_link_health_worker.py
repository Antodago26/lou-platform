"""
Bon Home — QA Link Health Worker (v6.4.5).

Phase 2 du cron `lou-qa-recall` (à appeler après le bloc recall snapshot).
Vérifie que les `properties.source_url` des annonces actives répondent encore.

Stratégie par portail :

  1. Non-Homegate (~85% du stock — agences directes, IS24 legacy, Comparis…) :
     `requests.head(url)` direct, gratuit. Throttle 10 req/s par domaine.

  2. Homegate (~15% du stock) : DataDome bloque le HEAD direct, le stealth
     ScrapingBee ET curl_cffi (probe 26/04). Deux paths :

     2.a. Optimization (e) : si `properties.scraped_at >= NOW() - 7d`,
          le cron prod (cron_job.py 16h UTC) a réussi à scraper l'URL
          cette semaine — donc Homegate l'a servi → URL alive. On marque
          `status='ok'`, `last_checked_at = NOW()`, sans appeler ScrapingBee.

          NB : c'est une SUBSTITUTION pragmatique du signal proposé par le
          CEO (lookup dans `qa_recall_snapshots` des IDs live). Le snapshot
          ne stocke PAS les IDs live, seulement les `missing_ids` (delta) —
          une lookup littérale fail systématiquement. `scraped_at` donne le
          MÊME signal effectif (URL vue alive cette semaine) sans modifier
          le format du snapshot. Documenté en clair dans le commit message.

     2.b. Sinon : `_sb_get(url, render_js=False, premium_proxy=True)` —
          ~11 crédits/call (1 base + 10 premium). Heuristique sur le body :
            - Marqueur DataDome/CF dans le HTML → 'unreachable' (Premium
              bloqué aussi, alerte WARNING si > 5% du batch)
            - Marqueur "annonce supprimée" / "page introuvable" → 'broken'
            - HTTP 200 sans marqueur suspect → 'ok'
            - HTTP 404/410 → 'broken'
            - Autre → 'unreachable'

Sémantique status (figée par CEO 26/04, 5xx révisé v6.4.6) :
  - 'ok'          : URL répond 200/2xx (ou cache_via_prod_scrape)
  - 'redirect'    : 3xx vers URL différente (final_url logged, pas d'alerte)
  - 'broken'      : 404 / 410 (signaux explicites "ressource n'existe plus")
                    OU marqueur "annonce supprimée" / "page introuvable"
                    dans le body. AUCUN autre status code ne mappe en
                    broken — en particulier PAS les 5xx (cf. unreachable).
  - 'unreachable' : 403, timeout, DNS, anti-bot, ET 5xx — peu importe la
                    source (HEAD direct, SB Premium). Distingo critique :
                      * Run d8fb6e2 du 26/04 : SB Premium retournait 500
                        systematic sur Homegate (DataDome via SB ou
                        SB-side failure indistinguable). Le mapping initial
                        5xx → 'broken' a produit 111 false positives.
                      * v6.4.6 : 5xx → 'unreachable'. Safer default sur
                        incertitude. Une vraie panne agence apparaîtra
                        sur plusieurs runs successifs et sera escaladée
                        manuellement.
                    Log silencieux, NE PAS masquer l'annonce.

Sélection : 1000 URLs/run (cap), ordre `last_checked_at NULLS FIRST`
(oldest first), filtre is_active=TRUE + source_url NOT NULL + (last_checked
NULL OR > 7j). Stock 7948 active → cycle complet en ~8 jours.

Effets de bord :
  - INSERT 1 row par URL dans qa_link_checks (FK CASCADE sur properties)
  - UPDATE properties.last_checked_at = NOW() pour chaque URL processée
  - INSERT/UPDATE 1 row qa_runs (run_type='link_check')
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg2
import requests

from db import get_db, return_db
from scrapers import _sb_get, sb_bypass_cache

log = logging.getLogger('lou-app')


# ============================================================================
# ⚠ ADVISORY DATA — qa_link_checks (depuis 26/04/2026)
# ============================================================================
# Les rows écrites dans `qa_link_checks` par ce worker NE DOIVENT PAS être
# consommées par des queries user-facing (filtre /api/properties, dépublication
# automatique, UPDATE properties SET is_active=FALSE) tant que :
#
#   1. La constante `LINK_HEALTH_AUTO_HIDE` ci-dessous n'a pas été flippée
#      à True via l'env var du même nom, ET
#   2. La classification (status='broken'/'ok'/'redirect'/'unreachable') a
#      été validée sur 3+ runs cron successifs sans false positive sur le
#      portail concerné.
#
# Historique false positives à conserver en mémoire :
#   - Run d8fb6e2 (26/04) : 111 Homegate marqués 'broken' à tort à cause de
#     ScrapingBee Premium 500 systematic. Fix v6.4.6 : 5xx → 'unreachable'
#     dans `_classify` (cf. commit message). Backfill DB via migration v643.
#
# Cf. aussi `COMMENT ON TABLE qa_link_checks` posé par schema_v642.py
# (visible via `\d+ qa_link_checks` dans psql) qui rappelle ce statut
# advisory directement au niveau Postgres.
# ============================================================================

# Flag de garde-fou. Aujourd'hui aucun consommateur en aval — ce flag est
# une CONVENTION pour empêcher qu'un futur PR ajoute "WHERE qa_link_checks.status
# != 'broken'" sans validation préalable. Tout futur code qui prend une
# décision basée sur qa_link_checks DOIT gater sur `LINK_HEALTH_AUTO_HIDE`.
LINK_HEALTH_AUTO_HIDE = (
    os.environ.get('LINK_HEALTH_AUTO_HIDE', 'false').strip().lower() == 'true'
)


# ============================================================
# Config
# ============================================================

# Cap d'URLs par run, configurable via env. 1000 = stock 8000 cyclé en 8 j.
_MAX_URLS_PER_RUN = int(os.environ.get('QA_LINK_HEALTH_MAX_URLS', '1000'))

# Si scraped_at récent (< N jours), skip ScrapingBee Premium pour Homegate
# (optimisation e). 7 j cohérent avec le seuil de re-check.
_AGE_THRESHOLD_DAYS = int(os.environ.get('QA_LINK_HEALTH_AGE_DAYS', '7'))

# HEAD direct timeout (les portails sains répondent < 1s).
_HEAD_TIMEOUT_S = 10

# UA réaliste — Mozilla générique. Les sites légitimes ne bloquent pas
# Mozilla ; les sites avec anti-bot (Homegate) bloquent même Chrome impersonate
# (cf. probe curl_cffi), donc l'UA n'est pas le facteur déterminant.
_HEAD_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': '*/*',
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.8',
}

# Throttle 10 req/s par domaine = 100 ms min entre 2 calls même domain.
_MIN_INTERVAL_PER_DOMAIN_S = 0.1

# Marqueurs anti-bot dans le body (HTTP 200 trompeur). Étend la liste si
# d'autres patterns apparaissent en prod.
_ANTIBOT_MARKERS = (
    'datadome',
    'captcha-delivery',
    'attention required! | cloudflare',
    'access denied',
)

# Marqueurs "annonce supprimée" dans les pages Homegate (cas où Premium
# passe le 200 mais le contenu est une page d'erreur applicative).
# Multilingue car Homegate sert FR/DE/EN selon le client.
_BROKEN_MARKERS = (
    'page introuvable',
    'objet supprimé',
    "objet n'est plus disponible",
    "annonce n'existe plus",
    'no longer available',
    'angebot wurde entfernt',
    'angebot ist nicht mehr verfügbar',
    'inserat existiert nicht',
)

# Coût ScrapingBee estimé pour 1 call Homegate Premium (render_js=False).
# 1 crédit base + 10 crédits premium_proxy. Pour reporting/logs uniquement.
_SB_CREDITS_PER_PREMIUM_CALL = 11

# Seuil d'alerte WARNING si trop d'unreachable dans le batch (signal que
# Premium ne passe plus DataDome non plus).
_UNREACHABLE_ALERT_PCT = 5.0


# ============================================================
# Helpers
# ============================================================

def _row_get(row, key, idx):
    """Tolère RealDictCursor (dict) ET cursor brut (tuple)."""
    if isinstance(row, dict):
        return row[key]
    return row[idx]


def _domain_of(url: str) -> str:
    """Extract netloc, lowercased. 'unknown' si parse fail."""
    try:
        return urlparse(url).netloc.lower() or 'unknown'
    except Exception:
        return 'unknown'


def _is_homegate(source_lower: str, url: str) -> bool:
    """True si listing Homegate. Source d'abord (le champ DB est canonique),
    URL en fallback si source vide ou ambiguë."""
    if source_lower == 'homegate':
        return True
    return 'homegate.ch' in _domain_of(url)


class _DomainThrottler:
    """Throttle simple par domaine. Bloque min `_MIN_INTERVAL_PER_DOMAIN_S`
    entre 2 appels au même domain. Single-thread (le worker est séquentiel)."""

    def __init__(self):
        self._last = {}

    def wait(self, domain: str) -> None:
        now = time.monotonic()
        elapsed = now - self._last.get(domain, 0.0)
        if elapsed < _MIN_INTERVAL_PER_DOMAIN_S:
            time.sleep(_MIN_INTERVAL_PER_DOMAIN_S - elapsed)
        self._last[domain] = time.monotonic()


def _classify(status_code, body=None, final_url=None):
    """Map HTTP response → (status, http_code, final_url, error_msg).

    Body markers évalués EN PREMIER : un 200 avec DataDome n'est pas 'ok'
    mais 'unreachable' (statut HTTP trompeur). Idem un 200 avec marqueur
    "annonce supprimée" est 'broken' applicatif malgré le 200 transport.
    """
    if body:
        body_lower = body.lower()[:5000]
        if any(m in body_lower for m in _ANTIBOT_MARKERS):
            return 'unreachable', status_code, None, 'antibot_in_body'
        if any(m in body_lower for m in _BROKEN_MARKERS):
            return 'broken', status_code, None, 'deleted_marker_in_body'

    if status_code is None or status_code in (0, -1):
        return 'unreachable', None, None, 'no_response'
    if 200 <= status_code < 300:
        return 'ok', status_code, None, None
    if status_code in (301, 302, 303, 307, 308):
        return 'redirect', status_code, final_url, None
    if status_code in (404, 410):
        return 'broken', status_code, None, None
    # v6.4.6 : 5xx → unreachable (avant : 'broken'). Run d8fb6e2 a montré
    # que ScrapingBee Premium peut renvoyer 500 systematic sur Homegate
    # (DataDome via SB ou SB-side failure indistinguable). Classer en
    # 'broken' produit des false positives qui dépublieraient à tort.
    # Safer default sur incertitude : 'unreachable' = log silencieux, pas
    # de masquage. Une vraie panne agence se manifestera sur plusieurs runs
    # successifs et sera escaladée manuellement.
    if 500 <= status_code < 600:
        return 'unreachable', status_code, None, f'server_error_{status_code}'
    if status_code == 403:
        return 'unreachable', 403, None, 'forbidden_403'
    return 'unreachable', status_code, None, f'unexpected_{status_code}'


# ============================================================
# Per-URL checkers
# ============================================================

def _check_via_head(url: str, throttler: _DomainThrottler) -> dict:
    """HEAD direct (gratuit). Pour URLs non-Homegate."""
    domain = _domain_of(url)
    throttler.wait(domain)
    try:
        r = requests.head(
            url,
            headers=_HEAD_HEADERS,
            timeout=_HEAD_TIMEOUT_S,
            allow_redirects=False,
        )
    except requests.Timeout:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': 'timeout'}
    except requests.ConnectionError as e:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': f'conn_err: {str(e)[:120]}'}
    except Exception as e:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': f'{type(e).__name__}: {str(e)[:120]}'}

    # 405 Method Not Allowed sur HEAD : fallback GET avec Range pour
    # télécharger juste le 1er KB (au lieu du body complet).
    if r.status_code == 405:
        return _check_via_get_range(url, throttler)

    final_url = None
    if 300 <= r.status_code < 400:
        loc = r.headers.get('Location') or r.headers.get('location')
        if loc:
            # URL relative → préfixer avec scheme + domain de l'URL source
            if loc.startswith('/'):
                parsed = urlparse(url)
                final_url = f"{parsed.scheme}://{parsed.netloc}{loc}"
            else:
                final_url = loc

    status, code, furl, err = _classify(r.status_code, body=None, final_url=final_url)
    return {'status': status, 'http_code': code, 'final_url': furl, 'error_msg': err}


def _check_via_get_range(url: str, throttler: _DomainThrottler) -> dict:
    """Fallback pour serveurs qui refusent HEAD (HTTP 405). On télécharge
    seulement les 1er 1024 octets via Range header pour minimiser la BP."""
    domain = _domain_of(url)
    throttler.wait(domain)
    try:
        r = requests.get(
            url,
            headers={**_HEAD_HEADERS, 'Range': 'bytes=0-1023'},
            timeout=_HEAD_TIMEOUT_S,
            allow_redirects=False,
        )
    except requests.Timeout:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': 'timeout_get_fallback'}
    except Exception as e:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': f'get_fallback_{type(e).__name__}'}

    final_url = r.headers.get('Location') if 300 <= r.status_code < 400 else None
    status, code, furl, err = _classify(r.status_code, body=None, final_url=final_url)
    return {'status': status, 'http_code': code, 'final_url': furl, 'error_msg': err}


def _check_via_scrapingbee_premium(url: str) -> dict:
    """Pour Homegate uniquement (DataDome bloque tout le reste). ~11 crédits."""
    try:
        with sb_bypass_cache():
            status_code, html = _sb_get(url, render_js=False, premium_proxy=True)
    except Exception as e:
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': f'sb_exception_{type(e).__name__}'}

    if status_code in (0, -1):
        return {'status': 'unreachable', 'http_code': None,
                'final_url': None, 'error_msg': 'sb_no_response'}

    status, code, furl, err = _classify(status_code, body=html, final_url=None)
    return {'status': status, 'http_code': code, 'final_url': furl, 'error_msg': err}


# ============================================================
# DB I/O
# ============================================================

def _create_run(conn) -> int:
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO qa_runs (run_type, status, metadata)
            VALUES ('link_check', 'running', %s::jsonb)
            RETURNING id
        """, (json.dumps({'max_urls': _MAX_URLS_PER_RUN}),))
        row = cur.fetchone()
    finally:
        cur.close()
    conn.commit()
    rid = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(rid)


def _finalize_run(conn, run_id: int, status: str, urls_checked: int,
                  unreachable_count: int, metadata: dict) -> None:
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE qa_runs
            SET status = %s,
                completed_at = NOW(),
                listings_processed = %s,
                errors_count = %s,
                metadata = %s::jsonb
            WHERE id = %s
        """, (status, urls_checked, unreachable_count, json.dumps(metadata), run_id))
    finally:
        cur.close()
    conn.commit()


def _select_urls_to_check(conn, max_urls: int, age_threshold: datetime) -> list:
    """Ordre `last_checked_at NULLS FIRST` ASC pour prioriser les jamais
    checkés puis les plus anciens. is_active=TRUE pour skip les annonces
    désactivées. source_url NOT NULL pour skip ce qu'on peut pas checker."""
    cur = conn.cursor()
    selected = []
    try:
        cur.execute("""
            SELECT id, source_url,
                   LOWER(COALESCE(source, '')) AS src_lower,
                   scraped_at
            FROM properties
            WHERE is_active = TRUE
              AND source_url IS NOT NULL
              AND (last_checked_at IS NULL OR last_checked_at < %s)
            ORDER BY last_checked_at NULLS FIRST
            LIMIT %s
        """, (age_threshold, max_urls))
        for r in cur.fetchall():
            selected.append({
                'id':         _row_get(r, 'id', 0),
                'source_url': _row_get(r, 'source_url', 1),
                'source':     _row_get(r, 'src_lower', 2),
                'scraped_at': _row_get(r, 'scraped_at', 3),
            })
    finally:
        cur.close()
    return selected


def _record_check(conn, prop_id: int, source_lower: str, url: str,
                  result: dict) -> None:
    """Insère qa_link_checks + UPDATE properties.last_checked_at en une
    transaction (atomicity : pas de check enregistré sans last_checked_at
    correspondant ou inversement)."""
    cur = conn.cursor()
    try:
        # portail = source DB d'abord, fallback sur 1er level domain
        portal = source_lower or _domain_of(url).split('.')[-2:][0] or 'unknown'
        cur.execute("""
            INSERT INTO qa_link_checks
                (listing_id, portal, url, http_status, ok,
                 status, final_url, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            prop_id,
            portal[:50],
            url[:1000],
            result['http_code'],
            result['status'] in ('ok', 'redirect'),
            result['status'],
            result['final_url'],
            result['error_msg'],
        ))
        cur.execute("""
            UPDATE properties
            SET last_checked_at = NOW()
            WHERE id = %s
        """, (prop_id,))
    finally:
        cur.close()
    conn.commit()


# ============================================================
# Entry point
# ============================================================

def run_link_health_check(max_urls: int = None) -> dict:
    """Sélectionne `max_urls` propriétés actives, vérifie chaque URL,
    INSERT qa_link_checks + UPDATE properties.last_checked_at.

    Retourne un dict de stats pour log/cron exit code :
        {
            'run_id':              int,
            'urls_checked':        int,
            'counts':              {'ok': N, 'redirect': N, 'broken': N, 'unreachable': N},
            'cache_hits_homegate': int,  # skip ScrapingBee via optim (e)
            'sb_credits_estimated': int,
            'unreachable_pct':     float,
            'elapsed_s':           float,
        }

    Statuts qa_runs :
        - 'success' si urls_checked > 0 et unreachable_pct <= 5%
        - 'partial' si unreachable_pct > 5% (alerte DataDome possible)
        - 'failed'  si urls_checked == 0 (rien sélectionné, ou conn morte
          avant le 1er INSERT)
    """
    if max_urls is None:
        max_urls = _MAX_URLS_PER_RUN

    log.info(
        f"[link-health] start max_urls={max_urls} age_threshold={_AGE_THRESHOLD_DAYS}d "
        f"LINK_HEALTH_AUTO_HIDE={LINK_HEALTH_AUTO_HIDE}"
    )
    t_start = time.time()

    age_threshold = datetime.now(timezone.utc) - timedelta(days=_AGE_THRESHOLD_DAYS)

    # ---------- 1) Open qa_runs row ----------
    run_id = None
    conn = None
    conn_broken = False
    try:
        conn = get_db()
        run_id = _create_run(conn)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[link-health] create_run transport error: {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # ---------- 2) Select URLs ----------
    selected = []
    conn = None
    conn_broken = False
    try:
        conn = get_db()
        selected = _select_urls_to_check(conn, max_urls, age_threshold)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[link-health] select transport error: {e}", exc_info=True)
        # Finalize run as failed et raise
        _safe_finalize_failed(run_id, str(e)[:200])
        raise
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    if not selected:
        log.info("[link-health] no URLs eligible for re-check")
        elapsed_s = round(time.time() - t_start, 1)
        _safe_finalize_failed(run_id, 'no_urls_selected', elapsed_s=elapsed_s)
        return {
            'run_id': run_id, 'urls_checked': 0,
            'counts': {'ok': 0, 'redirect': 0, 'broken': 0, 'unreachable': 0},
            'cache_hits_homegate': 0, 'sb_credits_estimated': 0,
            'unreachable_pct': 0.0, 'elapsed_s': elapsed_s,
        }

    # ---------- 3) Process each URL ----------
    throttler = _DomainThrottler()
    counts = {'ok': 0, 'redirect': 0, 'broken': 0, 'unreachable': 0}
    cache_hits = 0
    sb_credits = 0
    db_write_errors = 0

    conn = None
    conn_broken = False
    try:
        conn = get_db()
        for prop in selected:
            url = prop['source_url']
            prop_id = prop['id']
            source_lower = prop['source'] or ''
            scraped_at = prop['scraped_at']

            # Décide du chemin
            if _is_homegate(source_lower, url):
                # Optimization (e) : skip Premium si scraped_at récent
                if scraped_at:
                    sa = scraped_at if scraped_at.tzinfo else scraped_at.replace(tzinfo=timezone.utc)
                    if sa >= age_threshold:
                        result = {
                            'status': 'ok',
                            'http_code': None,
                            'final_url': None,
                            'error_msg': 'cached_via_prod_scrape',
                        }
                        cache_hits += 1
                    else:
                        result = _check_via_scrapingbee_premium(url)
                        sb_credits += _SB_CREDITS_PER_PREMIUM_CALL
                else:
                    result = _check_via_scrapingbee_premium(url)
                    sb_credits += _SB_CREDITS_PER_PREMIUM_CALL
            else:
                result = _check_via_head(url, throttler)

            counts[result['status']] = counts.get(result['status'], 0) + 1

            try:
                _record_check(conn, prop_id, source_lower, url, result)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                conn_broken = True
                log.error(f"[link-health] DB write transport error prop_id={prop_id}: {e}")
                break  # conn morte, inutile de continuer
            except Exception as e:
                # DB write failed mais conn vivante — log et continue
                db_write_errors += 1
                log.exception(f"[link-health] DB write failed prop_id={prop_id}: {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    # ---------- 4) Finalize run ----------
    elapsed_s = round(time.time() - t_start, 1)
    n = max(len(selected), 1)
    unreachable_pct = round(100 * counts['unreachable'] / n, 1)

    # Statut : partial si unreachable > 5% (signal anti-bot qui passe
    # plus le Premium), success sinon.
    final_status = 'partial' if unreachable_pct > _UNREACHABLE_ALERT_PCT else 'success'

    conn = None
    conn_broken = False
    try:
        conn = get_db()
        _finalize_run(
            conn, run_id, final_status,
            urls_checked=len(selected),
            unreachable_count=counts['unreachable'],
            metadata={
                'urls_checked': len(selected),
                'counts': counts,
                'cache_hits_homegate': cache_hits,
                'sb_credits_estimated': sb_credits,
                'db_write_errors': db_write_errors,
                'unreachable_pct': unreachable_pct,
                'elapsed_s': elapsed_s,
            },
        )
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        conn_broken = True
        log.error(f"[link-health] finalize transport error: {e}")
    finally:
        if conn is not None:
            try:
                return_db(conn, close=conn_broken)
            except Exception:
                pass

    log.info(
        f"[link-health] done urls={len(selected)} "
        f"ok={counts['ok']} redirect={counts['redirect']} "
        f"broken={counts['broken']} unreachable={counts['unreachable']} "
        f"cache_hits_hg={cache_hits} sb_credits={sb_credits} "
        f"unreachable_pct={unreachable_pct}% elapsed={elapsed_s}s"
    )
    if unreachable_pct > _UNREACHABLE_ALERT_PCT:
        log.warning(
            f"[link-health] unreachable rate {unreachable_pct}% > {_UNREACHABLE_ALERT_PCT}% — "
            "DataDome possibly blocking Premium too, investigate ScrapingBee logs"
        )

    return {
        'run_id': run_id,
        'urls_checked': len(selected),
        'counts': counts,
        'cache_hits_homegate': cache_hits,
        'sb_credits_estimated': sb_credits,
        'unreachable_pct': unreachable_pct,
        'elapsed_s': elapsed_s,
    }


def _safe_finalize_failed(run_id: int, error: str, elapsed_s: float = 0.0) -> None:
    """Finalise une run en 'failed' best-effort. N'élève pas — appelé depuis
    des contextes d'erreur où on ne veut pas masquer l'exception originale."""
    if run_id is None:
        return
    conn = None
    try:
        conn = get_db()
        _finalize_run(
            conn, run_id, 'failed',
            urls_checked=0, unreachable_count=0,
            metadata={'error': error[:300], 'elapsed_s': elapsed_s},
        )
    except Exception:
        # Best-effort, pas de re-raise
        pass
    finally:
        if conn is not None:
            try:
                return_db(conn)
            except Exception:
                pass
