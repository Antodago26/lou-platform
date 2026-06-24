# Prototype — Pivot scraping « sites d'agences » (backend Immomig)

> ⚠️ **Prototype, non câblé à la prod.** Aucun de ces fichiers n'est importé par
> `app.py` / `cron_job.py`. Ils servent à valider le modèle « 1 parser par
> backend » et à cadrer la mission du freelance.

## Pourquoi
bonhome veut passer de l'agrégation de **portails** (Homegate/ImmoScout, défendus,
ScrapingBee payant, risque CGU) au **scraping direct des sites d'agences** :
non défendus, ~gratuits, juridiquement plus sains, et porteurs de biens « cachés »
jamais publiés sur les portails.

Le verrou : on ne va pas coder 3000 scrapers à la main. Mais la **majorité des
régies suisses tournent sur une poignée de logiciels communs** → 1 parser par
backend couvre des centaines d'agences.

## Ce qui est prouvé (données réelles, juin 2026)

### `backend_detector.py`
Détecte le logiciel immo d'un site d'agence (Immomig, Apimo, CASASOFT, Estatik,
+ couche CMS/SPA). Validé sur **16 clients Immomig connus → 12 détectés direct**
(les 4 ratés = widget chargé en JS async).
**Immomig = backend dominant en Suisse romande.**

```bash
python3 backend_detector.py            # echantillon par defaut
python3 backend_detector.py naef.ch grange.ch ...
```

### `immomig_generic.py`
Parser Immomig **générique** (zéro code spécifique à l'agence). Stratégie en cascade :

| Couche | Méthode | Statut |
|---|---|---|
| 1. Détection + ID client | `immomigimg.ch/.../<ID>/pictures/` | ✅ universel (testé : 116, 133, 627, 922, 1061, 1698) |
| 2. Catalogue complet | `/sitemap_objects_fr.xml` (gzip) + fallback pages liste SSR | ✅ universel |
| 3. transaction / type / ville / ID | parse du slug `/fr/o/a-louer-appartement-…-fribourg-24048` | ✅ universel, statique |
| 4. **prix / pièces / surface** | HTML rendu serveur (sites SSR type rfsa) | ⚠️ **OK en SSR, absent en SPA** |

```bash
python3 immomig_generic.py rfsa.ch bulliard.ch vesa.ch muller-immobilier.ch
```

Exemple **rfsa.ch (SSR)** — biens complets extraits :
```
[location] appartement Marly   CHF 1045        41m2
[location] appartement Bulle   CHF 1220  2.5p  54m2
```

## Le morceau pour le freelance (ce qui reste)
Sur les sites Immomig en **SPA** (ex: bulliard, muller, immocrans), le prix et les
attributs détaillés sont rendus côté client → **pas dans le HTML statique**. Il faut
**reverse-engineer l'API JSON Immomig** (bundle `website.js`, paramétrée par l'ID
client) — une fois trouvée, elle alimente TOUTES les variantes d'un coup, y compris
les SSR. C'est le cœur de la mission Malt.

### Variantes de rendu Immomig identifiées
- **SSR** (rfsa) : tout dans le HTML → déjà géré ici.
- **SPA + JSON inline** (vesa, Nuxt) : données dans un état JSON échappé dans la page.
- **SPA + API** (bulliard, muller) : données via XHR → API à trouver.

## Suite (par ROI décroissant)
1. API Immomig (couvre le plus gros lot d'agences) ← freelance
2. Mêmes principes pour **Apimo**, **CASASOFT/iazi**, **Estatik**
3. IA-parser pour la longue traîne sur-mesure
4. **Découverte** : lister les agences CH + leur page « biens » (gros travail de fond)

## Format de sortie
À aligner sur `_make_property()` de `scrapers.py` (champ `source='agence'`) pour
réutiliser tel quel le scoring, la dédup, la DB et le monitoring santé existants.
