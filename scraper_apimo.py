"""
Bon Home — Parser GENERIQUE des sites d'agences sur le template Apimo HEBERGE
(« Design by Apimo », editeur apimo.net).

Pourquoi ce module
------------------
Deuxieme backend du pivot « scraping direct des sites d'agences » (apres
Immomig, cf. scraper_immomig.py). Apimo est minoritaire en Suisse romande, MAIS
les agences qui l'utilisent via le site hebergé Apimo partagent TOUTES le meme
gabarit HTML — donc 1 parser couvre N agences (modele « 1 parser par backend »).

Confirme juin 2026 sur : reference5.ch (NE/Montreux), lerezo.ch (VS),
agence-immobiliere-immoglobe.ch (NE).

/!\ Ne couvre PAS les integrations Apimo « sur-mesure » type plugin WordPress
(ex omnia.ch = plugin omnia-apimo + theme Elementor) : gabarit propre a l'agence,
a traiter en scraper bespoke separe. Ce module vise le seul template HEBERGE.

Comment ca marche
-----------------
Le template hebergé Apimo est du SSR classique :
  - page liste : /fr/acheter (+ /fr/louer) OU /fr/nos-biens (catalogue mixte),
    paginee via ?page=N ;
  - chaque carte a des classes STABLES : .property (bloc), .price, .rooms,
    .area, .city, .subtype ;
  - le lien objet porte tout le contexte dans le slug, separe par '+' :
        /fr/propriete/vente+appartement+cortaillod+<descr>+87120162
        ^transaction  ^type       ^ville                ^id numerique
La transaction se lit dans le slug -> un catalogue mixte (nos-biens) marche
aussi bien qu'une liste dediee.

Sortie
------
`scrape_apimo_agency(domain, transaction=None)` renvoie une liste de dicts au
format `scrapers._make_property` (source = domaine de l'agence), directement
ingerable par `save_to_db`.
"""
import re
import logging
from html import unescape
from urllib.parse import urlparse, urljoin

import requests

log = logging.getLogger('lou-scrapers')

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Token de type dans le slug -> type normalise bonhome (aligne sur _guess_type).
_PROP_TYPES = {
    "appartement": "appartement", "studio": "studio", "villa": "maison",
    "maison": "maison", "duplex": "appartement", "attique": "appartement",
    "loft": "appartement", "chalet": "maison", "terrain": "terrain",
    "parking": "parking", "place": "parking", "garage": "parking",
    "bureau": "bureau", "local": "commerce", "commerce": "commerce",
    "immeuble": "immeuble", "depot": "commerce", "arcade": "commerce",
}

# Chemins de page liste a tenter (le 1er qui rend des cartes gagne). Les sites
# hebergés Apimo utilisent soit acheter+louer separes, soit un nos-biens mixte.
_LISTING_PATHS = (
    "/fr/acheter", "/fr/louer", "/fr/nos-biens", "/fr/a-vendre",
    "/fr/vente", "/fr/location", "/fr/nos-biens-a-vendre",
    "/fr/ventes", "/fr/locations", "/fr/locations-saisonniere",
)
_MAX_PAGES = 40          # garde-fou pagination
_REQ_TIMEOUT = 20


def _new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    })
    return s


def detect_apimo(session, domain):
    """Sonde la home. Renvoie {'base': origine finale} si le site tourne sur le
    template hebergé Apimo (empreinte apimo.net / media.apimo.pro), sinon None."""
    url = domain if domain.startswith("http") else "https://" + domain
    try:
        r = session.get(url, timeout=_REQ_TIMEOUT, allow_redirects=True)
    except Exception as e:
        log.info("[apimo] %s injoignable: %s", domain, type(e).__name__)
        return None
    if r.status_code != 200 or not r.text:
        log.info("[apimo] %s home HTTP %s", domain, r.status_code)
        return None
    blob = r.text.lower()
    if "apimo.net" not in blob and "media.apimo.pro" not in blob and "design by apimo" not in blob:
        return None
    parsed = urlparse(r.url)
    return {"base": f"{parsed.scheme}://{parsed.netloc}"}


def _card_field(cls, card):
    """Texte d'un element .<cls> de la carte, balises internes (icones) retirees."""
    m = re.search(r'class="[^"]*\b' + cls + r'\b[^"]*"[^>]*>(.*?)</(?:div|span|p|li|h\d|a)>',
                  card, re.S)
    if not m:
        return None
    return unescape(re.sub(r'<[^>]+>', ' ', m.group(1))).strip() or None


def _split_cards(html):
    """Decoupe la page liste en cartes .property. On borne chaque carte au debut
    de la suivante ; on ne garde que celles qui portent un prix (.price)."""
    parts = re.split(r'(?=<[^>]+class="[^"]*\bproperty\b[^"]*")', html)
    return [p for p in parts if re.search(r'class="[^"]*\bprice\b', p)]


def _parse_price(text):
    """'2 240 000 CHF' -> 2240000 ; 'Prix sur demande' -> None."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_number(text):
    """'6.5' / '2.5 pieces' -> 6.5 ; None si absent."""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_slug(link):
    """Extrait transaction / type / ville / id du slug Apimo hebergé.
    /fr/propriete/vente+appartement+cortaillod+<descr>+87120162"""
    out = {"external_id": None, "transaction": None, "property_type": None, "city": None}
    seg = link.split("/fr/propriete/")
    slug = seg[1] if len(seg) > 1 else link.rstrip("/").split("/")[-1]
    toks = [t for t in slug.split("+") if t]
    if not toks:
        return out
    mid = re.search(r"(\d{5,})", toks[-1])
    if mid:
        out["external_id"] = mid.group(1)
    t0 = toks[0].lower()
    if t0.startswith("vente") or t0.startswith("achat"):
        out["transaction"] = "achat"
    elif t0.startswith("location") or t0.startswith("louer"):
        out["transaction"] = "location"
    if len(toks) > 1:
        out["property_type"] = _PROP_TYPES.get(toks[1].lower())
    if len(toks) > 2:
        out["city"] = toks[2].replace("-", " ").title()
    return out


def _fetch_listing(session, base, path):
    """Pagine une page liste (?page=N) et renvoie la liste des fragments carte.
    Stoppe quand une page ne rend aucune carte ou n'apporte aucun id nouveau."""
    cards, seen_links = [], set()
    for page in range(1, _MAX_PAGES + 1):
        url = f"{base}{path}" + (f"?page={page}" if page > 1 else "")
        try:
            r = session.get(url, timeout=_REQ_TIMEOUT)
        except Exception as e:
            log.info("[apimo] %s%s page %d erreur: %s", base, path, page, type(e).__name__)
            break
        if r.status_code != 200:
            break
        page_cards = _split_cards(r.text)
        if not page_cards:
            break
        new = 0
        for c in page_cards:
            link = (re.search(r'href="(/fr/propriete/[^"]+)"', c) or [None, None])[1]
            if link and link not in seen_links:
                seen_links.add(link)
                cards.append(c)
                new += 1
        if new == 0:  # page renvoyee mais aucun bien nouveau -> fin
            break
    return cards


def scrape_apimo_agency(domain, transaction=None, max_objects=None):
    """Scrape une agence sur template hebergé Apimo -> liste de dicts _make_property.

    domain      : ex 'reference5.ch' (source des biens = ce domaine).
    transaction : 'location' | 'achat' | None (None = les deux).
    max_objects : coupe la liste (tests/perf).

    Import PARESSEUX de scrapers.* pour eviter l'import circulaire.
    """
    from scrapers import _make_property, CITY_CANTONS, _normalize_city

    session = _new_session()
    info = detect_apimo(session, domain)
    if not info:
        log.info("[apimo] %s : pas un template hebergé Apimo — skip", domain)
        return []
    base = info["base"]
    source = urlparse(base).netloc.replace("www.", "")

    # Recolte les cartes de toutes les pages liste disponibles (dedup par lien).
    all_cards, seen_paths_links = [], set()
    for path in _LISTING_PATHS:
        for c in _fetch_listing(session, base, path):
            link = (re.search(r'href="(/fr/propriete/[^"]+)"', c) or [None, None])[1]
            if link and link not in seen_paths_links:
                seen_paths_links.add(link)
                all_cards.append(c)
    if not all_cards:
        log.info("[apimo] %s : aucune carte trouvee sur les pages liste", domain)
        return []
    log.info("[apimo] %s : %d cartes brutes", domain, len(all_cards))

    results, seen_ids = [], set()
    for c in all_cards:
        link = (re.search(r'href="(/fr/propriete/[^"]+)"', c) or [None, None])[1]
        if not link:
            continue
        slug = _parse_slug(link)
        ext = slug["external_id"]
        if not ext or ext in seen_ids:
            continue
        seen_ids.add(ext)
        if transaction and slug["transaction"] and slug["transaction"] != transaction:
            continue

        city = _card_field("city", c) or slug["city"]
        price = _parse_price(_card_field("price", c))
        rooms = _parse_number(_card_field("rooms", c))
        surface = _parse_number(_card_field("area", c))
        surface = int(surface) if surface else None
        subtype = _card_field("subtype", c)
        img = (re.search(r'(?:data-src|data-original|src)="(https://media\.apimo\.pro/[^"]+)"', c)
               or re.search(r'url\((https://media\.apimo\.pro/[^)]+)\)', c)
               or [None, None])[1]
        title = _card_field("title", c) or subtype or ""
        canton = CITY_CANTONS.get(_normalize_city(city or ""), "")

        prop = _make_property(
            external_id=f"apimo-{ext}",
            source=source,
            source_url=urljoin(base + "/", link),
            title=title,
            description="",
            property_type=slug["property_type"] or (subtype and _PROP_TYPES.get(subtype.split()[0].lower())),
            transaction=slug["transaction"] or transaction or "achat",
            price=price,
            rooms=rooms,
            surface=surface,
            floor=None,
            address="",
            city=city,
            canton=canton,
            postal_code=None,
            latitude=None,
            longitude=None,
            features=[subtype] if subtype else [],
            images=[img] if img else [],
            published_at=None,
        )
        if prop is not None:
            results.append(prop)
        if max_objects and len(results) >= max_objects:
            break

    log.info("[apimo] %s : %d biens exploitables", domain, len(results))
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    sites = sys.argv[1:] or ["reference5.ch", "lerezo.ch", "agence-immobiliere-immoglobe.ch"]
    for s in sites:
        biens = scrape_apimo_agency(s, max_objects=8)
        print(f"\n===== {s} : {len(biens)} biens =====")
        for b in biens[:8]:
            print(f"  [{b['transaction']:<7}] {str(b['property_type']):<11} "
                  f"{str(b['city']):<16} {('CHF '+str(b['price'])) if b['price'] else 'prix:?':<13} "
                  f"{(str(b['rooms'])+'p') if b['rooms'] else '':<6} "
                  f"{(str(b['surface'])+'m2') if b['surface'] else '':<7} {b['source_url'][:70]}")
