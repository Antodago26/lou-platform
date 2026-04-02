"""
Scraper for Homegate.ch — Switzerland's largest real estate portal
Uses their search API which returns JSON results.
"""
import json
import re
import urllib.parse
from .base import BaseScraper


class HomegateScraper(BaseScraper):
    name = "homegate"
    base_url = "https://www.homegate.ch"

    # Homegate uses location IDs, these are the main cities
    CITY_LOCATION_IDS = {
        "lausanne": "geo-city-lausanne",
        "montreux": "geo-city-montreux",
        "nyon": "geo-city-nyon",
        "morges": "geo-city-morges",
        "genève": "geo-city-geneve",
        "carouge": "geo-city-carouge-ge",
        "meyrin": "geo-city-meyrin",
        "vernier": "geo-city-vernier",
        "zurich": "geo-city-zuerich",
        "winterthur": "geo-city-winterthur",
        "berne": "geo-city-bern",
        "thoune": "geo-city-thun",
        "interlaken": "geo-city-interlaken",
        "sion": "geo-city-sion",
        "martigny": "geo-city-martigny",
        "sierre": "geo-city-sierre",
        "verbier": "geo-city-verbier",
    }

    CANTON_LOCATION_IDS = {
        "VD": "geo-canton-vaud",
        "GE": "geo-canton-geneve",
        "ZH": "geo-canton-zuerich",
        "BE": "geo-canton-bern",
        "VS": "geo-canton-valais",
    }

    def search(self, profile):
        """Search Homegate for matching properties"""
        transaction = self.TRANSACTION_MAP.get(
            (profile.get("transaction_type") or "").lower(), "rent"
        )
        offer_type = "buy" if transaction == "buy" else "rent"

        # Build location
        city = (profile.get("city") or "").lower()
        canton_code = self._get_canton_code(profile.get("canton"))
        location = self.CITY_LOCATION_IDS.get(city)
        if not location and canton_code:
            location = self.CANTON_LOCATION_IDS.get(canton_code)
        if not location:
            location = "geo-country-ch"

        # Build search URL
        # Homegate search page: /fr/{rent|buy}/real-estate/{location}
        search_path = f"/fr/{offer_type}/real-estate/{location}"
        params = {}

        # Budget
        budget = self._build_budget_params(profile.get("budget"), profile.get("transaction_type"))
        if budget.get("price_min"):
            params["ac"] = str(budget["price_min"])
        if budget.get("price_max"):
            params["ad"] = str(budget["price_max"])

        # Rooms
        rooms = self._parse_rooms_param(profile.get("rooms"))
        if rooms:
            params["ah"] = str(int(rooms))

        # Property type
        prop_type = self.TYPE_MAP.get((profile.get("property_type") or "").lower())
        if prop_type == "apartment":
            params["ae"] = "apartment"
        elif prop_type == "house":
            params["ae"] = "house"

        url = f"{self.base_url}{search_path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        html = self._fetch(url)
        if not html:
            return []

        return self._parse_results(html, offer_type)

    def _parse_results(self, html, offer_type):
        """Parse Homegate search results from HTML"""
        results = []

        # Homegate embeds listing data in JSON-LD or in a JS state object
        # Look for structured data
        json_ld_pattern = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL
        )
        for block in json_ld_pattern:
            try:
                data = json.loads(block)
                if isinstance(data, list):
                    for item in data:
                        prop = self._parse_json_ld_item(item, offer_type)
                        if prop:
                            results.append(prop)
                elif isinstance(data, dict):
                    if data.get("@type") == "ItemList":
                        for item in data.get("itemListElement", []):
                            prop = self._parse_json_ld_item(item.get("item", item), offer_type)
                            if prop:
                                results.append(prop)
            except json.JSONDecodeError:
                continue

        # Also try to parse listing cards from HTML
        # Pattern: data-test="result-list-item" or similar
        listing_patterns = re.findall(
            r'href="(/fr/(?:buy|rent)/\d+)"[^>]*>.*?(?:</a>|</article>)',
            html, re.DOTALL
        )

        # Parse __NEXT_DATA__ if available (Next.js app)
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_data:
            try:
                ndata = json.loads(next_data.group(1))
                listings = self._extract_from_next_data(ndata)
                for listing in listings:
                    prop = self._convert_listing(listing, offer_type)
                    if prop:
                        results.append(prop)
            except (json.JSONDecodeError, KeyError):
                pass

        return results[:20]  # Limit to 20 results

    def _parse_json_ld_item(self, item, offer_type):
        """Parse a JSON-LD RealEstateListing item"""
        if not isinstance(item, dict):
            return None

        item_type = item.get("@type", "")
        if "RealEstate" not in item_type and "Residence" not in item_type and "Apartment" not in item_type:
            if "url" not in item:
                return None

        price = None
        price_unit = "CHF"
        offers = item.get("offers", {})
        if isinstance(offers, dict):
            price = self._parse_price(str(offers.get("price", "")))
            if offer_type == "rent":
                price_unit = "CHF/mois"

        images = []
        photo = item.get("photo") or item.get("image")
        if isinstance(photo, list):
            images = [p.get("contentUrl", p) if isinstance(p, dict) else p for p in photo[:8]]
        elif isinstance(photo, (str, dict)):
            url = photo.get("contentUrl", photo) if isinstance(photo, dict) else photo
            images = [url]

        return {
            "source": self.name,
            "source_id": item.get("identifier", item.get("@id", "")),
            "source_url": item.get("url", ""),
            "title": item.get("name", ""),
            "price": price,
            "price_unit": price_unit,
            "rooms": item.get("numberOfRooms"),
            "surface_m2": self._parse_price(str(item.get("floorSize", {}).get("value", ""))),
            "address": self._format_address(item.get("address", {})),
            "city": item.get("address", {}).get("addressLocality", ""),
            "canton": item.get("address", {}).get("addressRegion", ""),
            "description": (item.get("description") or "")[:500],
            "images": images,
        }

    def _extract_from_next_data(self, ndata):
        """Extract listings from Next.js __NEXT_DATA__ object"""
        # Navigate through the Next.js data structure
        props = ndata.get("props", {}).get("pageProps", {})
        listings = props.get("listings", props.get("resultList", {}).get("listings", []))
        if not listings:
            # Try other paths
            for key in props:
                if isinstance(props[key], dict) and "listings" in props[key]:
                    listings = props[key]["listings"]
                    break
        return listings if isinstance(listings, list) else []

    def _convert_listing(self, listing, offer_type):
        """Convert a Homegate listing dict to our standard format"""
        if not isinstance(listing, dict):
            return None

        listing_id = listing.get("id", listing.get("listingId", ""))
        price_unit = "CHF/mois" if offer_type == "rent" else "CHF"

        characteristics = listing.get("characteristics", {})
        address = listing.get("address", {})

        images = []
        for pic in listing.get("pictures", listing.get("images", []))[:8]:
            if isinstance(pic, dict):
                images.append(pic.get("url", pic.get("urls", {}).get("listing", "")))
            elif isinstance(pic, str):
                images.append(pic)

        return {
            "source": self.name,
            "source_id": str(listing_id),
            "source_url": f"{self.base_url}/fr/{offer_type}/{listing_id}",
            "title": listing.get("title", characteristics.get("title", "")),
            "price": characteristics.get("price", listing.get("price")),
            "price_unit": price_unit,
            "rooms": characteristics.get("numberOfRooms", listing.get("rooms")),
            "surface_m2": characteristics.get("livingSpace", listing.get("surface")),
            "address": f"{address.get('street', '')} {address.get('postalCode', '')} {address.get('locality', '')}".strip(),
            "city": address.get("locality", ""),
            "canton": address.get("region", address.get("canton", "")),
            "description": (listing.get("description", ""))[:500],
            "images": images,
        }

    def _format_address(self, address):
        """Format a JSON-LD address object"""
        if isinstance(address, str):
            return address
        if isinstance(address, dict):
            parts = [
                address.get("streetAddress", ""),
                address.get("postalCode", ""),
                address.get("addressLocality", ""),
            ]
            return " ".join(p for p in parts if p).strip()
        return ""
