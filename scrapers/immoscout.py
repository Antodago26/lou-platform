"""
Scraper for ImmoScout24.ch — Major Swiss real estate portal
"""
import json
import re
import urllib.parse
from .base import BaseScraper


class ImmoScoutScraper(BaseScraper):
    name = "immoscout24"
    base_url = "https://www.immoscout24.ch"

    CANTON_SLUGS = {
        "VD": "canton-vaud",
        "GE": "canton-geneve",
        "ZH": "canton-zuerich",
        "BE": "canton-bern",
        "VS": "canton-valais",
    }

    def search(self, profile):
        transaction = self.TRANSACTION_MAP.get(
            (profile.get("transaction_type") or "").lower(), "rent"
        )
        offer_type = "acheter" if transaction == "buy" else "louer"

        canton_code = self._get_canton_code(profile.get("canton"))
        location = self.CANTON_SLUGS.get(canton_code, "")
        city = (profile.get("city") or "").lower().replace(" ", "-")

        # Build URL: /fr/immobilier/{acheter|louer}/lieu-{city} or canton
        if city and city != "votre ville":
            search_path = f"/fr/immobilier/{offer_type}/lieu-{city}"
        elif location:
            search_path = f"/fr/immobilier/{offer_type}/{location}"
        else:
            search_path = f"/fr/immobilier/{offer_type}"

        params = {}
        budget = self._build_budget_params(profile.get("budget"), profile.get("transaction_type"))
        if budget.get("price_min"):
            params["pf"] = str(budget["price_min"])
        if budget.get("price_max"):
            params["pt"] = str(budget["price_max"])

        rooms = self._parse_rooms_param(profile.get("rooms"))
        if rooms:
            params["nrf"] = str(int(rooms))

        prop_type = self.TYPE_MAP.get((profile.get("property_type") or "").lower())
        if prop_type:
            params["t"] = "2" if prop_type == "apartment" else "1"

        url = f"{self.base_url}{search_path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        html = self._fetch(url)
        if not html:
            return []

        return self._parse_results(html, transaction)

    def _parse_results(self, html, transaction):
        results = []

        # Try __NEXT_DATA__
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_data:
            try:
                ndata = json.loads(next_data.group(1))
                props = ndata.get("props", {}).get("pageProps", {})
                listings = props.get("listings", [])
                if not listings:
                    # Try navigating deeper
                    for key, val in props.items():
                        if isinstance(val, dict):
                            listings = val.get("listings", val.get("items", []))
                            if listings:
                                break

                for listing in (listings if isinstance(listings, list) else []):
                    prop = self._convert(listing, transaction)
                    if prop:
                        results.append(prop)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Try JSON-LD
        json_ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        for block in json_ld:
            try:
                data = json.loads(block)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        entry = item.get("item", item)
                        prop = self._from_jsonld(entry, transaction)
                        if prop:
                            results.append(prop)
            except json.JSONDecodeError:
                continue

        return results[:20]

    def _convert(self, listing, transaction):
        if not isinstance(listing, dict):
            return None

        lid = listing.get("id", listing.get("propertyId", ""))
        price_unit = "CHF/mois" if transaction == "rent" else "CHF"

        addr = listing.get("address", {}) or {}
        chars = listing.get("characteristics", listing) or {}

        images = []
        for pic in listing.get("images", listing.get("pictures", []))[:8]:
            if isinstance(pic, dict):
                images.append(pic.get("url", pic.get("src", "")))
            elif isinstance(pic, str):
                images.append(pic)

        return {
            "source": self.name,
            "source_id": str(lid),
            "source_url": f"{self.base_url}/fr/d/{lid}" if lid else "",
            "title": listing.get("title", chars.get("title", "")),
            "price": chars.get("price", listing.get("price")),
            "price_unit": price_unit,
            "rooms": chars.get("numberOfRooms", listing.get("rooms")),
            "surface_m2": chars.get("livingSpace", chars.get("surface", listing.get("surface"))),
            "address": f"{addr.get('street', '')} {addr.get('zip', '')} {addr.get('city', '')}".strip(),
            "city": addr.get("city", addr.get("locality", "")),
            "canton": addr.get("canton", addr.get("region", "")),
            "description": (listing.get("description", ""))[:500],
            "images": images,
        }

    def _from_jsonld(self, item, transaction):
        if not isinstance(item, dict):
            return None
        price_unit = "CHF/mois" if transaction == "rent" else "CHF"
        addr = item.get("address", {})
        return {
            "source": self.name,
            "source_id": item.get("@id", ""),
            "source_url": item.get("url", ""),
            "title": item.get("name", ""),
            "price": self._parse_price(str(item.get("offers", {}).get("price", ""))),
            "price_unit": price_unit,
            "rooms": item.get("numberOfRooms"),
            "surface_m2": None,
            "address": addr.get("streetAddress", "") if isinstance(addr, dict) else str(addr),
            "city": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
            "canton": addr.get("addressRegion", "") if isinstance(addr, dict) else "",
            "description": (item.get("description", ""))[:500],
            "images": [],
        }
