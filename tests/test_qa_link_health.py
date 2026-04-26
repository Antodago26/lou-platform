"""
Tests pour qa_link_health_worker (Phase 2 du cron lou-qa-recall).

Lancer : cd backend-v2 && python3 -m unittest tests.test_qa_link_health -v

Couvre les chemins critiques :
  - HEAD direct sur non-Homegate avec status 200 / 301 / 404 / timeout
  - Optimization (e) : Homegate avec scraped_at récent → skip ScrapingBee
  - Homegate avec scraped_at ancien → appel _sb_get(premium_proxy=True)
  - Heuristiques body : DataDome marker → unreachable, "annonce supprimée" → broken
  - Cap max_urls
  - Throttler (espacement 100ms entre 2 calls même domain)

Comme test_qa_recall : pas de DB réelle, get_db/return_db mockés.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', '')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mock_conn():
    """Crée un (conn, cur) MagicMocks. fetchone/fetchall scriptables."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


# ============================================================
# _classify : status code → 4-state mapping
# ============================================================

class ClassifyTest(unittest.TestCase):
    def test_200_ok(self):
        import qa_link_health_worker as worker
        s, code, furl, err = worker._classify(200)
        self.assertEqual(s, 'ok')
        self.assertEqual(code, 200)
        self.assertIsNone(err)

    def test_301_redirect_with_final_url(self):
        import qa_link_health_worker as worker
        s, code, furl, err = worker._classify(301, final_url='https://example.com/new')
        self.assertEqual(s, 'redirect')
        self.assertEqual(furl, 'https://example.com/new')

    def test_404_broken(self):
        import qa_link_health_worker as worker
        s, code, _, _ = worker._classify(404)
        self.assertEqual(s, 'broken')
        self.assertEqual(code, 404)

    def test_500_broken_with_error(self):
        import qa_link_health_worker as worker
        s, _, _, err = worker._classify(503)
        self.assertEqual(s, 'broken')
        self.assertEqual(err, 'server_error_503')

    def test_403_unreachable(self):
        import qa_link_health_worker as worker
        s, _, _, err = worker._classify(403)
        self.assertEqual(s, 'unreachable')
        self.assertEqual(err, 'forbidden_403')

    def test_datadome_in_body_overrides_200_to_unreachable(self):
        """200 trompeur avec DataDome dans le body : DOIT être unreachable.
        C'est le cas observé sur Homegate au probe 26/04 — sans cette
        détection, le worker dépublierait massivement à tort."""
        import qa_link_health_worker as worker
        body = '<html><body>DataDome challenge ... captcha-delivery.com</body></html>'
        s, _, _, err = worker._classify(200, body=body)
        self.assertEqual(s, 'unreachable')
        self.assertEqual(err, 'antibot_in_body')

    def test_broken_marker_in_body_overrides_200(self):
        """200 transport avec "annonce supprimée" dans le body = broken
        applicatif. Cas Homegate où Premium passe le 200 mais sert une
        page d'erreur."""
        import qa_link_health_worker as worker
        body = '<html><body><h1>Page introuvable</h1>...</body></html>'
        s, _, _, err = worker._classify(200, body=body)
        self.assertEqual(s, 'broken')
        self.assertEqual(err, 'deleted_marker_in_body')

    def test_no_response_unreachable(self):
        import qa_link_health_worker as worker
        s, _, _, err = worker._classify(None)
        self.assertEqual(s, 'unreachable')
        self.assertEqual(err, 'no_response')


# ============================================================
# _is_homegate
# ============================================================

class IsHomegateTest(unittest.TestCase):
    def test_source_homegate(self):
        import qa_link_health_worker as worker
        self.assertTrue(worker._is_homegate('homegate', 'https://www.homegate.ch/buy/123'))

    def test_url_homegate_source_empty(self):
        import qa_link_health_worker as worker
        self.assertTrue(worker._is_homegate('', 'https://www.homegate.ch/rent/456'))

    def test_immoscout_not_homegate(self):
        import qa_link_health_worker as worker
        self.assertFalse(worker._is_homegate('immoscout24', 'https://www.immoscout24.ch/fr/d/123'))

    def test_jouval_not_homegate(self):
        import qa_link_health_worker as worker
        self.assertFalse(worker._is_homegate('jouval', 'https://www.jouval.ch/property/abc'))


# ============================================================
# _check_via_head
# ============================================================

class CheckViaHeadTest(unittest.TestCase):
    def test_head_200_returns_ok(self):
        import qa_link_health_worker as worker
        throttler = worker._DomainThrottler()
        fake_resp = MagicMock(status_code=200, headers={})
        with patch.object(worker.requests, 'head', return_value=fake_resp):
            r = worker._check_via_head('https://www.jouval.ch/x', throttler)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['http_code'], 200)

    def test_head_301_returns_redirect_with_final_url(self):
        import qa_link_health_worker as worker
        throttler = worker._DomainThrottler()
        fake_resp = MagicMock(
            status_code=301,
            headers={'Location': 'https://www.jouval.ch/new'},
        )
        with patch.object(worker.requests, 'head', return_value=fake_resp):
            r = worker._check_via_head('https://www.jouval.ch/old', throttler)
        self.assertEqual(r['status'], 'redirect')
        self.assertEqual(r['final_url'], 'https://www.jouval.ch/new')

    def test_head_404_returns_broken(self):
        import qa_link_health_worker as worker
        throttler = worker._DomainThrottler()
        fake_resp = MagicMock(status_code=404, headers={})
        with patch.object(worker.requests, 'head', return_value=fake_resp):
            r = worker._check_via_head('https://www.jouval.ch/dead', throttler)
        self.assertEqual(r['status'], 'broken')

    def test_head_timeout_returns_unreachable(self):
        import qa_link_health_worker as worker
        import requests as real_requests
        throttler = worker._DomainThrottler()
        with patch.object(worker.requests, 'head', side_effect=real_requests.Timeout('boom')):
            r = worker._check_via_head('https://www.jouval.ch/slow', throttler)
        self.assertEqual(r['status'], 'unreachable')
        self.assertEqual(r['error_msg'], 'timeout')


# ============================================================
# Optimization (e) : Homegate avec scraped_at récent
# ============================================================

class HomegateOptimizationTest(unittest.TestCase):
    """Optimization (e) substituée : on utilise properties.scraped_at au
    lieu de qa_recall_snapshots (le snapshot ne stocke pas live_ids).
    Si scraped_at < 7 days, on skip ScrapingBee → status='ok' silencieux."""

    def _setup_db(self, scraped_at_days_ago):
        """Construit un mock DB qui retourne 1 seule property Homegate
        avec scraped_at = NOW() - scraped_at_days_ago."""
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [
            (42,),  # INSERT qa_runs RETURNING id
            # autres fetchone si besoin
        ]
        scraped_at = datetime.now(timezone.utc) - timedelta(days=scraped_at_days_ago)
        cur.fetchall.return_value = [
            (1, 'https://www.homegate.ch/buy/123', 'homegate', scraped_at),
        ]
        return conn, cur

    def test_homegate_scraped_recently_skips_scrapingbee(self):
        """scraped_at = il y a 2 jours → cache_hit, pas d'appel SB."""
        import qa_link_health_worker as worker
        conn, cur = self._setup_db(scraped_at_days_ago=2)
        sb_mock = MagicMock(return_value=(200, ''))
        head_mock = MagicMock()

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'), \
             patch.object(worker, '_sb_get', sb_mock), \
             patch.object(worker.requests, 'head', head_mock):
            result = worker.run_link_health_check(max_urls=1)

        # 1 URL Homegate, optim (e) → cache_hit, status='ok'
        self.assertEqual(result['urls_checked'], 1)
        self.assertEqual(result['cache_hits_homegate'], 1)
        self.assertEqual(result['counts']['ok'], 1)
        self.assertEqual(result['sb_credits_estimated'], 0)

        # Confirmation : ni ScrapingBee ni HEAD direct n'ont été appelés
        sb_mock.assert_not_called()
        head_mock.assert_not_called()

    def test_homegate_scraped_old_uses_premium_scrapingbee(self):
        """scraped_at = il y a 10 jours → fall to ScrapingBee Premium."""
        import qa_link_health_worker as worker
        conn, cur = self._setup_db(scraped_at_days_ago=10)
        # _sb_get retourne (200, body propre) → status='ok' via Premium
        sb_mock = MagicMock(return_value=(200, '<html><body>Apartment 3.5 rooms Neuchâtel</body></html>'))

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'), \
             patch.object(worker, '_sb_get', sb_mock), \
             patch.object(worker, 'sb_bypass_cache', MagicMock()):
            result = worker.run_link_health_check(max_urls=1)

        self.assertEqual(result['urls_checked'], 1)
        self.assertEqual(result['cache_hits_homegate'], 0)
        # 1 appel ScrapingBee Premium = 11 credits
        self.assertEqual(result['sb_credits_estimated'], 11)
        # Le call doit avoir passé premium_proxy=True
        sb_mock.assert_called_once()
        call_kwargs = sb_mock.call_args.kwargs
        self.assertTrue(call_kwargs.get('premium_proxy'),
                        f"_sb_get attendu avec premium_proxy=True, kwargs={call_kwargs}")

    def test_homegate_scraped_null_uses_premium(self):
        """scraped_at NULL (annonce jamais re-scrapée) → fall to Premium."""
        import qa_link_health_worker as worker
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [(7,)]
        cur.fetchall.return_value = [
            (1, 'https://www.homegate.ch/buy/789', 'homegate', None),
        ]
        sb_mock = MagicMock(return_value=(200, '<html>real listing</html>'))

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'), \
             patch.object(worker, '_sb_get', sb_mock), \
             patch.object(worker, 'sb_bypass_cache', MagicMock()):
            result = worker.run_link_health_check(max_urls=1)

        self.assertEqual(result['cache_hits_homegate'], 0)
        self.assertEqual(result['sb_credits_estimated'], 11)
        sb_mock.assert_called_once()


# ============================================================
# End-to-end : non-Homegate uses HEAD direct
# ============================================================

class NonHomegateRoutingTest(unittest.TestCase):
    def test_non_homegate_uses_head_not_scrapingbee(self):
        """URL Jouval → HEAD direct, pas ScrapingBee, peu importe scraped_at."""
        import qa_link_health_worker as worker
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [(7,)]
        cur.fetchall.return_value = [
            (1, 'https://www.jouval.ch/property/abc', 'jouval',
             datetime.now(timezone.utc) - timedelta(days=20)),  # vieux mais non-Homegate
        ]
        sb_mock = MagicMock()
        head_mock = MagicMock(return_value=MagicMock(status_code=200, headers={}))

        with patch.object(worker, 'get_db', return_value=conn), \
             patch.object(worker, 'return_db'), \
             patch.object(worker, '_sb_get', sb_mock), \
             patch.object(worker.requests, 'head', head_mock):
            result = worker.run_link_health_check(max_urls=1)

        self.assertEqual(result['urls_checked'], 1)
        self.assertEqual(result['counts']['ok'], 1)
        self.assertEqual(result['sb_credits_estimated'], 0)
        sb_mock.assert_not_called()
        head_mock.assert_called_once()


# ============================================================
# Throttler
# ============================================================

class DomainThrottlerTest(unittest.TestCase):
    def test_throttler_blocks_min_interval_same_domain(self):
        """Deux calls même domain consécutifs : le 2e attend ≥ 100 ms."""
        import qa_link_health_worker as worker
        import time
        throttler = worker._DomainThrottler()
        throttler.wait('example.com')
        t0 = time.monotonic()
        throttler.wait('example.com')
        elapsed = time.monotonic() - t0
        # _MIN_INTERVAL_PER_DOMAIN_S = 0.1 ; tolerance large
        self.assertGreaterEqual(elapsed, 0.08)

    def test_throttler_no_wait_different_domains(self):
        """Deux calls différents domains : pas d'attente."""
        import qa_link_health_worker as worker
        import time
        throttler = worker._DomainThrottler()
        throttler.wait('example.com')
        t0 = time.monotonic()
        throttler.wait('other.com')
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.05)


if __name__ == '__main__':
    unittest.main()
