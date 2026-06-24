#!/usr/bin/env python3
"""
Prototype : detecteur de backend immobilier pour sites d'agences suisses.
Objectif : prouver que la majorite des agences tournent sur une poignee de
logiciels communs -> 1 parser par backend couvre N agences.

Fetch homepage (+ 1 page listing si trouvee), cherche des empreintes connues,
rend un verdict {backend, confiance, signaux}.
"""
import re
import sys
import concurrent.futures
import requests
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Empreintes : backend -> liste de (regex, signal lisible, poids)
FINGERPRINTS = {
    "Immomig": [
        (r"immomig", "ref 'immomig'", 3),
        (r"immomig\.(com|net|ch)|wsimmo|immomedia", "domaine immomig", 5),
        (r"immomig-spa|mig-search|data-immomig|id=[\"']immomig", "widget SPA immomig", 5),
        (r"estate-search|object-search-immomig|/fr/objects/|/fr/object/", "routes immomig", 2),
    ],
    "Apimo": [
        (r"apimo\.net|cdn\.apimo", "CDN apimo.net", 4),
        (r"data-apimo|apimo-", "attributs apimo", 3),
        (r"/catalog/.*apimo|apimo.*catalog", "catalog apimo", 2),
    ],
    "CASASOFT": [
        (r"casasoft|casagateway|casamatch", "ref casasoft", 4),
        (r"cdn\.casasoft|static\.casasoft", "CDN casasoft", 4),
    ],
    "Estatik": [
        (r"js-es-listing|es-listing|estatik", "classes/plugin estatik", 4),
        (r"/property-category/", "URL estatik", 3),
    ],
    "Houzez (WP)": [
        (r"/themes/houzez", "theme houzez", 4),
        (r"houzez", "ref houzez", 2),
    ],
    "WPResidence (WP)": [
        (r"/themes/wpresidence|wpestate", "theme wpresidence", 4),
    ],
    "RealHomes (WP)": [
        (r"/themes/realhomes|inspiry", "theme realhomes", 4),
    ],
    "ImmoPlus / iHomefinder": [
        (r"ihomefinder|optima-express", "ihomefinder", 3),
    ],
}

# Detection de la couche CMS/SPA (fallback si aucun backend immo precis).
# Ordre = priorite (le 1er qui matche gagne).
CMS_HINTS = [
    (r"/wp-content/|/wp-json/|wp-embed", "WordPress"),
    (r"/sites/default/files|Drupal\.settings|drupal-", "Drupal"),
    (r"_next/static|__NEXT_DATA__", "Next.js (SPA)"),
    (r"__NUXT__|/_nuxt/", "Nuxt (SPA)"),
    (r"typo3temp|/typo3conf/", "TYPO3"),
    (r"wix\.com|wixstatic|_wixCssStates", "Wix"),
    (r"squarespace", "Squarespace"),
    (r"webflow", "Webflow"),
    (r"cdn\.shopify", "Shopify"),
]

# Liens "listing" probables a explorer (1 seul, le 1er trouve)
LISTING_HINTS = [
    "biens", "objets", "annonces", "recherche", "louer", "vente", "acheter",
    "properties", "property-category", "a-louer", "a-vendre", "immobilier",
    "nos-biens", "catalog", "offres",
]


def fetch(url, timeout=18):
    last = None
    # essaie l'URL telle quelle, puis le variant www / sans-www
    host = urlparse(url if url.startswith("http") else "https://" + url).netloc
    variants = [url]
    if not host.startswith("www."):
        variants.append(re.sub(r"://", "://www.", url, count=1))
    for v in variants:
        try:
            r = requests.get(v, headers={"User-Agent": UA, "Accept-Language": "fr-CH,fr;q=0.9"},
                             timeout=timeout, allow_redirects=True)
            return r.status_code, r.text, r.url, dict(r.headers)
        except Exception as e:
            last = f"ERR: {type(e).__name__}"
    return None, last or "ERR", url, {}


def find_listing_link(html, base):
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1)
        low = href.lower()
        if any(h in low for h in LISTING_HINTS) and not low.startswith(("mailto:", "tel:", "#")):
            full = urljoin(base, href)
            if urlparse(full).netloc == urlparse(base).netloc:
                return full
    return None


def detect(blob, headers):
    text = blob + " " + " ".join(f"{k}:{v}" for k, v in headers.items())
    text_low = text.lower()
    scores = {}
    signals = {}
    for backend, fps in FINGERPRINTS.items():
        for rx, label, weight in fps:
            if re.search(rx, text_low):
                scores[backend] = scores.get(backend, 0) + weight
                signals.setdefault(backend, set()).add(label)
    # couche CMS/SPA (premier match)
    cms = None
    for rx, name in CMS_HINTS:
        if re.search(rx, text_low):
            cms = name
            break
    # iframe vers un provider tiers ?
    iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', text_low)
    return scores, signals, cms, iframes


def analyze(url):
    if not url.startswith("http"):
        url = "https://" + url
    code, html, final, headers = fetch(url)
    out = {"url": url, "final": final, "code": code}
    if code is None or not isinstance(html, str) or html.startswith("ERR"):
        out["backend"] = "INJOIGNABLE"
        out["detail"] = html[:120]
        return out

    # combine home + 1 page listing
    blob = html
    listing = find_listing_link(html, final)
    listing_code = None
    if listing and listing != final:
        lc, lhtml, _, lheaders = fetch(listing)
        listing_code = lc
        if isinstance(lhtml, str) and not lhtml.startswith("ERR"):
            blob += "\n" + lhtml
            headers = {**headers, **lheaders}

    scores, signals, cms, iframes = detect(blob, headers)
    # provider via iframe (immomig/apimo/casasoft heberges)
    for ifr in iframes:
        for backend in ("immomig", "apimo", "casasoft"):
            if backend in ifr:
                key = {"immomig": "Immomig", "apimo": "Apimo", "casasoft": "CASASOFT"}[backend]
                scores[key] = scores.get(key, 0) + 5
                signals.setdefault(key, set()).add(f"iframe->{backend}")

    if scores:
        best = max(scores, key=scores.get)
        out["backend"] = best
        out["confiance"] = scores[best]
        out["signaux"] = sorted(signals.get(best, []))
        if cms:
            out["signaux"].append(f"[{cms}]")
    elif cms:
        out["backend"] = f"{cms} (backend immo inconnu)"
        out["confiance"] = 1
        out["signaux"] = [cms]
    else:
        out["backend"] = "SUR-MESURE (inconnu)"
        out["confiance"] = 0
        out["signaux"] = []
    out["listing_url"] = listing
    out["listing_code"] = listing_code
    out["all_scores"] = scores
    return out


SITES = [
    # --- VALIDATION : clients Immomig CONNUS (doivent ressortir Immomig) ---
    "propertyone.ch", "derham.ch", "verbel.ch", "regiegalland.ch",
    "gerances-giroud.ch", "vesa.ch", "bulliard.ch", "rfsa.ch",
    "valimmobilier.ch", "muller-immobilier.ch", "vimova.ch", "elitim.ch",
    "ci-leman.ch", "immocrans.ch", "sovalco.ch", "stalder-immobilier.ch",
    # --- deja en prod chez bonhome (verite terrain) ---
    "jouval.ch", "mulleretchriste.ch", "fidimmobil.ch",
    # --- grandes regies (hypothese : stacks sur-mesure) ---
    "naef.ch", "grange.ch", "rosset.ch", "bernard-nicod.ch",
    "comptoir-immobilier.ch", "domicim.ch",
]

if __name__ == "__main__":
    sites = sys.argv[1:] or SITES
    print(f"Analyse de {len(sites)} sites d'agences...\n")
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(analyze, sites))

    # Tri par backend
    rows.sort(key=lambda r: (r.get("backend", "z"), -r.get("confiance", 0)))
    w = max(len(urlparse(r["url"]).netloc) for r in rows) + 1
    print(f"{'SITE':<{w}} {'BACKEND DETECTE':<32} {'CONF':<5} SIGNAUX")
    print("-" * 100)
    for r in rows:
        dom = urlparse(r["url"]).netloc
        sig = ", ".join(r.get("signaux", [])) or (r.get("detail", "") if r["backend"] == "INJOIGNABLE" else "")
        print(f"{dom:<{w}} {r.get('backend',''):<32} {str(r.get('confiance','-')):<5} {sig[:50]}")

    # Stats agregees
    from collections import Counter
    c = Counter(r.get("backend", "?").split(" (")[0] for r in rows)
    print("\n=== Repartition par backend ===")
    for backend, n in c.most_common():
        print(f"  {n:>2}x  {backend}")
