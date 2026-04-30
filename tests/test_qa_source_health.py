"""
Tests pour qa_source_health_worker + endpoint /api/stats/listings-qa
v6.4.4 (repurpose après drop Homegate + ImmoScout24, CEO 30/04).

Lancer : cd backend-v2 && python3 -m unittest tests.test_qa_source_health -v

Scope :
  - Worker `run_source_health_snapshot` : DB mockée, on vérifie que les
    bons INSERT sont émis, que la classification ok|warn|fail respecte
    les seuils, et que `qa_runs` est finalisé avec le statut attendu.
  - Endpoint `GET /api/stats/listings-qa` : Flask minimal + DB mockée,
    on vérifie 503 sans snapshot, 200 avec rows, 401 sans token.
  - Garde-fou : flag ENABLE_HOMEGATE=false côté cron_job.py n'appelle
    pas scrape_homegate (vérifié via DISABLED_SOURCES set).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', '')
os.environ.setdefault('QA_TOKEN', 'test-qa-token-xxx')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# Worker : classification ok|warn|fail
# ============================================================

class ClassifyTest(unittest.TestCase):
    """Couvre les 3 statuts + le cas last_scrape NULL."""

    def setUp(self):
        from qa_source_health_worker import _classify
        self.classify = _classify
        self.now = datetime(2026, 4, 30, 4, 0, 0, tzinfo=timezone.utc)

    def test_ok_when_scraped_7d_positive(self):
        stats = {
            'scraped_7d': 12, 'last_scrape': self.now - timedelta(hours=2),
        }
        self.assertEqual(self.classify(stats, self.now), 'ok')

    def test_warn_when_no_7d_but_recent(self):
        stats = {
            'scraped_7d': 0,
            'last_scrape': self.now - timedelta(days=15),
        }
        self.assertEqual(self.classify(stats, self.now), 'warn')

    def test_fail_when_older_than_21d(self):
        stats = {
            'scraped_7d': 0,
            'last_scrape': self.now - timedelta(days=22),
        }
        self.assertEqual(self.classify(stats, self.now), 'fail')

    def test_fail_when_last_scrape_null(self):
        stats = {'scraped_7d': 0, 'last_scrape': None}
        self.assertEqual(self.classify(stats, self.now), 'fail')

    def test_warn_at_21d_boundary(self):
        # 21 jours pile = encore warn (la borne exclusive est > 21).
        stats = {
            'scraped_7d': 0,
            'last_scrape': self.now - timedelta(days=21),
        }
        self.assertEqual(self.classify(stats, self.now), 'warn')


# ============================================================
# Worker : run_source_health_snapshot end-to-end
# ============================================================

class RunSourceHealthSnapshotTest(unittest.TestCase):
    """Vérifie le flow complet : qa_runs INSERT → SELECT agrégé → INSERT
    qa_source_health (1 row par source) → UPDATE qa_runs."""

    def test_writes_one_row_per_source_and_finalizes_run(self):
        import qa_source_health_worker as worker

        now = datetime.now(timezone.utc)

        # cur.fetchone : 1× INSERT qa_runs RETURNING id → (5,)
        # cur.fetchall : 1× SELECT agrégé → 3 sources (ok/warn/fail)
        # cur.executemany : INSERT qa_source_health batch
        # cur.execute (UPDATE qa_runs) : pas de fetch
        fetchall_rows = [
            ('Flatfox',       420, 380, 410, now - timedelta(hours=2)),
            ('Immobilier.ch',  88,   0,  88, now - timedelta(days=15)),
            ('Anibis',         50,   0,   0, now - timedelta(days=30)),
        ]

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(5,)]
        cur.fetchall.return_value = fetchall_rows

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'):
            result = worker.run_source_health_snapshot()

        self.assertEqual(result['run_id'], 5)
        self.assertEqual(result['sources_total'], 3)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['status'], 'success')

        # Status par source bien dérivé
        statuses = {s['source']: s['status'] for s in result['sources']}
        self.assertEqual(statuses['Flatfox'],       'ok')
        self.assertEqual(statuses['Immobilier.ch'], 'warn')
        self.assertEqual(statuses['Anibis'],        'fail')

        # SQLs émis
        sqls = ' '.join(str(c.args[0]) for c in cur.execute.call_args_list)
        self.assertIn('INSERT INTO qa_runs', sqls)
        self.assertIn('UPDATE qa_runs', sqls)
        self.assertIn('FROM properties', sqls)
        # executemany pour qa_source_health
        em_sqls = ' '.join(str(c.args[0]) for c in cur.executemany.call_args_list)
        self.assertIn('INSERT INTO qa_source_health', em_sqls)

    def test_failed_status_when_no_sources(self):
        """DB vide → aucun row qa_source_health → status='failed'."""
        import qa_source_health_worker as worker

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(99,)]
        cur.fetchall.return_value = []

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'):
            result = worker.run_source_health_snapshot()

        self.assertEqual(result['sources_total'], 0)
        self.assertEqual(result['status'], 'failed')


# ============================================================
# Endpoint
# ============================================================

class EndpointListingsQaTest(unittest.TestCase):
    """Endpoint repurpose : retourne sources health, pas city recall."""

    def setUp(self):
        import routes_stats
        from flask import Flask
        self.routes_stats = routes_stats
        self.app = Flask(__name__)
        self.app.register_blueprint(routes_stats.stats_bp)
        self.client = self.app.test_client()
        self.auth_headers = {'X-QA-Token': 'test-qa-token-xxx'}

    def _mock_db(self, fetchall_value):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = fetchall_value
        return conn, cur

    def test_503_when_no_rows(self):
        conn, cur = self._mock_db(fetchall_value=[])
        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            resp = self.client.get(
                '/api/stats/listings-qa',
                headers=self.auth_headers,
            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['error'], 'snapshot_not_ready')

    def test_200_returns_sources_with_age(self):
        captured_at = datetime.now(timezone.utc) - timedelta(hours=3)
        last_scrape = captured_at - timedelta(hours=1)
        rows = [
            ('Flatfox',       captured_at, 412, 380, 410, last_scrape, 'ok'),
            ('Immobilier.ch', captured_at,  89,   0,  89, last_scrape, 'warn'),
        ]
        conn, cur = self._mock_db(fetchall_value=rows)

        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            resp = self.client.get(
                '/api/stats/listings-qa',
                headers=self.auth_headers,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()

        self.assertEqual(len(body['sources']), 2)
        self.assertAlmostEqual(body['snapshot_age_hours'], 3.0, places=1)
        flatfox = next(s for s in body['sources'] if s['source'] == 'Flatfox')
        self.assertEqual(flatfox['status'], 'ok')
        self.assertEqual(flatfox['total_active'], 412)
        self.assertEqual(flatfox['scraped_7d'], 380)

    def test_endpoint_uses_distinct_on_source(self):
        """Garde-fou : la query DOIT contenir DISTINCT ON (source) +
        ORDER BY source, captured_at DESC. Sans ça, on peut servir
        plusieurs rows par source ou un row stale."""
        conn, cur = self._mock_db(fetchall_value=[])
        with patch.object(self.routes_stats, 'get_db', return_value=conn), \
             patch.object(self.routes_stats, 'return_db'):
            self.client.get(
                '/api/stats/listings-qa',
                headers=self.auth_headers,
            )
        sqls = ' '.join(str(c.args[0]) for c in cur.execute.call_args_list)
        self.assertIn('DISTINCT ON (source)', sqls)
        self.assertIn('ORDER BY source, captured_at DESC', sqls)

    def test_401_without_token(self):
        with patch.object(self.routes_stats, 'get_db') as get_db_mock:
            resp = self.client.get('/api/stats/listings-qa')
        self.assertEqual(resp.status_code, 401)
        get_db_mock.assert_not_called()


# ============================================================
# Cron flags : DISABLED_SOURCES
# ============================================================

class CronJobDisabledSourcesTest(unittest.TestCase):
    """ENABLE_HOMEGATE / ENABLE_IMMOSCOUT24 doivent peupler DISABLED_SOURCES
    et le set est passé à scrape_all qui filtre la liste de scrapers."""

    def test_flag_disabled_helper(self):
        """`_flag_disabled` lit l'env et matche les strings 'false'/'0'/etc.
        L'absence de la var renvoie False (= source activée par défaut).
        """
        import cron_job

        with patch.dict(os.environ, {'TESTFLAG_X': 'false'}, clear=False):
            self.assertTrue(cron_job._flag_disabled('TESTFLAG_X'))
        with patch.dict(os.environ, {'TESTFLAG_X': '0'}, clear=False):
            self.assertTrue(cron_job._flag_disabled('TESTFLAG_X'))
        with patch.dict(os.environ, {'TESTFLAG_X': 'true'}, clear=False):
            self.assertFalse(cron_job._flag_disabled('TESTFLAG_X'))

        # Vide → activé (préserve comportement historique).
        env_no_x = {k: v for k, v in os.environ.items() if k != 'TESTFLAG_X'}
        with patch.dict(os.environ, env_no_x, clear=True):
            self.assertFalse(cron_job._flag_disabled('TESTFLAG_X'))

    def test_scrape_all_skips_disabled_sources(self):
        """scrape_all(disabled_sources={'Homegate', 'ImmoScout24'}) ne doit
        appeler ni scrape_homegate ni scrape_immoscout."""
        import scrapers

        homegate_mock = MagicMock(return_value=[])
        immoscout_mock = MagicMock(return_value=[])
        flatfox_mock = MagicMock(return_value=[])

        with patch.object(scrapers, 'scrape_homegate',  homegate_mock), \
             patch.object(scrapers, 'scrape_immoscout', immoscout_mock), \
             patch.object(scrapers, 'scrape_flatfox',   flatfox_mock), \
             patch.object(scrapers, 'scrape_immobilier_ch', MagicMock(return_value=[])), \
             patch.object(scrapers, 'scrape_acheter_louer', MagicMock(return_value=[])), \
             patch.object(scrapers, 'scrape_comparis',   MagicMock(return_value=[])), \
             patch.object(scrapers, 'scrape_properstar', MagicMock(return_value=[])):
            # Lausanne (canton VD) plutôt que NE pour éviter les agency
            # scrapers spécifiques NE (Jouval/Muller&Christe/Fidimmobil)
            # qui tapent réellement le réseau côté HTTP direct.
            scrapers.scrape_all(
                city='Lausanne', transaction='location',
                skip_nearby=True,
                disabled_sources={'Homegate', 'ImmoScout24'},
            )

        homegate_mock.assert_not_called()
        immoscout_mock.assert_not_called()
        # Flatfox toujours actif (sanity check)
        flatfox_mock.assert_called()


if __name__ == '__main__':
    unittest.main()
