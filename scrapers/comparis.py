"""
Scraper for Comparis.ch — Swiss comparison & real estate portal
Comparis has a REST API for property searches.
"""
import json
import re
import urllib.parse
from .base import BaseScraper


class ComparisScraper(BaseScraper):
    name = "comparis"
    base_url = "https://www.comparis.ch"

    # Comparis uses numeric canton IDs
    CANTON_IDS = {
        "VD": 22, "GE": 25, "ZH": 1, "BE": 2, "VS": 23,
        "FR": 10, "NE": 24, "JU": 26, "TI": 21, "LU": 3,
    }

    def search(self, profile):
        transaction = self.TRANSACTION_MAP.get(
            (profile.get("transaction_type") or "").lower(), "rent"
        )
        deal_type = "1" if transaction == "buy" else "4"  # 1=buy, 4=rent

        canton_code = self._get_canton_code(profile.get("canton"))
        city = profile.get("city") or ""

        # Build Comparis search URL
        params = {
            "requestobject": json.dumps({
                "DealType": int(deal_type),
                "SiteId": 0,
                "RootPropertyTypes": [1] if self.TYPE_MAP.get((profile.get("property_type") or "").lower()) == "apartment" else [2],
                "PropertyTypes": [],
                "RoomsFrom": self._parse_rooms_param(profile.get("rooms")),
                "RoomsTo": None,
                "FloorSearchType": 0,
                "LivingSpaceFrom": None,
                "LivingSpaceTo": None,
                "PriceFrom": self._build_budget_params(profile.get("budget"), profile.get("transaction_type")).get("price_min"),
                "PriceTo": self._build_budget_params(profile.get("budget"), profile.get("transaction_type")).get("price_max"),
                "CantonId": self.CANTON_IDS.get(canton_code, 0),
                "LocationSearchString": city if city.lower() != "votre ville" else "",
                "Sort": 6,  # Newest first
                "WithImagesOnly": None,
                "WithPointsOnly": None,
                "Radius": None,
                "Page": 1,
            })
        }

        # Comparis uses a search results page
        search_type = "kaufen" if transaction == "buy" else "mieten"
        location_slug = city.lower().replace(" ", "-") if city and city.lower() != "votre ville" else ""

        if location_slug:
            url = f"{self.base_url}/immobilien/result/list?{urllib.parse.urlencode(params)}"
        else:
            url = f"{self.base_url}/immobilien/result/list?{urllib.parse.urlencode(params)}"

        html = self._fetch(url)
        if not html:
            return []

        return self._parse_results(html, transaction)

    def _parse_results(self, html, transaction):
        results = []

        # Try to find embedded JSON data (Comparis uses React/Next.js)
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_data:
            try:
                ndata = json.loads(next_data.group(1))
                props = ndata.get("props", {}).get("pageProps", {})
                listings = props.get("results", props.get("listings", []))
                if not listings:
                    for key, val in props.items():
                        if isinstance(val, dict):
                            for k2, v2 in val.items():
                                if isinstance(v2, list) and len(v2) > 0 and isinstance(v2[0], dict):
                                    listings = v2
                                    break
                            if listings:
                                break

                for listing in (listings if isinstance(listings, list) else []):
                    prop = self._convert(listing, transaction)
                    if prop:
                        results.append(prop)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Try JSON-LD as fallback
        json_ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        for block in json_ld:
            try:
                data = json.loads(block)
                if isinstance(data, dict) and "itemListElement" in data:
                    for item in data["itemListElement"]:
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

        price_unit = "CHF/mois" if transaction == "rent" else "CHF"
        lid = listing.get("id", listing.get("propertyId", ""))

        images = []
        for pic in listing.get("images", listing.get("pictures", []))[:8]:
            if isinstance(pic, dict):
                images.append(pic.get("url", pic.get("src", "")))
            elif isinstance(pic, str):
                images.append(pic)

        return {
            "source": self.name,
            "source_id": str(lid),
            "source_url": listing.get("url", f"{self.base_url}/immobilien/detail/{lid}"),
            "title": listing.get("title", ""),
            "price": listing.get("price", listing.get("priceFormatted")),
            "price_unit": price_unit,
            "rooms": listing.get("rooms", listing.get("numberOfRooms")),
            "surface_m2": listing.get("livingSpace", listing.get("surface")),
            "address": listing.get("address", ""),
            "city": listing.get("city", listing.get("locality", "")),
            "canton": listing.get("canton", ""),
            "description": (listing.get("description", listing.get("text", "")))[:500],
            "images": images,
        }

    def _from_jsonld(self, item, transaction):
        if not isinstance(item, dict):
            return None
        price_unit = "CHF/mois" if transaction == "rent" else "CHF"
        return {
            "source": self.name,
            "source_id": item.get("@id", ""),
            "source_url": item.get("url", ""),
            "title": item.get("name", ""),
            "price": self._parse_price(str(item.get("offers", {}).get("price", ""))),
            "price_unit": price_unit,
            "rooms": item.get("numberOfRooms"),
            "surface_m2": None,
            "address": "",
            "city": "",
            "canton": "",
            "description": (item.get("description", ""))[:500],
            "images": [],
        }
