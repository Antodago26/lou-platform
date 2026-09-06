"""Tests du scraper anibis : jeton de recherche et parseurs (sans reseau)."""
import unittest

from scraper_anibis import (
    encode_search_token, decode_search_token, build_search,
    parse_price, parse_rooms_surface, parse_list_node, parse_detail,
)

REF_TOKEN = ("Ak8CqcmVhbEVzdGF0ZZSSkqtsaXN0aW5nVHlwZalhcGFydG1lbnSSqXByaWNlVHlwZaRSRU5U"
             "wMCRk6hsb2NhdGlvbrFnZW8tY2l0eS1sYXVzYW5uZcA")


class TokenTest(unittest.TestCase):
    def test_roundtrip_matches_site_token(self):
        obj = [None, 'realEstate', [[['listingType', 'apartment'], ['priceType', 'RENT']],
                                    None, None, [['location', 'geo-city-lausanne', None]]]]
        self.assertEqual(encode_search_token(obj), REF_TOKEN)
        self.assertEqual(decode_search_token(REF_TOKEN), obj)

    def test_build_search_canton_and_city(self):
        self.assertEqual(build_search('achat', 'house', canton='NE'),
                         [None, 'realEstate', [[['listingType', 'house'], ['priceType', 'BUY']], None, None,
                                               [['location', 'geo-canton-neuchatel', None]]]])
        self.assertEqual(build_search('location', None, city='Neuchâtel', radius_km=10)[2][3],
                         [['location', 'geo-city-neuchatel', 10]])


class ParserTest(unittest.TestCase):
    def test_price(self):
        self.assertEqual(parse_price('2 280.- par mois'), 2280)
        self.assertEqual(parse_price("1 250 000.-"), 1250000)
        self.assertIsNone(parse_price('Prix sur demande'))
        self.assertIsNone(parse_price(None))

    def test_rooms_surface(self):
        self.assertEqual(parse_rooms_surface('Bel appartement 3,5 pièces de 82 m2'), (3.5, 82))
        self.assertEqual(parse_rooms_surface('2.5p attique 60m²'), (2.5, 60))
        self.assertEqual(parse_rooms_surface('Studio meublé'), (None, None))

    def test_list_node(self):
        node = {
            'listingID': '1073907446',
            'localization': {'title': 'Location appartement', 'body': '2.5p\nTerasse 19m2 vue lac'},
            'postcodeInformation': {'postcode': '1004', 'locationName': 'Lausanne', 'canton': {'shortName': 'VD'}},
            'timestamp': '2026-09-06T19:43:07+02:00',
            'formattedPrice': '2 280.- par mois',
            'thumbnail': {'retinaRendition': {'src': 'https://c.anibis.ch/big/1.jpg'}},
            'seoInformation': {'frSlug': 'vaud/immobilier/appartements/location-appartement'},
        }
        p = parse_list_node(node, 'location')
        self.assertEqual(p['source_url'], 'https://www.anibis.ch/fr/vi/vaud/immobilier/appartements/location-appartement/1073907446')
        self.assertEqual(p['price'], 2280)
        self.assertEqual(p['rooms'], 2.5)
        self.assertEqual(p['canton'], 'VD')
        self.assertIn('terrasse', p['features'])
        self.assertIn('vue', p['features'])
        self.assertEqual(p['images'], ['https://c.anibis.ch/big/1.jpg'])

    def test_detail(self):
        nd = {'props': {'pageProps': {'dehydratedState': {'queries': [{
            'queryKey': ['GetListingDetails', {'id': '1'}],
            'state': {'data': {'listing': {
                'listingID': '1',
                'properties': [
                    {'listingPropertyID': 'apartment_realEstateRooms', 'text': '2'},
                    {'listingPropertyID': 'apartment_realEstateSize', 'text': '60'},
                    {'listingPropertyID': 'synthetic_address', 'text': 'Avenue X 61, Lausanne'},
                ],
                'coordinates': {'latitude': 46.53, 'longitude': 6.61},
                'images': [{'rendition': {'src': 'https://c.anibis.ch/big/a.jpg'}}, {'rendition': {'src': 'https://c.anibis.ch/big/b.jpg'}}],
                'sellerInfo': {'alias': 'Yaya '},
            }}}}]}}}}
        d = parse_detail(nd)
        self.assertEqual(d['rooms'], 2.0)
        self.assertEqual(d['surface'], 60)
        self.assertEqual(d['address'], 'Avenue X 61, Lausanne')
        self.assertEqual(d['latitude'], 46.53)
        self.assertEqual(len(d['images']), 2)
        self.assertEqual(d['contact_name'], 'Yaya')


if __name__ == '__main__':
    unittest.main()


class WantedAdTest(unittest.TestCase):
    def test_wanted(self):
        from scraper_anibis import is_wanted_ad
        self.assertTrue(is_wanted_ad('Famille sérieuse cherche maison avec jardin'))
        self.assertTrue(is_wanted_ad('Cherche appartement 3 pièces'))
        self.assertTrue(is_wanted_ad('Recherche 4.5 pièces Neuchâtel'))
        self.assertFalse(is_wanted_ad('Bel appartement 3.5 pièces avec balcon'))
        self.assertFalse(is_wanted_ad('Location appartement', 'Grand 4 pièces, recherche locataire calme'))


class CleanCityTest(unittest.TestCase):
    def test_clean(self):
        from scraper_anibis import clean_city
        self.assertEqual(clean_city('Colombier NE'), 'Colombier')
        self.assertEqual(clean_city('Corcelles NE'), 'Corcelles')
        self.assertEqual(clean_city('St-Blaise'), 'Saint-Blaise')
        self.assertEqual(clean_city('Biel/Bienne'), 'Bienne')
        self.assertEqual(clean_city('Neuchâtel'), 'Neuchâtel')
        self.assertEqual(clean_city('La Chaux-de-Fonds'), 'La Chaux-de-Fonds')
