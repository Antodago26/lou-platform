"""
Bon Home — Parser GENERIQUE des sites d'agences sur backend Immomig.

Pourquoi ce module
------------------
Pivot produit (cf. prototypes/immomig/README.md) : on quitte le scraping des
PORTAILS (Homegate/ImmoScout, defendus, ScrapingBee payant, risque CGU) pour
scraper DIRECTEMENT les sites d'agences — non defendus, ~gratuits, et porteurs
de biens « caches » jamais publies sur les portails.

Immomig est le backend dominant en Suisse romande (~43% des agences de
l'echantillon). UN seul parser couvre donc des dizaines d'agences, sans code
specifique a chacune : c'est le modele « 1 parser par backend ».

Comment ca marche
-----------------
La majorite des sites Immomig « website.js » exposent une route AJAX interne
qui rend la liste des objets en HTML cote serveur :

    GET /{lang}/a/o/search/list        -> JSON { list: "<article>...</article>", pagination: {...} }

Un simple GET (avec le cookie de session pose par la home) suffit — PAS besoin
de rendu JS ni de ScrapingBee. Le champ `list` contient le HTML des cartes :
titre, prix, ville, pieces, image, lien objet. La transaction (louer/vendre) et
le type (appartement/maison/...) se lisent dans le slug de l'URL objet.

    /fr/o/a-louer-appartement-fribourg-6090489
        ^transaction ^type       ^ville   ^id

Ce endpoint AJAX est la cle qui debloque les sites 100% SPA (bulliard, muller,
immocrans...) : leurs pages liste sont vides en statique, mais l'endpoint, lui,
rend tout cote serveur. Valide sur bulliard/muller/immocrans/rfsa (juin 2026).

Variante Nuxt (sovalco, vesa) : pas de route website.js, l'etat est un JSON
inline dans la page. Non couverte ici (minorite) -> `scrape_immomig_agency`
renvoie [] proprement, l'agence est simplement marquee sans resultat.

Sortie
------
`scrape_immomig_agency(domain, transaction=None)` renvoie une liste de dicts au
format `scrapers._make_property` (source = domaine de l'agence, ex 'bulliard.ch')
directement ingerable par `save_to_db` — on reutilise tel quel le scoring, la
dedup cross-source, la DB et le monitoring sante existants.
"""
import re
import logging
from html import unescape
from urllib.parse import urlparse, urljoin

import requests

log = logging.getLogger('lou-scrapers')

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Slug type token -> type normalise bonhome (aligne sur _guess_type de scrapers.py)
_PROP_TYPES = {
    "appartement": "appartement", "studio": "studio", "villa": "maison",
    "maison": "maison", "duplex": "appartement", "attique": "appartement",
    "loft": "appartement", "chalet": "maison", "terrain": "terrain",
    "parking": "parking", "place": "parking", "garage": "parking",
    "bureau": "bureau", "local": "commerce", "commerce": "commerce",
    "immeuble": "immeuble", "depot": "commerce", "arcade": "commerce",
}

_LANGS = ("fr", "de", "en", "it")

# Suffixes canton en fin de slug a retirer ('...-romont-fr' -> 'romont').
_CANTON_TOKENS = {
    "fr", "vd", "vs", "ge", "ne", "ju", "be", "ti", "zh", "ag", "so",
    "lu", "sg", "gr", "tg", "sh", "zg", "ow", "nw", "ur", "sz", "gl",
    "ai", "ar", "bl", "bs",
}
# Villes romandes a nom compose (le split par '-' casserait le nom). On teste
# ces suffixes AVANT de tomber sur le dernier token seul.
_MULTIWORD_CITIES = (
    "crans-montana", "la-tour-de-treme", "la-tour-de-treme", "la-chaux-de-fonds",
    "le-mont-sur-lausanne", "villars-sur-glane", "villars-sur-ollon",
    "corcelles-cormondreche", "chene-bougeries", "chene-bourg", "grand-saconnex",
    "le-grand-saconnex", "bourg-en-lavaux", "saint-legier", "la-tour-de-peilz",
    "montreux-territet", "val-de-ruz", "val-de-travers", "marin-epagnier",
    "saint-blaise", "le-landeron", "la-neuveville", "romont-fr", "bulle-fr",
)
_MAX_PAGES = 40          # garde-fou : ~12 objets/page -> 480 objets max/agence
_PAGE_SIZE = 48          # on force une grosse page pour limiter les requetes
_REQ_TIMEOUT = 20


def _new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    })
    return s


def detect_immomig(session, domain):
    """Sonde la home d'une agence. Renvoie dict(base, lang, client_id) si le site
    tourne sur Immomig « website.js », sinon None.

    - base : origine finale apres redirections (https://www.rfsa.ch)
    - lang : 1er segment de langue detecte dans les liens (fr/de/en/it)
    - client_id : id client Immomig, lu dans une URL immomigimg.ch/.../<id>/pictures
    """
    url = domain if domain.startswith("http") else "https://" + domain
    try:
        r = session.get(url, timeout=_REQ_TIMEOUT, allow_redirects=True)
    except Exception as e:
        log.info("[immomig] %s injoignable: %s", domain, type(e).__name__)
        return None
    if r.status_code != 200 or not r.text:
        log.info("[immomig] %s home HTTP %s", domain, r.status_code)
        return None

    html = r.text
    parsed = urlparse(r.url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # ID client Immomig : segment numerique avant /pictures|websites|seo/
    m = re.search(r'immomigimg\.ch/[^"\'\s]*?/(\d+)/(?:pictures|websites|seo)/', html)
    if not m:
        return None  # pas Immomig (ou variante sans images immomigimg -> non couvert)
    client_id = m.group(1)

    # Langue : segment du path final, sinon 1er lien /xx/ present dans la page
    lang = None
    seg = [p for p in parsed.path.split("/") if p]
    if seg and seg[0] in _LANGS:
        lang = seg[0]
    if not lang:
        lm = re.search(r'href=["\']/(%s)/' % "|".join(_LANGS), html)
        lang = lm.group(1) if lm else "fr"

    return {"base": base, "lang": lang, "client_id": client_id}


def _fetch_cards(session, base, lang):
    """Itere l'endpoint AJAX de liste et renvoie la liste des fragments HTML
    <article>. Suit la pagination via le param `page`. Stoppe des qu'une page
    ne rend aucun article ou au garde-fou _MAX_PAGES.

    Renvoie (articles, endpoint_ok) — endpoint_ok=False si la route AJAX
    n'existe pas (site Nuxt / SSR sans website.js)."""
    articles = []
    seen_page1_empty = False
    for page in range(1, _MAX_PAGES + 1):
        ep = f"{base}/{lang}/a/o/search/list?page={page}&page_size={_PAGE_SIZE}"
        try:
            r = session.get(
                ep, timeout=_REQ_TIMEOUT,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Accept": "application/json, text/javascript, */*"},
            )
        except Exception as e:
            log.info("[immomig] %s page %d erreur: %s", base, page, type(e).__name__)
            break
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "json" not in ctype.lower():
            if page == 1:
                return [], False  # endpoint absent -> signale au caller
            break
        try:
            data = r.json()
        except ValueError:
            if page == 1:
                return [], False
            break
        frag = data.get("list") or ""
        page_arts = re.findall(r'<article\b.*?</article>', frag, re.S)
        if not page_arts:
            if page == 1:
                seen_page1_empty = True
            break
        articles.extend(page_arts)
        # Fin de pagination : moins d'objets que demande => derniere page.
        pages = (data.get("pagination") or {}).get("pages")
        if isinstance(pages, int) and page >= pages:
            break
        if len(page_arts) < _PAGE_SIZE and not (data.get("pagination") or {}).get("nexts"):
            break
    return articles, (not seen_page1_empty)


def _parse_slug(url):
    """Extrait transaction / type / ville / id du slug objet Immomig.
    Ex: /fr/o/a-louer-appartement-de-plain-pied-fribourg-6090489"""
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    out = {"external_id": None, "transaction": None, "property_type": None, "city": None}
    mid = re.search(r"-(\d+)$", slug)
    if mid:
        out["external_id"] = mid.group(1)
        core = slug[:mid.start()]
    else:
        core = slug
    if re.match(r"(a-?louer|louer|location|rent|^l-)", core):
        out["transaction"] = "location"
    elif re.match(r"(a-?vendre|vendre|vente|achat|buy|sale|^v-)", core):
        out["transaction"] = "achat"
    for kw, norm in _PROP_TYPES.items():
        if kw in core:
            out["property_type"] = norm
            break
    out["city"] = _city_from_core(core)
    return out


def _city_from_core(core):
    """Devine la ville depuis le corps du slug (sans l'id final).
    Gere les villes composees et retire un suffixe canton ('-fr')."""
    if not core:
        return None
    # 1. ville composee connue en fin de slug ?
    for mw in _MULTIWORD_CITIES:
        if core.endswith(mw):
            city = mw
            if city.endswith("-fr") or city.endswith("-vd"):
                city = city.rsplit("-", 1)[0]  # 'romont-fr' -> 'romont'
            return city.replace("-", " ").replace("_", " ").title()
    tokens = [t for t in core.split("-") if t]
    if not tokens:
        return None
    # 2. dernier token = abreviation canton -> on prend le precedent
    if tokens[-1].lower() in _CANTON_TOKENS and len(tokens) >= 2:
        tokens = tokens[:-1]
    return tokens[-1].replace("_", " ").title()


def _first(pattern, text, group=1, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def _parse_card(article, base):
    """Parse un fragment <article> en dict brut (avant _make_property).
    Renvoie None si pas d'URL objet exploitable."""
    href = _first(r'href=["\']([^"\']*/o/[^"\']+)["\']', article)
    if not href:
        return None
    source_url = urljoin(base + "/", href)
    slug = _parse_slug(source_url)

    obj_id = _first(r'data-object-id=["\'](\d+)["\']', article) or slug["external_id"]
    if not obj_id:
        return None

    # Ville / adresse : valeur de la ligne caract_location. Le bloc peut contenir
    # un <svg> d'icone avant le <div class="value"> (cas rfsa), et la value peut
    # tenir sur 2 lignes "Ville" + "Rue ... N" -> on separe. Beaucoup de
    # templates (muller, immocrans) n'ont PAS caract_location -> fallback slug.
    loc_raw = _first(r'caract_location.*?class="value"[^>]*>(.*?)</div>', article, 1, re.S)
    city = None
    address = ""
    if loc_raw:
        # coupe sur <br> ou retours ligne -> [ville, rue...]
        parts = [unescape(re.sub(r'<[^>]+>', ' ', seg)).strip()
                 for seg in re.split(r'<br\s*/?>|\n', loc_raw)]
        parts = [p for p in parts if p]
        if parts:
            city = parts[0] or None
            address = " ".join(parts[1:]).strip()
    if not city:
        city = (slug["city"] or "").strip() or None

    # Prix : valeur de la ligne caract_price (icone possible avant la value),
    # sinon 1er "CHF ..." de la carte. On ne garde que les chiffres.
    price_raw = _first(r'caract_price.*?class="value"[^>]*>(.*?)</div>', article, 1, re.S)
    price_block = price_raw if price_raw else article
    price = None
    pm = re.search(r"CHF[\s ]*([\d'’\.\s]+)", price_block)
    if pm:
        digits = re.sub(r"[^\d]", "", pm.group(1))
        if digits:
            price = int(digits)

    # Titre : og-like — 1er element .title de la carte
    title = _first(r'class="title"[^>]*>\s*([^<]+?)\s*<', article) or ""
    title = unescape(title).strip()

    # Pieces : depuis le titre ("4.5 pieces") ou n'importe ou dans la carte
    rooms = None
    rm = re.search(r"(\d+(?:[.,]\d+)?)\s*pi[eè]ces?", article, re.I)
    if rm:
        try:
            rooms = float(rm.group(1).replace(",", "."))
        except ValueError:
            pass

    # Surface : rarement dans la carte liste, best-effort
    surface = None
    sm = re.search(r"(\d{2,4})\s*m[²2]", article)
    if sm:
        surface = int(sm.group(1))

    # Image : 1re immomigimg de la carte (on prend la variante srcset la + grande)
    image = _first(r'(https://www\.immomigimg\.ch/[^"\'\s]+\.jpg)', article)

    return {
        "obj_id": str(obj_id),
        "source_url": source_url,
        "transaction": slug["transaction"],
        "property_type": slug["property_type"],
        "city": city,
        "address": address,
        "price": price,
        "rooms": rooms,
        "surface": surface,
        "title": title,
        "image": image,
    }


def scrape_immomig_agency(domain, transaction=None, max_objects=None):
    """Scrape une agence Immomig et renvoie une liste de dicts _make_property.

    domain      : ex 'bulliard.ch' (source des biens = ce domaine).
    transaction : 'location' | 'achat' | None (None = les deux).
    max_objects : coupe la liste (tests/perf).

    Import PARESSEUX de scrapers.* pour eviter l'import circulaire
    (scrapers.py importe ce module dans scrape_all).
    """
    from scrapers import _make_property, CITY_CANTONS, _normalize_city

    session = _new_session()
    info = detect_immomig(session, domain)
    if not info:
        log.info("[immomig] %s : pas un site Immomig website.js — skip", domain)
        return []

    base, lang, client_id = info["base"], info["lang"], info["client_id"]
    source = urlparse(base).netloc.replace("www.", "")  # ex 'bulliard.ch'

    articles, endpoint_ok = _fetch_cards(session, base, lang)
    if not endpoint_ok:
        log.info("[immomig] %s : endpoint liste AJAX absent (variante Nuxt/SSR) — 0 bien", domain)
        return []
    log.info("[immomig] %s (client #%s, /%s) : %d cartes brutes",
             domain, client_id, lang, len(articles))

    results = []
    seen = set()
    for art in articles:
        card = _parse_card(art, base)
        if not card:
            continue
        if card["obj_id"] in seen:
            continue
        seen.add(card["obj_id"])
        if transaction and card["transaction"] and card["transaction"] != transaction:
            continue
        canton = CITY_CANTONS.get(_normalize_city(card["city"] or ""), "")
        prop = _make_property(
            external_id=f"immomig-{client_id}-{card['obj_id']}",
            source=source,
            source_url=card["source_url"],
            title=card["title"],
            description="",
            property_type=card["property_type"],
            transaction=card["transaction"] or transaction or "location",
            price=card["price"],
            rooms=card["rooms"],
            surface=card["surface"],
            floor=None,
            address=card.get("address", ""),
            city=card["city"],
            canton=canton,
            postal_code=None,
            latitude=None,
            longitude=None,
            features=[],
            images=[card["image"]] if card["image"] else [],
            published_at=None,
        )
        if prop is not None:
            results.append(prop)
        if max_objects and len(results) >= max_objects:
            break

    log.info("[immomig] %s : %d biens exploitables", domain, len(results))
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    sites = sys.argv[1:] or ["rfsa.ch", "bulliard.ch", "muller-immobilier.ch", "immocrans.ch"]
    for s in sites:
        biens = scrape_immomig_agency(s, max_objects=6)
        print(f"\n===== {s} : {len(biens)} biens =====")
        for b in biens[:6]:
            print(f"  [{b['transaction']:<9}] {str(b['property_type']):<11} "
                  f"{str(b['city']):<16} {('CHF '+str(b['price'])) if b['price'] else 'prix:?':<13} "
                  f"{(str(b['rooms'])+'p') if b['rooms'] else '':<6} "
                  f"{(str(b['surface'])+'m2') if b['surface'] else '':<7} {b['source_url']}")
