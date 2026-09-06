# Audit Bonhome pré-bêta — 2026-04-18

Synthèse de 4 audits parallèles (security, scrapers, DB, archi+observability) sur `backend-v2/`.
Cible : beta 15 testeurs. Focus : ce qui saigne avant d'ouvrir.

**TL;DR** — le code est globalement propre côté SQLi/XSS/CORS (audits v6.3 ont payé), mais 4 catégories de risques bloquent la beta :
1. `/api/chat` est un **trou budgétaire Anthropic** (pas d'auth, rate limit bypassable).
2. **Zéro monitoring** (Sentry, health DB, alertes scrapers par ville) — en beta on perd les erreurs dans le vide.
3. **Homegate = SPOF** (un seul sélecteur CSS) + pas de retry 403/429 → catalog vide silencieux.
4. **Postgres free tier sans backup** + boot-rescore × workers = risque data + spike DB à chaque redeploy.

---

## 🔴 BLOQUANT BETA (fix avant ouverture)

### Sécurité / budget

| # | Fichier | Issue | Action |
|---|---------|-------|--------|
| S1 | `routes_chat.py:245,410,442` | `/api/chat`, `/api/chat/reset`, `/api/chat/unresolved-choice` **non-auth** + `session_id` user-fourni → spam Anthropic (budget killer), lecture croisée conversations anon | Exiger JWT OU signer `session_id` côté serveur (HMAC) ; rate-limit par IP + JWT |
| S2 | `routes_chat.py:31-40` | Rate limiter in-memory **par worker** (×2 gunicorn), reset au redeploy, clé = `session_id` client-fourni → contournable en incrémentant cookie | Passer à `flask-limiter` + Redis (ou fallback memory avec key IP) |
| S3 | `auth.py:327` + signup `:227` | **Aucun rate limit** sur `/login` et `/signup`. 409 vs 401 permet l'énumération email. hCaptcha seulement sur signup et seulement si `HCAPTCHA_SECRET` set | `flask-limiter` 5/min/IP sur login ; harmoniser la réponse 401 anti-énumération |
| S4 | `static/app.js:10,391` + auth.py | JWT 7j en **localStorage** → toute XSS = session volée 7j. Aucune révocation serveur | Cookie `httpOnly+Secure+SameSite=Lax` OU baisser à 24h + refresh + table `revoked_tokens` |
| S5 | `auth.py:37-40` | `HCAPTCHA_SECRET`, `RESEND_API_KEY`, `ADMIN_EMAIL`, `CRON_SECRET` avec fallback `''` silencieux en prod → captcha off, emails perdus, cron endpoint open access | Fail-fast en prod (pattern déjà appliqué à `JWT_SECRET`) |

### Observabilité

| # | Fichier | Issue | Action |
|---|---------|-------|--------|
| O1 | (global) | **Zéro Sentry/Rollbar**. En beta une 500 disparaît dans Render logs | `pip install sentry-sdk[flask]`, init dans `app.py`, DSN via env |
| O2 | `routes_pages.py:49` | `/health` renvoie `{"status":"ok"}` statique — DB down = HEAD / toujours 200 | Ajouter `SELECT 1` + état pool dans `/health` |

### Scrapers (catalog integrity)

| # | Fichier | Issue | Action |
|---|---------|-------|--------|
| SC1 | `scrapers.py:516-517` | Homegate = **single CSS selector** `[data-test="result-list-item"]`. Changement HTML = 0 listings silencieux jusqu'à J+1 | Ajouter parse `__NEXT_DATA__`/`__INITIAL_STATE__` en premier, HTML en fallback (pattern IS24) |
| SC2 | `scrapers.py:279-285` | `_sb_get` retry **seulement** sur 500/502/504. Un 403/429 ScrapingBee = return immédiat | Sur 403/429 retry avec `premium_proxy=true` (~10 lignes) |
| SC3 | `scrapers.py:513,658,1699,1202` | `scrape_all` **break-on-fail** : page N en erreur = pages N+1..max perdues | Remplacer `break` par `continue` + compteur |
| SC4 | `cron_job.py:374` | Sentinelle **globale** (`count == 0` all cities). Homegate NE vide mais Lausanne OK → aucune alerte | Sentinelle par `(scraper, ville)` OU variation vs moyenne 7j |

### DB / Infra

| # | Fichier | Issue | Action |
|---|---------|-------|--------|
| DB1 | `app.py:338` | `_rescore_all_on_boot` lancé par **chaque worker** gunicorn → 2× travail identique, ~150 k UPSERTs au moindre redeploy avec 15 testeurs | Leader election via `pg_advisory_lock` (clé fixe) OU env flag `BOOT_RESCORE_LEADER` OU déplacer en cron externe |
| DB2 | `app.py:92` vs `schema.sql` | `property_sources` référencée ligne 92 **mais jamais créée dans schema.sql** (créée lazy dans `scrapers.py:2538`). UPDATE casse silencieusement si scrape pas encore tourné | Ajouter `CREATE TABLE property_sources` dans `schema.sql` |
| DB3 | `render.yaml:40` | Postgres plan=**free** → pas de backup automatique. Comptes users réels | Upgrade Neon Launch ($7/mo) ou Render starter AVANT ouverture beta |
| DB4 | `app.py:338` + `routes_scraping.py:150,590` | Threads daemon sans handler SIGTERM → Render redeploy tue mid-transaction | Ajouter `signal.signal(SIGTERM)` avec `event.set()` + join timeout |

### Runtime

| # | Fichier | Issue | Action |
|---|---------|-------|--------|
| R1 | `render.yaml:10` | gunicorn `--workers 2 --timeout 300` **sync**. ScrapingBee bloque 30s/appel → 2 requêtes simultanées max. 15 testeurs clics parallèles = timeouts | `--workers 2 --threads 4 --worker-class gthread` (changement 1 ligne) |

---

## 🟡 À RÉGLER SOUS 2 SEMAINES

### Security

- **[CSP]** `app.js` fait massivement `innerHTML`. `escapeHtml` est utilisé aux bons endroits (titles/descriptions) mais il n'y a **aucune CSP**. Ajouter `Content-Security-Policy: default-src 'self'; script-src 'self'; ...` dans `app.py:183-192`.
- **[RGPD/LPD]** `unresolved_locations` loggue `query` + `user_id` + `anon_session_id` sans mention privacy. `localStorage lou_anon_*` sans bandeau consentement. Ajouter banner + clause dans `/privacy`. Hasher `anon_session_id` si pas besoin de retraçabilité humaine.
- **[token query string]** `auth.py:77` `token_required` accepte `?token=` → loggué par Render/proxies. Retirer.
- **[email HTML injection]** `auth.py:187-209 notify_new_signup` injecte `{user_name}` brut en HTML. `html.escape()`.
- **[f-string SQL]** `routes_properties.py:103,136,293,409,507` + `routes_alerts.py:108` : actuellement whitelist-safe mais pattern fragile. Refactor helper `safe_execute`.

### DB

- **[FK manquante]** `conversations.user_id` sans `REFERENCES users(id)` (`schema.sql:151`). Orphelins à la suppression user.
- **[Index]** Ajouter `idx_prop_active_tx (is_active, transaction, price) WHERE is_active` — query boot rescore la cherche (`app.py:296`).
- **[Index]** `unresolved_locations(user_id)` manquant → full scan à la suppression user.
- **[Pool]** `db.py:17-18` POOL_MAX=10 × workers. Ajouter un log quand `pool.getconn()` dépasse 80 %.
- **[UPDATE redondant]** `app.py:80-113` réécrit les URLs IS24 **à chaque boot de chaque worker**. Gate par check `WHERE url NOT LIKE 'https://www.immoscout24%'`.

### Scrapers

- **[Dedup cross-portail]** `_find_cross_portal_duplicate` (`scrapers.py:2561`) : branche sans surface (L2588-2597) matche n'importe quel bien même prix/pièces/ville → 2 appart 3.5p 2100 CHF à Lausanne fusionnés. Exiger surface OU ajouter NPA/adresse.
- **[Credits ScrapingBee]** Header `Spb-Remaining-Credits` jamais lu. Logguer + alerter à <10 %.
- **[Cron silent fail]** `cron_job.py:362` catch-all rollback ; aucun compteur envoyé dans l'email. Inclure `failed_cities` dans `_send_scraper_alert`.

### Archi / code quality

- **[Dup INSERT scored_properties]** même gros INSERT à `app.py:311-328`, `routes_scraping.py:113`, `auth.py:544+`. Extraire dans `scoring_engine.upsert_scored_property()`.
- **[Migrations au module-load]** `app.py:42-160` = 120 lignes de DDL inline à chaque cold start. Déplacer dans `manage.py migrate`, gardes par `migrations_applied`.
- **[Logs non structurés]** Pas de `user_id`/`request_id` consistant. `logging.basicConfig` → JSON formatter + middleware `before_request` qui set un UUID.
- **[`except Exception: pass`]** `scoring_engine.py:365/390/414`, `auth.py:603/605`, `routes_properties.py:168/528` — masquage silencieux. Logguer avec `exc_info=True`.

---

## 🟢 TECH DEBT / POST-BETA

- **Cache-busting manuel** `?v=20260417h` — middleware Flask auto-mtime (déjà dans `project_cache_busting_todo.md`). Le bug 2026-04-14 va se répéter.
- **Aucun test** (pas de `tests/`, pas de `pytest.ini`). `scrapers.py` 2700 lignes sans un seul test — ImmoScout HTML change = breakage silent.
- **Requirements pinning mixte** : `requests==2.31.0` a CVE-2024-35195. Bump à `2.32.3`. `anthropic>=0.49.0` / `curl_cffi>=0.7.0` / `cloudscraper>=1.2.71` → pinner.
- **TODO PRICING_ENABLED** dupliqué (`routes_chat.py:286`, `routes_properties.py:89`).
- **X-XSS-Protection** (app.py:188) déprécié, retirer.
- **Surface normalization incohérente** (Homegate L568 sans validation, vs `_clean_surface` L471 qui check 5-5000).
- **Currency hardcoded CHF** `scrapers.py:350` — Properstar peut renvoyer EUR (frontaliers).
- **Canton disambiguation partielle** : `colombier-ne` géré Homegate L504, pas IS24. Centraliser.
- **UA unique** `_direct_get` — ban progressif. Rotation simple (liste 5 UA).
- **`scrape_cache` TTL 24h global** — nouveau listing invisible h+24.
- **Pas de logs audit login OK/KO** → forensique beta impossible.
- **2 workers sync détaillé** cf R1.
- **Rollback migrations** pas scripté — les nouvelles tables (`geo_cache`, `unresolved_locations`, `gps_source`) sont safe à DROP (.get() côté read).

---

## Annexe A — Endpoint QA `/api/stats/listings-qa`

Endpoint de cron externe pour comparer Bonhome vs portails en live.

**Contrat**
```
GET /api/stats/listings-qa?city={slug}
Header: X-QA-Token: <QA_TOKEN>
→ 200 {
    "city": "neuchatel",
    "bonhome_indexed": 142,        # COUNT(*) properties actives
    "bonhome_by_source": {         # breakdown par portail
      "homegate": 48,
      "immoscout24": 62,
      "comparis": 18,
      "acheter-louer": 9,
      "properstar": 5
    },
    "fetched_at": "2026-04-18T14:22:10Z"
}
→ 401 si header absent/faux
→ 404 si city inconnue
```

**Code prêt à commit** (à placer dans `routes_properties.py` ou nouveau `routes_stats.py`) :

```python
# routes_stats.py
import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from db import get_conn, put_conn

bp_stats = Blueprint('stats', __name__)

QA_TOKEN = os.environ.get('QA_TOKEN', '')

@bp_stats.route('/api/stats/listings-qa', methods=['GET'])
def listings_qa():
    if not QA_TOKEN or request.headers.get('X-QA-Token') != QA_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    city = (request.args.get('city') or '').strip().lower()
    if not city:
        return jsonify({"error": "city required"}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS total
            FROM properties
            WHERE is_active = TRUE AND LOWER(city) = %s
        """, (city,))
        row = cur.fetchone()
        total = (row['total'] if isinstance(row, dict) else row[0]) or 0
        if total == 0:
            # On ne 404 pas (ville peut être valide mais vide) ;
            # le caller distingue 0 vs 404 via is_known_city si besoin.
            pass

        cur.execute("""
            SELECT ps.source, COUNT(DISTINCT p.id) AS n
            FROM properties p
            LEFT JOIN property_sources ps ON ps.property_id = p.id
            WHERE p.is_active = TRUE AND LOWER(p.city) = %s
            GROUP BY ps.source
        """, (city,))
        by_source = {}
        for r in cur.fetchall():
            src = r['source'] if isinstance(r, dict) else r[0]
            n = r['n'] if isinstance(r, dict) else r[1]
            if src:
                by_source[src] = n

        return jsonify({
            "city": city,
            "bonhome_indexed": total,
            "bonhome_by_source": by_source,
            "fetched_at": datetime.now(timezone.utc).isoformat()
        }), 200
    finally:
        put_conn(conn)
```

**Enregistrement** (dans `app.py` imports + `app.register_blueprint`) :

```python
from routes_stats import bp_stats
app.register_blueprint(bp_stats)
```

**Env var à set sur Render** :
```
QA_TOKEN=<32 bytes hex — générer avec python -c "import secrets; print(secrets.token_hex(32))">
```

**Note** : ne renvoie PAS `homegate_live` / `immoscout24_live` depuis ce backend — ça ferait scrape synchrone dans la request (timeout). Le cron externe fait lui-même les fetch portails, puis compare au chiffre Bonhome renvoyé par cet endpoint.

> **Pas commit sans feu vert** — le code est prêt dans ce rapport, dis-moi si je push.

---

## Plan d'attaque recommandé (ordre)

**J0 (avant ouverture beta)** — tout 🔴 sauf S4 (JWT cookie migration = risque régression)
1. S3 + S2 : `flask-limiter` (1 soir)
2. S1 : auth `/api/chat` + HMAC `session_id` (1 soir)
3. S5 : fail-fast secrets prod (30 min)
4. O1 : Sentry init (1h)
5. O2 : `/health` DB check (15 min)
6. SC1 : Homegate `__NEXT_DATA__` fallback (2h)
7. SC2 : retry 403/429 premium_proxy (30 min)
8. SC3 : break→continue (15 min)
9. SC4 : sentinelle par ville (1h)
10. DB1 : leader election boot rescore (30 min, `pg_advisory_lock`)
11. DB2 : `property_sources` dans `schema.sql` (10 min)
12. DB3 : upgrade Neon Launch (click)
13. DB4 : SIGTERM handler threads (1h)
14. R1 : `--worker-class gthread --threads 4` (1 ligne render.yaml)
15. **Endpoint QA** (30 min + feu vert)

Total estimé : ~1.5 jour-dev.

**Semaine 1-2 beta** — tout 🟡, par ordre : CSP → FK + index DB → dedup cross-portail → credits SB → migrations hors boot → logs structurés.

**Post-beta** — 🟢 : tests, cache-busting middleware, pinning, rotation UA.
