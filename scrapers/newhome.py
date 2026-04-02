"""
Scraper for Newhome.ch — Swiss real estate portal for new constructions & existing properties
"""
import json
import re
import urllib.parse
from .base import BaseScraper


class NewhomeScraper(BaseScraper):
    name = "newhome"
    base_url = "https://www.newhome.ch"

    CANTON_MAP = {
        "VD": "vaud",
        "GE": "geneve",
        "ZH": "zuerich",
        "BE": "bern",
        "VS": "valais",
    }

    def search(self, profile):
        transaction = self.TRANSACTION_MAP.get(
            (profile.get("transaction_type") or "").lower(), "rent"
        )
        offer = "kaufen" if transaction == "buy" else "mieten"

        canton_code = self._get_canton_code(profile.get("canton"))
        canton_slug = self.CANTON_MAP.get(canton_code, "")
        city = (profile.get("city") or "").lower().replace(" ", "-")

        if city and city != "votre ville":
            search_path = f"/fr/{offer}/{city}"
        elif canton_slug:
            search_path = f"/fr/{offer}/kanton-{canton_slug}"
        else:
            search_path = f"/fr/{offer}"

        params = {}
        budget = self._build_budget_params(profile.get("budget"), profile.get("transaction_type"))
        if budget.get("price_min"):
            params["pf"] = str(budget["price_min"])
        if budget.get("price_max"):
            params["pt"] = str(budget["price_max"])

        rooms = self._parse_rooms_param(profile.get("rooms"))
        if rooms:
            params["rf"] = str(int(rooms))

        url = f"{self.base_url}{search_path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        html = self._fetch(url)
        if not html:
            return []

        return self._parse_results(html, transaction)

    def _parse_results(self, html, transaction):
        results = []

        # Try JSON-LD
        json_ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        for block in json_ld:
            try:
                data = json.loads(block)
                items = []
                if isinstance(data, dict) and "itemListElement" in data:
                    items = data["itemListElement"]
                elif isinstance(data, list):
                    items = data

                for item in items:
                    entry = item.get("item", item) if isinstance(item, dict) else item
                    if isinstance(entry, dict):
                        prop = self._from_jsonld(entry, transaction)
                        if prop:
                            results.append(prop)
            except json.JSONDecodeError:
                continue

        # Try __NEXT_DATA__ or nuxt data
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not next_data:
            next_data = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>', html, re.DOTALL)

        if next_data:
            try:
                ndata = json.loads(next_data.group(1))
                listings = self._find_listings(ndata)
                for listing in listings:
                    prop = self._convert(listing, transaction)
                    if prop:
                        results.append(prop)
            except (json.JSONDecodeError, TypeError):
                pass

        return results[:20]

    def _find_listings(self, obj, depth=0):
        if depth > 5:
            return []
        if isinstance(obj, list) and len(obj) > 0:
            if isinstance(obj[0], dict) and any(k in obj[0] for k in ["id", "price", "title", "rooms"]):
                return obj
        if isinstance(obj, dict):
            for key in ["listings", "items", "results", "properties", "data"]:
                if key in obj:
                    result = self._find_listings(obj[key], depth + 1)
                    if result:
                        return result
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    result = self._find_listings(val, depth + 1)
                    if result:
                        return result
        return []

    def _convert(self, listing, transaction):
        if not isinstance(listing, dict):
            return None
        lid = listing.get("id", "")
        price_unit = "CHF/mois" if transaction == "rent" else "CHF"
        images = []
        for pic in listing.get("images", listing.get("pictures", []))[:8]:
            url = pic.get("url", pic) if isinstance(pic, dict) else pic
            if isinstance(url, str):
                images.append(url)
        return {
            "source": self.name,
            "source_id": str(lid),
            "source_url": listing.get("url", f"{self.base_url}/fr/d/{lid}"),
            "title": listing.get("title", ""),
            "price": listing.get("price"),
            "price_unit": price_unit,
            "rooms": listing.get("rooms", listing.get("numberOfRooms")),
            "surface_m2": listing.get("surface", listing.get("livingSpace")),
            "address": listing.get("address", ""),
            "city": listing.get("city", listing.get("locality", "")),
            "canton": listing.get("canton", ""),
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
            "address": addr.get("streetAddress", "") if isinstance(addr, dict) else "",
            "city": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
            "canton": addr.get("addressRegion", "") if isinstance(addr, dict) else "",
            "description": (item.get("description", ""))[:500],
            "images": [],
        }
