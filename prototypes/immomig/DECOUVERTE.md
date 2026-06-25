# Découverte de masse — backends des agences suisses (échantillon 88)

Détection automatique (`backend_detector.py`) sur **88 agences réelles** :
52 issues de l'annuaire agences-immobilieres.ch (échantillon neutre romand)
+ 38 clients Immomig confirmés (références officielles), dédupliqués.

> ⚠️ Détection sur **HTML statique uniquement**. Les agences dont le widget immo
> est chargé en **JS async** ressortent « WordPress / sur-mesure » alors qu'elles
> tournent souvent sur Immomig/Apimo. **Le % de backends répétables ci-dessous est
> donc un PLANCHER**, pas un plafond — un rendu headless en convertirait une partie.

## Répartition

| Backend | Nb | % | Répétable ? |
|---|---:|---:|---|
| **Immomig** | 38 | 43% | ✅ 1 parser = 38 agences |
| WordPress (backend immo à confirmer) | 20 | 22% | à requalifier (probable widget JS) |
| Sur-mesure / inconnu | 13 | 14% | non (grandes régies surtout) |
| Injoignable (à réessayer) | 7 | 8% | — |
| Drupal / SPA sur-mesure | 4 | 5% | non |
| **CASASOFT** | 1 | 1% | ✅ répétable |
| **Estatik** | 1 | 1% | ✅ répétable |
| Wix / Webflow / TYPO3 / WPResidence | 4 | 5% | thèmes WP/CMS génériques |

**≥ 45% sur un backend répétable** (Immomig + CASASOFT + Estatik) → couvrables par
**~4 parsers**. En requalifiant les 20 « WordPress » avec un rendu JS, ce chiffre
montera nettement.

## Conclusion opérationnelle (ordre de priorité)

1. **Immomig** — de loin le plus gros lot. Le parser générique (`immomig_generic.py`)
   + l'API SPA (mission freelance) couvre ~43% des agences **immédiatement**.
2. **Requalifier les 20 « WordPress »** avec un rendu headless → en récupérer une
   bonne part sous Immomig/Apimo.
3. **Apimo / CASASOFT / Estatik** — mêmes principes, lots suivants.
4. **Grandes régies sur-mesure** (Bernard-Nicod=Drupal, Rosset=SPA, Gerofinance,
   Immopulse…) → priorité basse : elles sont déjà sur tous les portails, peu de
   « biens cachés ».
5. **Injoignables** (cardeco, janin, sarmolens, tactimmo, vert-immobilier…) → simple
   retry (erreurs SSL/transitoires probables).

## Fichiers
- `agences_priorisees.csv` — la liste complète (domaine, backend, confiance, répétable).
- `domaines_agences.json` — les 88 domaines bruts (réutilisable / extensible).

## Pour étendre à des centaines d'agences
L'annuaire n'a donné que ~54 fiches. Sources à ajouter pour scaler la découverte :
USPI (membres par canton), SVIT, annuaires cantonaux, Google Maps « régie immobilière
+ ville », clients listés sur les sites d'Apimo / CASASOFT. Le détecteur encaisse
n'importe quelle liste : `python3 backend_detector.py dom1.ch dom2.ch ...`.
