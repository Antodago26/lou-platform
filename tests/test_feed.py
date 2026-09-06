"""Tests du feed mobile : phrase de Lou et validation des swipes (sans DB)."""
import unittest

from routes_feed import lou_note, validate_swipe


def _prop(**over):
    base = {
        'score_detail': {'zone': 100, 'budget': 95, 'surface': 80, 'equipment': 70},
        'distance_km': None, 'features': ['balcon', 'cave'], 'days_online': 1, 'surface': 78,
    }
    base.update(over)
    return base


class LouNoteTest(unittest.TestCase):
    def test_feature_budget_zone(self):
        s = lou_note(_prop())
        self.assertTrue(s.startswith('Balcon, dans ton budget, en plein dans ta zone.'), s)
        self.assertIn('Publié hier', s)

    def test_bemol_budget(self):
        s = lou_note(_prop(score_detail={'zone': 100, 'budget': 40, 'surface': 90}))
        self.assertIn('Seul bémol : nettement au-dessus de ton budget', s)

    def test_distance(self):
        s = lou_note(_prop(score_detail={'zone': 60, 'budget': 95, 'surface': 90},
                           distance_km=12.4, features=[]))
        self.assertIn('à 12 km de ta zone', s)
        self.assertIn('12 km de trajet', s)

    def test_nothing_known(self):
        s = lou_note({'score_detail': {}, 'features': [], 'distance_km': None, 'days_online': 5})
        self.assertEqual(s, 'Correspond à tes critères.')

    def test_surface_neutral_not_a_bemol(self):
        s = lou_note(_prop(score_detail={'zone': 100, 'budget': 95, 'surface': 50}))
        self.assertNotIn('surface', s)

    def test_surface_really_small(self):
        s = lou_note(_prop(score_detail={'zone': 100, 'budget': 95, 'surface': 30}))
        self.assertIn('surface un peu juste', s)

    def test_published_today(self):
        s = lou_note(_prop(days_online=0))
        self.assertIn("Publié aujourd'hui", s)


class ValidateSwipeTest(unittest.TestCase):
    def test_ok(self):
        parsed, err = validate_swipe({'property_id': '12', 'action': 'LIKE'})
        self.assertIsNone(err)
        self.assertEqual(parsed, (12, 'like'))

    def test_bad_action(self):
        parsed, err = validate_swipe({'property_id': 1, 'action': 'love'})
        self.assertIsNone(parsed)
        self.assertIn('action', err)

    def test_bad_id(self):
        for bad in (None, 'x', 0, -3):
            parsed, err = validate_swipe({'property_id': bad, 'action': 'pass'})
            self.assertIsNone(parsed, bad)

    def test_not_dict(self):
        parsed, err = validate_swipe(None)
        self.assertIsNone(parsed)


if __name__ == '__main__':
    unittest.main()
