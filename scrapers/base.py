"""
Base scraper class for Swiss real estate portals.
All scrapers use only Python stdlib (urllib, html.parser, json, re).
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import re
import ssl
import time
from html.parser import HTMLParser


class TextExtractor(HTMLParser):
    """Simple HTML to text converter"""
    def __init__(self):
        super().__init__()
        self.text = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data.strip())

    def get_text(self):
        return " ".join(t for t in self.text if t)


class BaseScraper:
    """Base class for all Swiss real estate portal scrapers"""

    name = "base"
    base_url = ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CH,fr;q=0.9,de;q=0.8,en;q=0.7",
    }

    # Canton code mapping for Swiss portals
    CANTON_CODES = {
        "vaud": "VD",
        "genève": "GE",
        "zurich": "ZH",
        "berne": "BE",
        "valais": "VS",
        "fribourg": "FR",
        "neuchâtel": "NE",
        "jura": "JU",
        "tessin": "TI",
        "lucerne": "LU",
        "bâle-ville": "BS",
        "bâle-campagne": "BL",
        "argovie": "AG",
        "soleure": "SO",
        "thurgovie": "TG",
        "saint-gall": "SG",
        "grisons": "GR",
        "schaffhouse": "SH",
        "appenzell rhodes-extérieures": "AR",
        "appenzell rhodes-intérieures": "AI",
        "uri": "UR",
        "schwyz": "SZ",
        "obwald": "OW",
        "nidwald": "NW",
        "glaris": "GL",
        "zoug": "ZG",
    }

    TRANSACTION_MAP = {
        "achat": "buy",
        "location": "rent",
    }

    TYPE_MAP = {
        "appartement": "apartment",
        "maison": "house",
        "villa": "house",
        "studio": "apartment",
    }

    def __init__(self):
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def search(self, profile):
        """
        Search for properties matching the profile.
        Must be implemented by each portal scraper.

        Args:
            profile: dict with canton, city, property_type, transaction_type, budget, rooms, priorities

        Returns:
            list of property dicts with: source, source_id, source_url, title, price,
            price_unit, rooms, surface_m2, address, city, canton, description, images
        """
        raise NotImplementedError

    def _fetch(self, url, as_json=False):
        """Fetch a URL with retry logic"""
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, context=self._ctx, timeout=15) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                    if as_json:
                        return json.loads(data)
                    return data
            except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
                if attempt < 2:
                    time.sleep(1 + attempt)
                else:
                    print(f"[{self.name}] Error fetching {url}: {e}")
                    return None

    def _extract_text(self, html):
        """Extract text from HTML"""
        parser = TextExtractor()
        parser.feed(html)
        return parser.get_text()

    def _parse_price(self, text):
        """Extract price from text like 'CHF 1'200'000' or '2500 CHF/mois'"""
        if not text:
            return None
        text = text.replace("'", "").replace("'", "").replace(",", "").replace(".-", "")
        nums = re.findall(r'[\d]+', text)
        if nums:
            # Take the largest number (most likely the price)
            prices = [int(n) for n in nums if int(n) > 10]
            if prices:
                return max(prices)
        return None

    def _get_canton_code(self, canton):
        """Convert canton name to code"""
        if not canton:
            return None
        c = canton.lower().strip()
        if c in self.CANTON_CODES:
            return self.CANTON_CODES[c]
        # Already a code?
        if len(c) == 2:
            return c.upper()
        return None

    def _build_budget_params(self, budget_str, transaction_type):
        """Parse budget string to min/max values"""
        if not budget_str:
            return {}
        b = budget_str.lower().replace(" ", "").replace("'", "")

        # Purchase
        if "500k-1m" in b:
            return {"price_min": 500000, "price_max": 1000000}
        if "<500k" in b or "< 500k" in b:
            return {"price_min": 0, "price_max": 500000}
        if "1m-2m" in b:
            return {"price_min": 1000000, "price_max": 2000000}
        if "2m+" in b:
            return {"price_min": 2000000}

        # Rental
        if "<1500" in b:
            return {"price_min": 0, "price_max": 1500}
        if "1500-2500" in b:
            return {"price_min": 1500, "price_max": 2500}
        if "2500-4000" in b:
            return {"price_min": 2500, "price_max": 4000}
        if "4000+" in b:
            return {"price_min": 4000}

        return {}

    def _parse_rooms_param(self, rooms_str):
        """Parse rooms string to number for API"""
        if not rooms_str:
            return None
        nums = re.findall(r'[\d.]+', rooms_str)
        if nums:
            return float(nums[0])
        return None
