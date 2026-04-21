"""
Tests pour la refonte QA v6.4.0 — worker recall + endpoint lecture snapshot.

Lancer : cd backend-v2 && python3 -m unittest tests.test_qa_recall -v

Scope :
  - Worker `run_recall_snapshot_for_city` : scrapers + DB mockés, on
    vérifie que les bons INSERT/UPDATE sont émis et que les compteurs
    sont cohérents.
  - Endpoint `GET /api/stats/listings-qa` : on monte un Flask minimal
    avec seulement `stats_bp`, on mocke get_db/return_db, on vérifie
    les 3 chemins principaux (503 no snapshot, 200 avec snapshot, garde-
    fou ORDER BY captured_at DESC LIMIT 1).

Pas de DB réelle (comme tests/test_return_db.py). QA_TOKEN est posé AVANT
l'import de routes_stats sinon `_check_token` est figé à vide.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', '')
# Token arbitraire, doit matcher l'auth_headers des tests endpoint.
os.environ.setdefault('QA_TOKEN', 'test-qa-token-xxx')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# Worker tests
# ============================================================

class RunRecallSnapshotForCityTest(unittest.TestCase):
    """Vérifie que run_recall_snapshot_for_city insère bien qa_runs +
    qa_recall_snapshots, et calcule correctement recall_pct à partir
    des IDs scrapés vs indexés."""

    def _fake_listings(self, transaction):
        """Factory de listings pour tous les scrapers : 2 IDs par combo."""
        return [
            {'external_id': f'live-{transaction}-1'},
            {'external_id': f'live-{transaction}-2'},
        ]

    def test_run_recall_snapshot_for_city_inserts_row(self):
        """Happy path : 4 combos × 2 live IDs = 8 source_total ; DB mocke
        renvoie 1 ID matching par combo = 4 our_total ; recall = 50%.
        Doit émettre : 1 INSERT qa_runs, 4 SELECT our_ids, 1 INSERT
        snapshot, 1 UPDATE qa_runs."""
        import qa_recall_worker as worker

        # Cursor scripting :
        #   fetchone() est appelé pour :
        #     - RETURNING id du INSERT qa_runs      → (7,)
        #     - RETURNING id du INSERT snapshot     → (42,)
        #   fetchall() est appelé pour :
        #     - 4× SELECT our_ids (un par combo)    → 1 row chacun (50% recall)
        fetchone_seq = [(7,), (42,)]
        fetchall_seq = [
            [('live-achat-1',)],       # homegate_achat:    matche 1/2
            [('live-location-1',)],    # homegate_location: matche 1/2
            [('live-achat-1',)],       # immoscout24_achat: matche 1/2
            [('live-location-1',)],    # immoscout24_loc:   matche 1/2
        ]
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = fetchone_seq
        cur.fetchall.side_effect = fetchall_seq

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'), \
             patch.object(worker, 'scrape_homegate',
                          side_effect=lambda city, transaction, max_pages: self._fake_listings(transaction)), \
             patch.object(worker, 'scrape_immoscout',
                          side_effect=lambda city, transaction, max_pages: self._fake_listings(transaction)), \
             patch.object(worker, 'sb_budget', MagicMock()):
            result = worker.run_recall_snapshot_for_city('peseux')

        # Compteurs
        self.assertEqual(result['source_total'], 8)  # 2 listings × 4 combos
        self.assertEqual(result['our_total'], 4)     # 1 match × 4 combos
        self.assertEqual(result['recall_pct'], 50.0)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['run_id'], 7)
        self.assertEqual(result['snapshot_id'], 42)

        # Les bons SQLs ont été émis (on inspecte le 1er arg de chaque execute)
        sqls = ' '.join(str(c.args[0]) for c in cur.execute.call_args_list)
        self.assertIn('INSERT INTO qa_runs', sqls)
        self.assertIn('INSERT INTO qa_recall_snapshots', sqls)
        self.assertIn('UPDATE qa_runs', sqls)
        # 4 combos = 4 SELECT our_ids
        self.assertGreaterEqual(sqls.count('UNION ALL'), 4)

    def test_run_recall_unknown_city_raises(self):
        """Slug absent de _CITY_SLUG_TO_DISPLAY → ValueError claire.
        Pas de DB touchée."""
        import qa_recall_worker as worker
        with patch.object(worker, 'get_db') as get_db_mock:
            with self.assertRaises(ValueError):
                worker.run_recall_snapshot_for_city('ville-inexistante-xyz')
            get_db_mock.assert_not_called()


# ============================================================
# Endpoint tests
# ============================================================

class EndpointListingsQaTest(unittest.TestCase):
    """Endpoint GET /api/stats/listings-qa — lecture snapshot uniquement."""

    def setUp(self):
        # Import tardif pour que QA_TOKEN (posé au haut du fichier) soit vu
        # au module-load de routes_stats.
        import routes_stats
        from flask import Flask
        self.routes_stats = routes_stats
        self.app = Flask(__name__)
        self.app.register_blueprint(routes_stats.stats_bp)
        self.client = self.app.test_client()
        self.auth_headers = {'X-QA-Token': 'test-qa-token-xxx'}

    def _mock_db(self, fetchone_value):
        """Retourne (conn, cur) MagicMocks avec fetchone() scripté."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = fetchone_value
        return conn, cur

    def test_endpoint_returns_503_when_no_snapshot(self):
        """Aucun row en DB → 503 snapshot_not_ready avec message explicite
        pointant vers le prochain run cron (04:00 UTC)."""
        conn, cur = self._mock_db(fetchone_value=None)
        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            resp = self.client.get(
                '/api/stats/listings-qa?city=peseux',
                headers=self.auth_headers,
            )
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertEqual(body['error'], 'snapshot_not_ready')
        self.assertEqual(body['city'], 'peseux')
        self.assertIn('04:00 UTC', body.get('message', ''))

    def test_endpoint_returns_snapshot_with_age(self):
        """Row présent → 200 avec snapshot_age_hours calculé depuis
        captured_at. Shape de réponse = exactement ce que le front
        (et Cowork après update) consomme."""
        captured_at = datetime.now(timezone.utc) - timedelta(hours=5, minutes=30)
        row = (
            42,                                    # id (ignoré par l'endpoint)
            captured_at,                           # captured_at
            100,                                   # source_total_listings
            95,                                    # our_total_listings
            Decimal('95.00'),                      # recall_pct (NUMERIC)
            [{'portal': 'homegate', 'transaction': 'achat', 'id': 'hg-1'}],
            {'homegate_achat': {'source_total': 50, 'our_total': 48, 'recall_pct': 96.0}},
        )
        conn, cur = self._mock_db(fetchone_value=row)

        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            resp = self.client.get(
                '/api/stats/listings-qa?city=peseux',
                headers=self.auth_headers,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['city'], 'peseux')
        self.assertEqual(body['source_total_listings'], 100)
        self.assertEqual(body['our_total_listings'], 95)
        self.assertEqual(body['recall_pct'], 95.0)
        self.assertAlmostEqual(body['snapshot_age_hours'], 5.5, places=1)
        self.assertIn('homegate_achat', body['breakdown'])
        self.assertEqual(len(body['missing_listing_ids']), 1)

    def test_endpoint_returns_oldest_snapshot_correctly_filtered(self):
        """Garde-fou : l'endpoint DOIT ordonner par captured_at DESC et
        LIMIT 1. On ne peut pas tester l'ordering réel sans DB (fetchone
        est mocké), mais on peut vérifier que le SQL émis contient bien
        ces clauses — prévient une régression silencieuse qui servirait
        un vieux snapshot.
        """
        conn, cur = self._mock_db(fetchone_value=None)
        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            self.client.get(
                '/api/stats/listings-qa?city=peseux',
                headers=self.auth_headers,
            )
        sqls = ' '.join(str(c.args[0]) for c in cur.execute.call_args_list)
        self.assertIn('ORDER BY captured_at DESC', sqls)
        self.assertIn('LIMIT 1', sqls)
        # Garde-fou bonus : pas de scrape résiduel côté endpoint
        self.assertNotIn('sb_budget', sqls)
        self.assertNotIn('scrape_', sqls)

    def test_endpoint_401_without_token(self):
        """Sécurité : pas de header X-QA-Token → 401, DB pas touchée."""
        with patch.object(self.routes_stats, 'get_db') as get_db_mock:
            resp = self.client.get('/api/stats/listings-qa?city=peseux')
        self.assertEqual(resp.status_code, 401)
        get_db_mock.assert_not_called()

    def test_endpoint_400_without_city(self):
        """Garde-fou UX : ?city= absent → 400, pas 503 / pas de DB."""
        with patch.object(self.routes_stats, 'get_db') as get_db_mock:
            resp = self.client.get(
                '/api/stats/listings-qa',
                headers=self.auth_headers,
            )
        self.assertEqual(resp.status_code, 400)
        get_db_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
