# Brief Code — P1 : Architecture endpoint QA → cache nocturne

**Date** : 19 avril 2026 (soir)
**Priorité** : P1 — bloque l'exploitation réelle du QA scrapers sur Neuchâtel + Boudry
**Prérequis** : P0 (SSL error `bad record mac`) déployé et validé en prod. Ne pas enchaîner avant que Antony ait confirmé 24 h de logs clean.

---

## Problème

`GET /api/stats/listings-qa?city=X` fait aujourd'hui du **live scraping synchrone** dans la requête HTTP : 2 portails × 2 transactions (vente + location) = 4 appels ScrapingBee séquentiels, chacun fan-outant vers des détails listings via `ThreadPoolExecutor(4)`. Pour Neuchâtel et Boudry (grosses villes), le temps total dépasse le timeout edge Render/Cloudflare (~200-400 s) → **502 Bad Gateway** systématiques. La scheduled task Cowork à 7h07 CEST est donc aveugle sur ces deux villes, qui sont précisément les deux villes avec le plus d'annonces du canton.

Symptomatique : sur le run du 19/04 à 21h41, le runner Python a reçu 502 après 213 s (1er call) puis 402 s (retry) sur Neuchâtel et Boudry, et 500 SSL sur Peseux — ce dernier est traité par le fix P0, les 502 restent.

## Objectif

Transformer l'endpoint QA en **lecture simple d'un cache DB** alimenté par un job cron nocturne. Cibles :

1. `GET /api/stats/listings-qa?city=X` répond sous **1 seconde** (lecture DB pure), zéro 502.
2. La scheduled task Cowork à 7h07 locale (CEST) récupère des données calculées le jour même.
3. Historique queryable — on peut tracker l'évolution du recall par ville/portail/transaction dans le temps, base pour alerting futur.

---

## Architecture proposée

### 1. Nouvelle table `qa_recall_daily`

```sql
CREATE TABLE qa_recall_daily (
  id            BIGSERIAL PRIMARY KEY,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  computed_date DATE        NOT NULL,  -- pour index par jour, cheap
  city_slug     TEXT        NOT NULL,
  portal        TEXT        NOT NULL,  -- 'homegate' | 'immoscout24' | ...
  transaction   TEXT        NOT NULL,  -- 'rent' | 'buy'
  live_count    INT         NOT NULL,
  matched_in_db INT         NOT NULL,
  recall        NUMERIC(4,3) NOT NULL,  -- 0.000 à 1.000
  missing_ids   JSONB       NOT NULL DEFAULT '[]'::jsonb,
  error_status  TEXT,                   -- NULL si OK, sinon 'scrape_failed'|'timeout'|...
  error_msg     TEXT,
  duration_ms   INT
);
CREATE INDEX idx_qa_recall_lookup ON qa_recall_daily (city_slug, computed_date DESC);
CREATE INDEX idx_qa_recall_portal ON qa_recall_daily (portal, transaction, computed_date DESC);
```

Pourquoi pas un `UNIQUE (city_slug, portal, transaction, computed_date)` : on veut pouvoir tolérer plusieurs runs dans la même journée (manuel + cron). L'endpoint retourne le plus récent par `(city_slug, portal, transaction)`.

Note migration : le prompt de reprise mentionne un problème `migrations au module-load` en backlog 🟡. À ne pas réintroduire ici — utiliser le mécanisme de migration déjà en place (Alembic, flyway, SQL brut, à toi de voir) ou une migration dédiée exécutée explicitement.

### 2. Job cron nocturne `jobs/qa_recall.py`

Pseudo-contrat :

```python
# Tourne à 06:30 UTC (≈ 08:30 CEST été / 07:30 CEST hiver).
# La scheduled task Cowork tourne à 07:07 locale → on a une marge confortable.
#
# Pour chaque (city, portal, transaction) dans qa_targets :
#   1. Appelle le scraper live (Homegate / ImmoScout24 / ...) → liste IDs vus
#   2. Query la DB bonhome → IDs indexés pour cette ville/portail/transaction
#   3. Compute recall + missing_ids (limiter missing_ids à 20 samples max pour
#      pas bloater la table)
#   4. INSERT dans qa_recall_daily
#
# Parallélisation interne : semaphore asyncio ou ThreadPoolExecutor avec
# max 4-6 concurrent requests vers ScrapingBee (pas plus, on partage le budget
# crédits avec le scraping prod). À profiler.
#
# Gestion erreurs : une ville qui foire doit INSERT un row avec error_status,
# pas planter le job entier. L'endpoint doit pouvoir dire "Neuchâtel a foiré hier,
# voici la dernière valeur connue" plutôt que d'être muet.
```

### 3. Mécanisme de déclenchement cron

Trois options, à **trancher explicitement** dans le commit (cf. questions ouvertes ci-dessous) :

| Option | Avantages | Inconvénients |
|---|---|---|
| **Render cron service** | Natif Render, zéro infra à ajouter, logs unifiés | Render facture les cron services à l'heure ($$ ?). Vérifier plan actuel. |
| **Endpoint interne + ping externe** | Simple, gratuit (Cowork scheduled task peut ping). | Auth à bien gérer (token dédié), logique dans le web process, occupe un worker le temps du job. |
| **APScheduler / Celery beat in-process** | Pas de dépendance externe. | Double exécution si gunicorn a 2 workers et la lib n'est pas bien configurée. Bug vu plein de fois. |

**Ma reco** : option 1 si le plan Render le permet, sinon option 2 (endpoint `POST /internal/cron/qa-recall` protégé par `X-Internal-Cron-Token`, pingué par un cron GitHub Actions qui tourne 24/7 gratuit, ou par la scheduled task Cowork elle-même 30 min avant le rapport matinal).

### 4. Refactor `/api/stats/listings-qa`

L'endpoint devient :

```python
@router.get("/api/stats/listings-qa")
def listings_qa(city: str, days: int = 1, x_qa_token: str = Header(...)):
    check_token(x_qa_token)
    rows = db.fetch("""
        SELECT DISTINCT ON (portal, transaction)
               portal, transaction, live_count, matched_in_db,
               recall, missing_ids, computed_at, error_status, error_msg
        FROM qa_recall_daily
        WHERE city_slug = %s
          AND computed_date >= CURRENT_DATE - %s
        ORDER BY portal, transaction, computed_at DESC
    """, (city, days - 1))
    if not rows:
        return {"city": city, "status": "no_data",
                "message": "QA run hasn't produced data for this city yet.",
                "hint": "Last cron job: check /internal/cron/qa-recall/last-run"}
    return build_response(city, rows)
```

Plus de scraping live dans l'endpoint. **Strict.**

Paramètre `days` (optionnel, default 1) pour permettre à la scheduled task Cowork de lire une fenêtre de 2-3 jours si le dernier cron a foiré → dégradation gracieuse.

### 5. Liste des villes cibles

Pour la bêta, 5 villes :
- `cortaillod`
- `colombier-ne`
- `neuchatel`
- `boudry`
- `peseux`

Mettre cette liste soit dans une table `qa_targets` (extensible sans code change), soit dans un fichier de config. À toi de voir — si c'est en config, documenter clairement où l'éditer.

---

## Questions ouvertes à trancher par Code

Je ne tranche pas, tu connais le code mieux que moi. Réponds dans le PR ou dans un message avant d'implémenter :

1. **Scheduler cron** : Render cron service (option 1) ou endpoint interne + ping externe (option 2) ? Si option 1, vérifier le plan Render. Si option 2, choisir qui ping (GitHub Actions ? scheduled task Cowork prépositionnée ?).
2. **Parallélisation** : semaphore de combien ? Par défaut je dirais 4 pour rester conservateur sur le budget ScrapingBee. À profiler sur un run réel.
3. **Liste des villes** : table DB ou config file ? Impact sur onboarding de nouvelles villes post-bêta.
4. **Historique** : on garde tout l'historique (croissance linéaire lente, ~5 villes × 2 portails × 2 tx × 365 jours = 7300 rows/an, trivial) ou on met un TTL à 90 jours ? Ma reco : tout garder.
5. **Retry par ville** : si Homegate Neuchâtel foire, on retry 1× dans le même job ou on skip et on réessaie demain ? Ma reco : retry 1× avec 30s de délai, sinon skip.

---

## Critères de sortie

1. Migration `qa_recall_daily` appliquée en prod, idempotente.
2. Job `qa_recall.py` tourne en local (commande `python -m jobs.qa_recall` ou équivalent) sur les 5 villes en < 5 min total. Output : rows insérées.
3. Cron déclenché à 06:30 UTC chaque jour, confirmé par un run réel.
4. `GET /api/stats/listings-qa?city=neuchatel` retourne un 200 sous 1 s, avec des données fraîches du jour.
5. `GET /api/stats/listings-qa?city=boudry` idem.
6. La scheduled task Cowork 7h07 récupère des données non vides sur les 5 villes (à valider le lendemain de la mise en prod).
7. Un commit avec un message qui décrit l'architecture choisie (scheduler, parallélisation, liste de villes).

---

## Flags / pièges

- **Budget ScrapingBee** : le job ajoute 5 villes × 2 portails × 2 tx = 20 recherches/jour + les détails listings. Vérifier que ça reste dans le budget mensuel. Si tight, envisager de ne lancer le job que 1 jour sur 2 ou de réduire les villes.
- **Neon free tier autosuspend** : le job doit tenir plusieurs minutes actif, il va tenir la compute Neon éveillée pendant son exécution — OK pas de problème.
- **Doublon cron + endpoint** : si tu gardes un fallback live dans l'endpoint (par exemple si `days` query param > N jours → live), tu réintroduis le bug 502. Mon conseil : **pas de fallback live, point**. Si la DB n'a pas de data, on renvoie `status: "no_data"` et l'appelant se débrouille.
- **Fix P0 dépendance** : le job va appeler `_sb_get` et donc `_CACHE_CONN`. Le fix P0 de Code passe `_CACHE_CONN` en `threading.local()`. Si le job tourne en mono-thread, OK. Si tu le parallélises avec `ThreadPoolExecutor`, chaque thread aura sa conn — OK aussi, c'est exactement le fix. Valider qu'il n'y a pas de régression.
- **Migration fork** : le prompt de reprise mentionne un point 🟡 "migrations au module-load" — ne pas exécuter la migration au module-load, faire une commande explicite. Sinon un redéploiement pendant que la bêta tourne peut figer la DB.

---

## À transmettre à Code verbatim

Ce document entier.

## À rapporter à Cowork avant de commit

- Réponses aux 5 questions ouvertes.
- Estimation en heures : diag + impl + tests.
- Tout edge case que tu identifies et que je n'ai pas anticipé.
