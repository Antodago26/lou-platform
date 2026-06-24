#!/usr/bin/env python3
"""
Parser Immomig GENERIQUE pour bonhome — prototype de production.

Idee : UN module ingere le catalogue de N'IMPORTE QUELLE agence sur Immomig,
sans code specifique a l'agence. Branchable dans scrapers.py (sortie au format
_make_property).

Strategie en cascade (du plus fiable au moins fiable) :
  1. DETECTION  : immomigimg.ch/.../<CLIENT_ID>/... -> c'est de l'Immomig + on a l'ID client.
  2. CATALOGUE  : /sitemap_objects_fr.xml -> TOUTES les URLs de biens (universel, statique, gzip).
  3. SLUG       : /fr/o/{transaction}-{type}-...-{ville}-{ID} -> transaction/type/ville/ID (universel).
  4. ENRICH SSR : prix/surface/pieces depuis la page liste ou objet quand rendu serveur (rfsa-like).
                  Les sites 100% SPA (bulliard-like) n'exposent pas le prix en statique
                  -> champ a None + flag 'needs_api' (= le morceau pour le freelance Malt).
"""
import re
import requests
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PROP_TYPES = {
    "appartement": "appartement", "studio": "studio", "villa": "maison",
    "maison": "maison", "duplex": "appartement", "attique": "appartement",
    "loft": "appartement", "chalet": "maison", "terrain": "terrain",
    "parking": "parking", "place": "parking", "garage": "parking",
    "bureau": "bureau", "local": "commercial", "commerce": "commercial",
    "immeuble": "immeuble", "depot": "commercial", "arcade": "commercial",
}


def _get(url, timeout=20):
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr-CH,fr;q=0.9"},
                         timeout=timeout, allow_redirects=True)
        return r.status_code, r.text, dict(r.headers), r.url
    except Exception as e:
        return None, f"ERR:{type(e).__name__}", {}, url


# 1. DETECTION ----------------------------------------------------------------
def detect_immomig(html, headers=None):
    """Retourne l'ID client Immomig (str) si le site tourne sur Immomig, sinon None."""
    blob = html + " " + " ".join((headers or {}).values())
    # L'ID client est le segment numerique juste avant /pictures|websites|seo/.
    # (immomigimg.ch/i/<hash>/<size>/<m|s|sww...>/<CLIENT_ID>/pictures/...)
    m = re.search(r'immomigimg\.ch/[^"\'\s]*?/(\d+)/(?:pictures|websites|seo)/', blob)
    return m.group(1) if m else None


# 2. CATALOGUE ----------------------------------------------------------------
def discover_objects(base):
    """Liste toutes les URLs de biens. sitemap_objects_fr.xml en priorite,
    fallback : liens /o/ ou /objet/ trouves sur la home + page liste."""
    urls = []
    for sm in ("/sitemap_objects_fr.xml", "/fr/sitemap_objects.xml", "/sitemap.xml"):
        code, txt, _, _ = _get(base + sm)
        if code == 200 and "<loc>" in txt:
            locs = re.findall(r"<loc>([^<]+)</loc>", txt)
            objs = [l for l in locs if re.search(r"/(o|objet|object|bien)/", l)]
            if objs:
                return list(dict.fromkeys(objs))
            # sitemap index -> suit les sous-sitemaps "objects"
            subs = [l for l in locs if "object" in l.lower() and l.endswith(".xml")]
            for s in subs[:3]:
                c2, t2, _, _ = _get(s)
                if c2 == 200:
                    urls += re.findall(r"<loc>([^<]+)</loc>", t2)
            objs = [u for u in urls if re.search(r"/(o|objet|object|bien)/", u)]
            if objs:
                return list(dict.fromkeys(objs))
        # sitemap vide ou absent -> on tente le fallback HTML ci-dessous
    # FALLBACK : sites SSR sans sitemap peuple (ex: rfsa) -> liens objet depuis
    # la home + pages liste classiques.
    for path in ("", "/a-louer", "/fr/louer", "/a-vendre", "/fr/acheter", "/biens"):
        code, html, _, _ = _get(base + path)
        if code != 200:
            continue
        for href in re.findall(r'href=["\']([^"\']+)["\']', html):
            if re.search(r"/(o|objet|object|bien|fr)/[^/]*[a-z][^/]*-?\d", href) \
               and not href.endswith((".jpg", ".png", ".css", ".js", ".pdf")):
                full = href if href.startswith("http") else base + href
                if urlparse(full).netloc == urlparse(base).netloc:
                    urls.append(full.split("?")[0])
    # garde ce qui ressemble a une page-objet (slug + id final)
    urls = [u for u in dict.fromkeys(urls) if re.search(r"-\d{2,}$|/\d{3,}$", urlparse(u).path)]
    return urls


# 3. SLUG ---------------------------------------------------------------------
def parse_slug(url):
    """Extrait transaction/type/ville/ID du slug Immomig.
    Ex: /fr/o/a-louer-appartement-de-plain-pied-fribourg-24048"""
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    out = {"external_id": None, "transaction": None, "property_type": None, "city": None}
    mid = re.search(r"-(\d+)$", slug)
    if mid:
        out["external_id"] = mid.group(1)
        slug_core = slug[:mid.start()]
    else:
        slug_core = slug
    if re.match(r"a-?louer|louer|location|rent|^l-", slug):
        out["transaction"] = "location"
    elif re.match(r"a-?vendre|vendre|vente|achat|buy|sale|^v-", slug):
        out["transaction"] = "achat"
    for kw, norm in PROP_TYPES.items():
        if kw in slug_core:
            out["property_type"] = norm
            break
    tokens = [t for t in slug_core.split("-") if t]
    if tokens:
        out["city"] = tokens[-1].replace("_", " ").title()
    return out


# 4. ENRICH SSR ---------------------------------------------------------------
def enrich_from_html(html):
    """Best-effort : prix / pieces / surface / titre / image depuis HTML rendu serveur."""
    d = {"price": None, "rooms": None, "surface": None, "title": None, "image": None}
    mt = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if mt:
        d["title"] = mt.group(1).strip()
    mi = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if mi:
        d["image"] = mi.group(1).strip()
    mp = re.search(r"CHF[\s ]*([\d'’\.]+)", html)
    if mp:
        try:
            d["price"] = int(re.sub(r"[^\d]", "", mp.group(1)))
        except ValueError:
            pass
    mr = re.search(r"(\d+[.,]?\d*)\s*pi[eè]ces", html, re.I)
    if mr:
        d["rooms"] = float(mr.group(1).replace(",", "."))
    ms = re.search(r"(\d{2,4})\s*m[²²½2]", html)
    if ms:
        d["surface"] = int(ms.group(1))
    return d


# ORCHESTRATION ---------------------------------------------------------------
def scrape_immomig_site(base, max_objects=None, enrich=True):
    base = base.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    code, home, headers, final = _get(base)
    if code != 200:
        return {"ok": False, "reason": f"home HTTP {code}", "client_id": None, "biens": []}
    base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
    client_id = detect_immomig(home, headers)
    if not client_id:
        return {"ok": False, "reason": "pas Immomig", "client_id": None, "biens": []}

    obj_urls = discover_objects(base)
    if max_objects:
        obj_urls = obj_urls[:max_objects]

    biens, need_api = [], 0
    for u in obj_urls:
        slug = parse_slug(u)
        rec = {
            "external_id": slug["external_id"] and f"immomig-{client_id}-{slug['external_id']}",
            "source": "agence",
            "source_url": u,
            "transaction": slug["transaction"],
            "property_type": slug["property_type"],
            "city": slug["city"],
            "price": None, "rooms": None, "surface": None,
            "title": None, "images": [],
        }
        if enrich:
            c, h, _, _ = _get(u)
            if c == 200:
                e = enrich_from_html(h)
                rec.update({k: e[k] for k in ("price", "rooms", "surface")})
                rec["title"] = e["title"]
                rec["images"] = [e["image"]] if e["image"] else []
        if rec["price"] is None:
            need_api += 1
        biens.append(rec)

    return {"ok": True, "client_id": client_id, "base": base,
            "n_catalogue": len(obj_urls), "n_scrapes": len(biens),
            "n_sans_prix_statique": need_api, "biens": biens}


if __name__ == "__main__":
    import sys
    sites = sys.argv[1:] or [
        "rfsa.ch", "bulliard.ch", "vesa.ch", "gerances-giroud.ch",
        "muller-immobilier.ch", "valimmobilier.ch",
    ]
    for s in sites:
        # enrich limite pour le test (perf) : 8 objets/site
        r = scrape_immomig_site(s, max_objects=8, enrich=True)
        print(f"\n===== {s} =====")
        if not r["ok"]:
            print(f"  SKIP: {r['reason']}")
            continue
        print(f"  Immomig client #{r['client_id']} | catalogue total: {r['n_catalogue']} biens "
              f"| testes: {r['n_scrapes']} | sans prix en statique: {r['n_sans_prix_statique']}/{r['n_scrapes']}")
        for b in r["biens"][:5]:
            print(f"    [{b['transaction'] or '?':<9}] {str(b['property_type'] or '?'):<11} "
                  f"{str(b['city'] or '?'):<14} {('CHF '+str(b['price'])) if b['price'] else 'prix:JS':<11} "
                  f"{(str(b['rooms'])+'p') if b['rooms'] else '':<5} {(str(b['surface'])+'m2') if b['surface'] else ''}")
