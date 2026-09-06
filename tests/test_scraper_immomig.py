"""
Tests du parser generique Immomig (scraper_immomig.py) — pivot « scraping direct
des sites d'agences ».

Lancer : cd backend-v2 && python3 -m unittest tests.test_scraper_immomig -v

Couvre, SANS reseau (fixtures HTML reelles capturees juin 2026) :
  - _parse_slug : transaction / type / id depuis l'URL objet
  - _city_from_core : suffixe canton ('-fr') + villes composees (crans-montana)
  - _parse_card : extraction prix / pieces / surface / ville / adresse d'une carte
    (fixtures bulliard = SPA simple, rfsa = icone svg + ville/rue sur 2 lignes)
  - scrape_immomig_agency : orchestration complete avec session/HTTP mockes
    (detection client_id, pagination, format _make_property, filtre transaction)
  - integration save_to_db : les biens agence passent _is_valid_source_url
"""
import os
import re
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper_immomig as si

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class ParseSlugTest(unittest.TestCase):
    def test_location_appartement(self):
        out = si._parse_slug("https://www.bulliard.ch/fr/o/a-louer-appartement-fribourg-6090489")
        self.assertEqual(out["transaction"], "location")
        self.assertEqual(out["property_type"], "appartement")
        self.assertEqual(out["external_id"], "6090489")
        self.assertEqual(out["city"], "Fribourg")

    def test_achat_villa(self):
        out = si._parse_slug("https://x.ch/fr/o/a-vendre-villa-belfaux-5704194")
        self.assertEqual(out["transaction"], "achat")
        self.assertEqual(out["property_type"], "maison")  # villa -> maison
        self.assertEqual(out["external_id"], "5704194")

    def test_no_id(self):
        out = si._parse_slug("https://x.ch/fr/o/a-louer-studio-sion")
        self.assertIsNone(out["external_id"])
        self.assertEqual(out["transaction"], "location")


class CityFromCoreTest(unittest.TestCase):
    def test_canton_suffix_stripped(self):
        # 'a-vendre-domespace-romont-fr' -> Romont (pas 'Fr')
        self.assertEqual(si._city_from_core("a-vendre-domespace-romont-fr"), "Romont")

    def test_multiword_city(self):
        self.assertEqual(si._city_from_core("a-louer-appartement-crans-montana"), "Crans Montana")

    def test_simple_last_token(self):
        self.assertEqual(si._city_from_core("a-louer-appartement-marly"), "Marly")

    def test_empty(self):
        self.assertIsNone(si._city_from_core(""))


class ParseCardTest(unittest.TestCase):
    def test_bulliard_spa_card(self):
        card = si._parse_card(_fixture("immomig_bulliard_card.html"), "https://www.bulliard.ch")
        self.assertIsNotNone(card)
        self.assertEqual(card["obj_id"], "6090489")
        self.assertEqual(card["city"], "Fribourg")
        self.assertEqual(card["transaction"], "location")
        self.assertEqual(card["property_type"], "appartement")
        self.assertEqual(card["price"], 2700)
        self.assertEqual(card["rooms"], 4.5)
        self.assertTrue(card["source_url"].endswith("-6090489"))
        self.assertTrue(card["image"].startswith("https://www.immomigimg.ch/"))

    def test_rfsa_card_icon_and_two_line_location(self):
        # rfsa : <svg> icone AVANT la value, et value = "Ville\nRue N" -> on
        # separe ville (1re ligne) et adresse (le reste), + surface presente.
        card = si._parse_card(_fixture("immomig_rfsa_card.html"), "https://www.rfsa.ch")
        self.assertIsNotNone(card)
        self.assertEqual(card["city"], "La Tour-de-Trême")
        self.assertIn("Rue", card["address"])
        self.assertEqual(card["price"], 1528)
        self.assertEqual(card["rooms"], 2.5)
        self.assertEqual(card["surface"], 67)

    def test_card_without_object_link_returns_none(self):
        self.assertIsNone(si._parse_card("<article>pas de lien objet</article>", "https://x.ch"))


class ScrapeAgencyOrchestrationTest(unittest.TestCase):
    """scrape_immomig_agency avec session HTTP mockee : home (detection) +
    endpoint liste AJAX paginé. Verifie le format _make_property et le filtre
    transaction, sans toucher au reseau ni a la DB."""

    def _fake_session(self, home_html, list_json):
        sess = MagicMock()

        def _get(url, **kwargs):
            resp = MagicMock()
            if "/a/o/search/list" in url:
                # page 1 = donnees, page>=2 = vide (fin de pagination)
                page = int(re.search(r"page=(\d+)", url).group(1))
                resp.status_code = 200
                resp.headers = {"Content-Type": "application/json; charset=utf-8"}
                resp.json.return_value = list_json if page == 1 else {"list": "", "pagination": {}}
            else:
                resp.status_code = 200
                resp.headers = {"Content-Type": "text/html"}
                resp.text = home_html
                resp.url = "https://www.bulliard.ch/fr"
            return resp

        sess.get.side_effect = _get
        return sess

    def test_full_agency_scrape(self):
        # home avec une image immomigimg -> client_id 133 detecte
        home = ('<html><a href="/fr/rent">Louer</a>'
                '<img src="https://www.immomigimg.ch/i/abc/800x450/s/133/pictures/objects/x.jpg"></html>')
        card = _fixture("immomig_bulliard_card.html")
        list_json = {"list": card, "pagination": {"pages": 1}}

        with patch.object(si, "_new_session", return_value=self._fake_session(home, list_json)):
            biens = si.scrape_immomig_agency("bulliard.ch", transaction="location")

        self.assertEqual(len(biens), 1)
        b = biens[0]
        self.assertEqual(b["source"], "bulliard.ch")
        self.assertEqual(b["external_id"], "immomig-133-6090489")
        self.assertEqual(b["transaction"], "location")
        self.assertEqual(b["price"], 2700)
        self.assertEqual(b["rooms"], 4.5)
        self.assertEqual(b["city"], "Fribourg")
        # format _make_property : cles indispensables presentes
        for key in ("external_id", "source", "source_url", "title", "property_type",
                    "transaction", "price", "currency", "scraped_at"):
            self.assertIn(key, b)

    def test_transaction_filter_excludes_other(self):
        home = ('<html><a href="/fr/x"></a>'
                '<img src="https://www.immomigimg.ch/i/a/800x450/s/133/pictures/objects/x.jpg"></html>')
        card = _fixture("immomig_bulliard_card.html")  # bien en location
        list_json = {"list": card, "pagination": {"pages": 1}}
        with patch.object(si, "_new_session", return_value=self._fake_session(home, list_json)):
            biens = si.scrape_immomig_agency("bulliard.ch", transaction="achat")
        self.assertEqual(biens, [])  # la carte est en location -> filtree

    def test_non_immomig_site_returns_empty(self):
        sess = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/html"}
        resp.text = "<html>site WordPress sans immomigimg</html>"
        resp.url = "https://www.example.ch/"
        sess.get.return_value = resp
        with patch.object(si, "_new_session", return_value=sess):
            self.assertEqual(si.scrape_immomig_agency("example.ch"), [])


class SaveToDbCompatTest(unittest.TestCase):
    """Un bien agence doit passer la validation source_url de save_to_db."""

    def test_agency_source_url_is_valid(self):
        import scrapers
        self.assertTrue(scrapers._is_valid_source_url(
            "https://www.bulliard.ch/fr/o/a-louer-appartement-fribourg-6090489", "bulliard.ch"))
        # host qui ne correspond pas au domaine source -> rejete
        self.assertFalse(scrapers._is_valid_source_url(
            "https://www.autre-site.ch/fr/o/x-1", "bulliard.ch"))


if __name__ == "__main__":
    unittest.main()
