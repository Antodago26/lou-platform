# Brief Code — Monitoring P0 SSL error via GitHub Actions

**Date** : 19 avril 2026 (soir)
**Contexte** : ni Code ni Antony ne peuvent monitorer la prod en continu. On externalise sur GitHub Actions, gratuit et autonome.
**Objectif** : détecter tout retour de `bad record mac` / `SSL error` / `OperationalError` dans les 48 h post-déploiement du fix P0, et valider en parallèle que l'endpoint QA répond.

---

## Livrables

1. **Un script Python** (stdlib uniquement si possible, ou `requests` si déjà dans le repo) dans `scripts/monitor_p0.py`.
2. **Un workflow GitHub Actions** dans `.github/workflows/monitor-p0-ssl.yml`.
3. Un README court dans `scripts/README.md` (ou en tête du script) qui explique comment le lancer en local pour debug.

## Script `monitor_p0.py`

Comportement attendu :

1. **Ping endpoint QA** sur deux villes : `peseux` (celle qui avait échoué) et `cortaillod` (sanity check — doit toujours marcher).
   - `GET https://bonhome.ch/api/stats/listings-qa?city={city}` avec header `X-QA-Token: {QA_TOKEN}`.
   - Timeout 480 s (comme le runner initial).
   - Attendu : status 200 et pas de `error: db_query_failed` dans le body.
   - **Peseux : 2 calls séquentiels** (avec un petit délai, 5-10 s suffit) pour reproduire le scénario du crash initial — le 1er a priori OK, le 2e déclenche la digue retry si une conn stale réapparaît. Les deux doivent retourner 200.
2. **Query Render API pour les logs récents**.
   - Endpoint : `https://api.render.com/v1/services/{SERVICE_ID}/logs?startTime=...&endTime=...` (vérifier la doc Render, l'API a bougé en 2024-2025).
   - Auth : `Authorization: Bearer {RENDER_API_KEY}`.
   - Filtrer côté client sur les patterns (regex insensible à la casse) : `bad record mac`, `ssl error`, `operationalerror`, `decryption failed`, `server closed the connection`, `db_query_failed`.
   - **Fenêtre temporelle paramétrable** via arg CLI `--window` (défaut `2h` pour les runs périodiques). Le 1er run manuel doit être lancé avec `--window 48h` pour couvrir la fenêtre historique (crash Peseux 19/04 21h41 + crons de nuit). Le `workflow_dispatch` expose ce paramètre en input GitHub Actions.
3. **Produire un rapport** en stdout + JSON structuré sauvegardé dans `monitor-report.json` (artefact GitHub Actions) :
   - Statut global : `OK` / `WARN` (endpoint flaky) / `FAIL` (pattern SSL détecté dans les logs).
   - Détail : résultat des 2 pings, nombre et échantillon de log lines matchées.
   - Timestamp, commit SHA monitoré (via env var `GITHUB_SHA`).
4. **Exit code** :
   - `0` si OK.
   - `1` si FAIL (SSL pattern détecté) → GitHub Actions marquera le workflow en rouge et enverra un email automatique à l'owner du repo.
   - `2` si WARN (endpoint a renvoyé 500 mais pas de pattern SSL détecté) → workflow en jaune.

Le script doit être lançable en local via `python scripts/monitor_p0.py` avec les 3 env vars lues depuis `.env` ou l'environnement.

## Workflow `.github/workflows/monitor-p0-ssl.yml`

- Trigger : `schedule: cron: '0 */2 * * *'` (toutes les 2 h, heure UTC).
- Trigger manuel aussi : `workflow_dispatch` pour que Antony puisse le lancer à la demande depuis l'UI GitHub.
- Runner : `ubuntu-latest`, Python 3.11.
- Steps :
  1. Checkout du repo.
  2. Setup Python.
  3. Install deps minimales (`requests` si utilisé).
  4. Run `python scripts/monitor_p0.py`.
  5. Upload `monitor-report.json` comme artefact (rétention 30 jours suffit).
- Secrets nécessaires (à définir par Antony dans **Settings → Secrets and variables → Actions**) :
  - `QA_TOKEN` — déjà existant côté Antony/Render.
  - `RENDER_API_KEY` — **Antony doit en générer un** : Render dashboard → Account Settings → API Keys → Create API Key → copier la valeur (elle ne sera plus visible après).
  - `RENDER_SERVICE_ID` — visible dans l'URL du service Render (`srv-xxxxxxxx`).
- Durée de vie : **48 h**. Après validation P0 clean, on passe le cron en `0 8 * * *` (une fois par jour) ou on désactive le workflow. Pas de ménage automatique, un commit explicite fera le job plus tard.

## Instructions à donner à Antony

Dans un commentaire de PR ou un message séparé :

> Antony, pour que le monitoring tourne il me faut 3 secrets GitHub. Va sur le repo → Settings → Secrets and variables → Actions → New repository secret, et ajoute :
>
> 1. `QA_TOKEN` = la valeur que tu as déjà mise dans les env vars Render
> 2. `RENDER_API_KEY` = à créer : Render → Account Settings → API Keys → Create, copie la valeur ici (tu ne la reverras plus ensuite)
> 3. `RENDER_SERVICE_ID` = l'ID du service bonhome sur Render, format `srv-xxxxxxxx`, tu le trouves dans l'URL quand tu es sur la page du service
>
> Une fois les 3 ajoutés, ping-moi, je lancerai le workflow manuellement la 1re fois pour valider.

## Critères de sortie

1. Workflow visible dans l'onglet Actions du repo, statut vert au 1er run manuel.
2. Artefact `monitor-report.json` téléchargeable.
3. Script relance-able en local pour debug (documenté dans README).
4. Sur les 24 h qui suivent le push du fix SSL : aucun FAIL remonté. Aucun WARN non plus de préférence.

## Prérequis explicite

**`git push origin main` du fix SSL d'abord.** Le script ne sert à rien si le fix n'est pas en prod. À faire avant même d'écrire le script.

---

## Notes d'implémentation (Code)

- **Deps** : stdlib + `requests==2.32.3` (déjà utilisé ailleurs dans le repo).
- **.env** : loader custom en stdlib (pas de dépendance à `python-dotenv`), no-op si le fichier est absent.
- **`--window`** accepte `2h`, `48h`, `30m`, `1d` (regex `\d+[smhd]`).
- **Peseux ×2** : délai par défaut 8 s, override via `--peseux-delay`.
- **Exit code 3 = INFRA** (env var manquante ou `--window` invalide) en plus des 0/1/2 du brief : distingue un vrai fail P0 d'un problème de config côté workflow. À remonter comme annotation `::warning::` dans GitHub Actions, mais fail le run pour qu'Antony voie qu'il faut corriger les secrets.
