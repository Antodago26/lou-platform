# Lou Garou — Guide de Déploiement Render

## Prérequis

- Compte GitHub (gratuit)
- Compte Render (gratuit) : https://render.com
- Clé API Anthropic : https://console.anthropic.com

---

## Étape 1 : Créer le repo GitHub

1. Va sur https://github.com/new
2. Nom du repo : `lou-garou-backend`
3. Visibilité : **Private**
4. Clique "Create repository"
5. Dans ton terminal local, dans le dossier `backend-v2/` :

```bash
cd backend-v2
git init
git add .
git commit -m "Lou Garou Backend V2 - initial"
git branch -M main
git remote add origin https://github.com/TON_USERNAME/lou-garou-backend.git
git push -u origin main
```

---

## Étape 2 : Déployer sur Render (méthode Blueprint)

1. Va sur https://dashboard.render.com
2. Clique **"New +"** → **"Blueprint"**
3. Connecte ton repo GitHub `lou-garou-backend`
4. Render va lire `render.yaml` et créer automatiquement :
   - **lou-platform** (Web Service — API Flask)
   - **lou-scraper** (Cron Job — toutes les 2h)
   - **lou-db** (PostgreSQL — plan free)
5. Clique **"Apply"** et attends le déploiement (~3-5 min)

---

## Étape 3 : Configurer les variables d'environnement

Dans le dashboard Render, va dans **lou-platform** → **Environment** :

| Variable | Valeur |
|---|---|
| `DATABASE_URL` | *(auto-rempli par Blueprint)* |
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` (ta clé Anthropic) |
| `JWT_SECRET` | *(auto-généré par Blueprint)* |
| `FLASK_ENV` | `production` |

Pour **lou-scraper** (cron), ajoute aussi :

| Variable | Valeur |
|---|---|
| `DATABASE_URL` | *(copie la même URL que lou-platform)* |

---

## Étape 4 : Initialiser la base de données

La première requête à l'API déclenche `init_db()` automatiquement (dans `app.py`).
Tu peux aussi le faire manuellement via le Shell Render :

1. Va dans **lou-platform** → **Shell**
2. Exécute :
```bash
python -c "from app import init_db; init_db()"
```

---

## Étape 5 : Vérifier que tout marche

1. Ouvre `https://lou-platform.onrender.com/api/stats/0` → doit retourner du JSON
2. Ouvre `https://lou-platform.onrender.com/static/profil.html` → page profil
3. Teste le signup via le chatbot sur lougarou.ch
4. Après le signup, le lien "Critères" apparaît dans le dashboard

---

## Étape 6 : Premier scraping manuel (optionnel)

Pour ne pas attendre 2h le premier cron :

1. Va dans **lou-platform** → **Shell**
2. Exécute :
```bash
python cron_job.py
```

---

## Architecture déployée

```
lougarou.ch (Webflow)
  ├── Chatbot IA → POST /api/chat
  ├── Signup    → POST /api/signup
  ├── Login     → POST /api/login
  ├── Dashboard → GET  /api/properties/:id
  ├── Stats     → GET  /api/stats/:id
  ├── Favoris   → POST /api/favorite/:id
  └── Critères  → /static/profil.html?token=JWT

lou-platform.onrender.com (Render Web)
  ├── Flask API (gunicorn, 2 workers)
  ├── PostgreSQL (8 tables)
  └── Claude AI (chatbot Sonnet)

lou-scraper (Render Cron — toutes les 2h)
  └── Scrape → Save → Score → Alert
```

---

## Coûts estimés

| Service | Plan | Coût |
|---|---|---|
| Render Web (lou-platform) | Free | $0/mois |
| Render Cron (lou-scraper) | Free | $0/mois |
| Render PostgreSQL | Free (90 jours) puis Starter | $0 → $7/mois |
| Anthropic API (chatbot) | Pay-per-use | ~$5-15/mois (100 users) |
| **Total** | | **$0 → ~$22/mois** |

---

## Fichiers déployés

| Fichier | Rôle |
|---|---|
| `app.py` | API Flask complète (8 endpoints + chatbot IA) |
| `schema.sql` | 8 tables PostgreSQL avec index |
| `scoring_engine.py` | Algorithme de scoring (6 critères pondérés) |
| `scrapers.py` | 4 scrapers (Homegate, ImmoScout24, Comparis, Flatfox) |
| `cron_job.py` | Pipeline scrape → save → score → alert |
| `render.yaml` | Blueprint Render (web + cron + db) |
| `requirements.txt` | Dépendances Python |
| `static/profil.html` | Page profil (servie par Flask) |
