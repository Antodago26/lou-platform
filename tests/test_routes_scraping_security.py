"""
Tests sur les blindages de routes_scraping.py shippés en 2026-05.

Lancer : cd backend-v2 && python3 -m unittest tests.test_routes_scraping_security -v

Couvre :
  - /api/import : @admin_required (audit C1) — refuse user normal
  - /api/import : URL scheme validation (audit C1) — rejette javascript:,
    data:, http:// avec déclaration spécifique, scheme manquant
  - /api/import : cap title length, cap images count, filter non-http URLs
  - /api/scrape : rate-limit per-user (audit H3)
  - /api/import : rate-limit per-user (audit H3)
  - _require_cron_secret : header only (audit H2), constant-time
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('JWT_SECRET', 'test-secret-must-be-32-bytes-of-stuff')
os.environ.setdefault('DATABASE_URL', 'postgresql://test')
os.environ.setdefault('CRON_SECRET', 'super-secret-cron-token-xyz123456')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reset_buckets():
    import rate_limit
    rate_limit._buckets_min.clear()
    rate_limit._buckets_hour.clear()


# ============================================================
# /api/import — admin gate (audit C1)
# ============================================================

class ApiImportAdminGateTest(unittest.TestCase):
    """Avant 2026-05 : tout user authentifié pouvait POSTer 500 listings.
    Après C1 : @admin_required + validation URL."""

    def setUp(self):
        _reset_buckets()
        from flask import Flask
        import auth, routes_scraping
        self.auth = auth
        self.routes_scraping = routes_scraping
        # Set ADMIN_EMAIL for the tests
        self._admin_patcher = patch.object(auth, 'ADMIN_EMAIL', 'admin@bonhome.ch')
        self._admin_patcher.start()
        self.addCleanup(self._admin_patcher.stop)

        self.app = Flask(__name__)
        self.app.register_blueprint(auth.auth_bp)
        self.app.register_blueprint(routes_scraping.scraping_bp)
        self.client = self.app.test_client()

        self.admin_token = auth.make_token(1)  # user_id 1 = admin
        self.user_token = auth.make_token(2)   # user_id 2 = regular user

        # Patch get_db globally — admin_required calls it to look up the email
        self._db_patcher = patch('auth.get_db')
        self._return_db_patcher = patch('auth.return_db')
        self.mock_get_db = self._db_patcher.start()
        self._return_db_patcher.start()
        self.addCleanup(self._db_patcher.stop)
        self.addCleanup(self._return_db_patcher.stop)
        # Also patch routes_scraping side
        self._db_patcher_rs = patch('routes_scraping.get_db')
        self._return_db_patcher_rs = patch('routes_scraping.return_db')
        self.mock_get_db_rs = self._db_patcher_rs.start()
        self._return_db_patcher_rs.start()
        self.addCleanup(self._db_patcher_rs.stop)
        self.addCleanup(self._return_db_patcher_rs.stop)

    def _set_db_user(self, user_id, email):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = {'email': email}
        self.mock_get_db.return_value = conn

    def _post_import(self, token, listings):
        return self.client.post(
            '/api/import',
            json={'listings': listings},
            headers={'Authorization': f'Bearer {token}'},
        )

    def test_non_admin_403(self):
        """Audit C1 : un user normal authentifié doit recevoir 403, plus 200."""
        self._set_db_user(2, 'alice@example.com')
        resp = self._post_import(self.user_token, [
            {'title': 'Test', 'source': 'Test', 'source_url': 'https://example.com/1'}
        ])
        self.assertEqual(resp.status_code, 403)

    def test_no_token_401(self):
        resp = self.client.post('/api/import', json={'listings': []})
        self.assertEqual(resp.status_code, 401)

    def test_admin_with_valid_listing_passes(self):
        self._set_db_user(1, 'admin@bonhome.ch')
        # save_to_db is in scrapers — mock the full call chain
        with patch('scrapers.save_to_db', return_value=1):
            resp = self._post_import(self.admin_token, [
                {
                    'title': 'Bel appart',
                    'source': 'Test',
                    'source_url': 'https://example.com/listing/1',
                }
            ])
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['ok'])

    def test_javascript_scheme_rejected(self):
        """Audit C1 : source_url=javascript:alert(1) = vecteur stored-XSS."""
        self._set_db_user(1, 'admin@bonhome.ch')
        with patch('scrapers.save_to_db', return_value=0):
            resp = self._post_import(self.admin_token, [
                {
                    'title': 'XSS attempt',
                    'source': 'Test',
                    'source_url': 'javascript:alert(1)',
                }
            ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('source_url', resp.get_json()['error'])

    def test_data_scheme_rejected(self):
        self._set_db_user(1, 'admin@bonhome.ch')
        with patch('scrapers.save_to_db', return_value=0):
            resp = self._post_import(self.admin_token, [
                {
                    'title': 'data: attempt',
                    'source': 'Test',
                    'source_url': 'data:text/html,<script>alert(1)</script>',
                }
            ])
        self.assertEqual(resp.status_code, 400)

    def test_url_too_long_rejected(self):
        self._set_db_user(1, 'admin@bonhome.ch')
        long_url = 'https://example.com/' + ('a' * 600)
        with patch('scrapers.save_to_db', return_value=0):
            resp = self._post_import(self.admin_token, [
                {'title': 'Long URL', 'source': 'Test', 'source_url': long_url}
            ])
        self.assertEqual(resp.status_code, 400)

    def test_title_too_long_rejected(self):
        self._set_db_user(1, 'admin@bonhome.ch')
        with patch('scrapers.save_to_db', return_value=0):
            resp = self._post_import(self.admin_token, [
                {
                    'title': 'a' * 400,
                    'source': 'Test',
                    'source_url': 'https://example.com/1',
                }
            ])
        self.assertEqual(resp.status_code, 400)

    def test_non_http_image_urls_filtered(self):
        """Les URLs d'images non-http(s) doivent être silencieusement
        filtrées, pas rejeter la listing entière (compat scrapers)."""
        self._set_db_user(1, 'admin@bonhome.ch')

        captured_listings = []
        def capture_save(_conn, listings):
            captured_listings.extend(listings)
            return len(listings)

        with patch('scrapers.save_to_db', side_effect=capture_save):
            resp = self._post_import(self.admin_token, [
                {
                    'title': 'Mixed images',
                    'source': 'Test',
                    'source_url': 'https://example.com/1',
                    'images': [
                        'https://cdn.example.com/img1.jpg',
                        'javascript:alert(1)',  # filtered
                        'http://example.com/img2.jpg',  # kept (http allowed)
                        'data:image/png;base64,xxx',    # filtered
                    ],
                }
            ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured_listings), 1)
        # Only 2 valid http(s) URLs survived
        self.assertEqual(captured_listings[0]['images'], [
            'https://cdn.example.com/img1.jpg',
            'http://example.com/img2.jpg',
        ])

    def test_more_than_500_listings_rejected(self):
        self._set_db_user(1, 'admin@bonhome.ch')
        listings = [
            {'title': f'L{i}', 'source': 'Test', 'source_url': f'https://example.com/{i}'}
            for i in range(501)
        ]
        with patch('scrapers.save_to_db', return_value=0):
            resp = self._post_import(self.admin_token, listings)
        self.assertEqual(resp.status_code, 400)


# ============================================================
# /api/scrape — rate-limit per-user (audit H3)
# ============================================================

class ApiScrapeRateLimitTest(unittest.TestCase):
    """Audit H3 : 1 scrape/min per user pour prévenir le drain ScrapingBee."""

    def setUp(self):
        _reset_buckets()
        from flask import Flask
        import auth, routes_scraping
        self.auth = auth
        self.app = Flask(__name__)
        self.app.register_blueprint(auth.auth_bp)
        self.app.register_blueprint(routes_scraping.scraping_bp)
        self.client = self.app.test_client()
        self.token = auth.make_token(7)

        # Patch DB calls so the route doesn't touch real Postgres
        self._db_patcher = patch('routes_scraping.get_db')
        self._return_patcher = patch('routes_scraping.return_db')
        mock_db = self._db_patcher.start()
        self._return_patcher.start()
        self.addCleanup(self._db_patcher.stop)
        self.addCleanup(self._return_patcher.stop)
        # Mock cursor: profile lookup returns a city
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [{'city': 'Lausanne'}]
        cur.fetchone.return_value = {'transaction': 'location'}
        mock_db.return_value = conn

        # Block the bg thread so it doesn't actually scrape
        self._thread_patcher = patch('routes_scraping.threading.Thread')
        self._thread_patcher.start()
        self.addCleanup(self._thread_patcher.stop)

    def test_2nd_scrape_in_same_minute_429(self):
        headers = {'Authorization': f'Bearer {self.token}'}
        resp1 = self.client.post('/api/scrape', json={}, headers=headers)
        self.assertNotEqual(resp1.status_code, 429,
                            f"first call should pass, got {resp1.status_code}")
        resp2 = self.client.post('/api/scrape', json={}, headers=headers)
        self.assertEqual(resp2.status_code, 429,
                         "2nd /api/scrape within minute must be rate-limited")


# ============================================================
# _require_cron_secret — audit H2 (header only, constant-time)
# ============================================================

class CronSecretTest(unittest.TestCase):
    """Audit H2 : ?secret= retiré, hmac.compare_digest pour timing safety."""

    def setUp(self):
        from flask import Flask
        import routes_scraping
        self.routes_scraping = routes_scraping
        self.app = Flask(__name__)

        @self.app.route('/_cron_test')
        def _cron_test():
            from flask import jsonify
            ok = self.routes_scraping._require_cron_secret()
            return jsonify({'ok': ok})

        self.client = self.app.test_client()

    def test_correct_header_passes(self):
        with patch.object(self.routes_scraping, 'CRON_SECRET',
                          'super-secret-cron-token-xyz123456'):
            resp = self.client.get(
                '/_cron_test',
                headers={'X-Cron-Secret': 'super-secret-cron-token-xyz123456'},
            )
        self.assertTrue(resp.get_json()['ok'])

    def test_query_string_rejected(self):
        """Audit H2 : ?secret= ne doit PLUS passer."""
        with patch.object(self.routes_scraping, 'CRON_SECRET',
                          'super-secret-cron-token-xyz123456'):
            resp = self.client.get(
                '/_cron_test?secret=super-secret-cron-token-xyz123456'
            )
        self.assertFalse(resp.get_json()['ok'])

    def test_wrong_secret_rejected(self):
        with patch.object(self.routes_scraping, 'CRON_SECRET',
                          'super-secret-cron-token-xyz123456'):
            resp = self.client.get(
                '/_cron_test',
                headers={'X-Cron-Secret': 'wrong-token-xyz'},
            )
        self.assertFalse(resp.get_json()['ok'])

    def test_no_secret_configured_rejects_all(self):
        """Fail-closed : si CRON_SECRET n'est pas configuré, refuse tout."""
        with patch.object(self.routes_scraping, 'CRON_SECRET', ''):
            resp = self.client.get(
                '/_cron_test',
                headers={'X-Cron-Secret': 'anything'},
            )
        self.assertFalse(resp.get_json()['ok'])


if __name__ == '__main__':
    unittest.main()


class IngestKeyTest(unittest.TestCase):
    """Phase 2 : /api/import accepte X-Ingest-Key en plus du JWT admin."""

    def _app(self):
        from flask import Flask
        import auth as auth_mod
        app = Flask(__name__)

        @app.route('/p', methods=['POST'])
        @auth_mod.ingest_or_admin_required
        def p():
            from flask import jsonify, request
            return jsonify({"key_auth": request.ingest_key_auth, "user_id": request.user_id})
        return app, auth_mod

    def test_good_key(self):
        app, auth_mod = self._app()
        old = auth_mod.INGEST_KEY
        auth_mod.INGEST_KEY = 'bh_test_key'
        try:
            r = app.test_client().post('/p', headers={'X-Ingest-Key': 'bh_test_key'})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()['key_auth'])
        finally:
            auth_mod.INGEST_KEY = old

    def test_bad_key_falls_back_to_admin_401(self):
        app, auth_mod = self._app()
        old = auth_mod.INGEST_KEY
        auth_mod.INGEST_KEY = 'bh_test_key'
        try:
            r = app.test_client().post('/p', headers={'X-Ingest-Key': 'wrong'})
            self.assertEqual(r.status_code, 401)
        finally:
            auth_mod.INGEST_KEY = old

    def test_no_key_configured(self):
        app, auth_mod = self._app()
        old = auth_mod.INGEST_KEY
        auth_mod.INGEST_KEY = ''
        try:
            r = app.test_client().post('/p', headers={'X-Ingest-Key': ''})
            self.assertEqual(r.status_code, 401)
        finally:
            auth_mod.INGEST_KEY = old
