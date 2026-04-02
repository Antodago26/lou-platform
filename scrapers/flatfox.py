"""
Scraper for Flatfox.ch — Swiss rental platform with public API
Flatfox has a documented public API at api.flatfox.ch
"""
import json
import urllib.parse
from .base import BaseScraper


class FlatfoxScraper(BaseScraper):
    name = "flatfox"
    base_url = "https://flatfox.ch"
    api_url = "https://flatfox.ch/api/v1/public"

    def search(self, profile):
        transaction = self.TRANSACTION_MAP.get(
            (profile.get("transaction_type") or "").lower(), "rent"
        )

        # Flatfox is primarily a rental platform
        # Still include for buy but expect fewer results

        city = (profile.get("city") or "").strip()
        canton_code = self._get_canton_code(profile.get("canton"))

        params = {
            "ordering": "-insertion",
            "limit": 20,
        }

        # Location search
        if city and city.lower() != "votre ville":
            params["search"] = city
        elif canton_code:
            params["canton"] = canton_code

        # Object type
        prop_type = self.TYPE_MAP.get((profile.get("property_type") or "").lower())
        if prop_type == "apartment":
            params["object_category"] = "APARTMENT"
        elif prop_type == "house":
            params["object_category"] = "HOUSE"

        # Rooms
        rooms = self._parse_rooms_param(profile.get("rooms"))
        if rooms:
            params["min_rooms"] = str(int(rooms))

        # Budget
        budget = self._build_budget_params(profile.get("budget"), profile.get("transaction_type"))
        if budget.get("price_min"):
            params["min_price"] = str(budget["price_min"])
        if budget.get("price_max"):
            params["max_price"] = str(budget["price_max"])

        # Offer type
        if transaction == "buy":
            params["offer_type"] = "SALE"
        else:
            params["offer_type"] = "RENT"

        url = f"{self.api_url}/listings/?{urllib.parse.urlencode(params)}"
        data = self._fetch(url, as_json=True)

        if not data:
            return []

        results = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            results = []

        return [self._convert(item, transaction) for item in results if self._convert(item, transaction)]

    def _convert(self, listing, transaction):
        if not isinstance(listing, dict):
            return None

        price_unit = "CHF/mois" if transaction == "rent" else "CHF"
        lid = listing.get("pk", listing.get("id", ""))

        images = []
        for img in listing.get("images", [])[:8]:
            if isinstance(img, dict):
                images.append(img.get("url", img.get("thumbnail_url", "")))
            elif isinstance(img, str):
                images.append(img)

        address_parts = [
            listing.get("street", ""),
            listing.get("zip", ""),
            listing.get("city", ""),
        ]

        return {
            "source": self.name,
            "source_id": str(lid),
            "source_url": listing.get("url", f"{self.base_url}/en/flat/{lid}"),
            "title": listing.get("title", listing.get("short_title", "")),
            "price": listing.get("price_display", listing.get("rent_gross", listing.get("price_buy"))),
            "price_unit": price_unit,
            "rooms": listing.get("number_of_rooms"),
            "surface_m2": listing.get("surface_living"),
            "address": " ".join(p for p in address_parts if p).strip(),
            "city": listing.get("city", ""),
            "canton": listing.get("canton", ""),
            "description": (listing.get("description", listing.get("public_description", "")))[:500],
            "images": images,
        }
