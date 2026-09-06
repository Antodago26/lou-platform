"""
Tests sur les blindages sécurité d'auth.py shippés en 2026-05.

Lancer : cd backend-v2 && python3 -m unittest tests.test_auth_security -v

Couvre :
  - _password_too_long : guard bcrypt 72-byte truncation oracle
  - make_token + _decode_jwt_or_401 : round-trip JWT
  - token_required : header OK, query string REJETÉE (audit H1)
  - token_required_query_ok : header ET query string acceptés
  - admin_required : passe sur ADMIN_EMAIL, 403 sinon
  - Rate-limit /api/login : per-IP cap, per-email cap (audit C2)
  - Rate-limit /api/signup : per-IP cap (audit C2)
  - /api/login : 401 sur bad password (no oracle), generic message
  - /api/login : 401 sur password > 72 bytes (audit bcrypt 72-byte fix)
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set required env vars BEFORE importing auth (which reads at import time).
os.environ.setdefault('JWT_SECRET', 'test-secret-must-be-32-bytes-of-stuff')
os.environ.setdefault('DATABASE_URL', 'postgresql://test')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reset_buckets():
    """Wipe rate-limit state between tests."""
    import rate_limit
    rate_limit._buckets_min.clear()
    rate_limit._buckets_hour.clear()


# ============================================================
# _password_too_long
# ============================================================

class PasswordTooLongTest(unittest.TestCase):
    """Guard against bcrypt's 72-byte silent truncation oracle.

    Without this check, two passwords sharing the first 72 UTF-8 bytes
    would hash identically — anyone knowing a 72-byte prefix could log in.
    """

    def setUp(self):
        from auth import _password_too_long, MAX_PASSWORD_BYTES
        self.fn = _password_too_long
        self.cap = MAX_PASSWORD_BYTES

    def test_short_password_ok(self):
        self.assertFalse(self.fn('short'))

    def test_72_bytes_ascii_ok(self):
        self.assertFalse(self.fn('a' * 72))

    def test_73_bytes_ascii_too_long(self):
        self.assertTrue(self.fn('a' * 73))

    def test_emoji_counted_in_utf8_bytes(self):
        """Each '🔥' = 4 UTF-8 bytes. 18 emojis = 72 bytes (OK), 19 = 76 (too long)."""
        self.assertFalse(self.fn('🔥' * 18))
        self.assertTrue(self.fn('🔥' * 19))

    def test_none_treated_as_too_long(self):
        """Defensive: a None password should fail the guard, not crash the route."""
        # encode() of an empty str returns b'' (length 0) → False; None → exception → True
        self.assertTrue(self.fn(None))


# ============================================================
# JWT round-trip
# ============================================================

class JwtTest(unittest.TestCase):
    def test_make_token_then_decode_returns_user_id(self):
        from auth import make_token, _decode_jwt_or_401
        token = make_token(42)
        user_id, err = _decode_jwt_or_401(token)
        self.assertEqual(user_id, 42)
        self.assertIsNone(err)

    def _ctx(self):
        # jsonify a besoin d'un contexte d'application : une app minimale suffit.
        from flask import Flask
        return Flask(__name__).app_context()

    def test_decode_empty_token_401(self):
        from auth import _decode_jwt_or_401
        with self._ctx():
            user_id, err = _decode_jwt_or_401('')
        self.assertIsNone(user_id)
        self.assertIsNotNone(err)
        body, status = err
        self.assertEqual(status, 401)

    def test_decode_invalid_token_401(self):
        from auth import _decode_jwt_or_401
        with self._ctx():
            user_id, err = _decode_jwt_or_401('not.a.real.jwt')
        self.assertIsNone(user_id)
        body, status = err
        self.assertEqual(status, 401)


# ============================================================
# token_required / token_required_query_ok decorators
# ============================================================

class _DecoratorTestBase(unittest.TestCase):
    def _make_app(self, decorator):
        from flask import Flask, jsonify, request
        app = Flask(__name__)

        @app.route('/_protected')
        @decorator
        def _protected():
            return jsonify({'user_id': request.user_id})

        return app, app.test_client()


class TokenRequiredTest(_DecoratorTestBase):
    """Audit H1 (2026-05) : header-only, query string rejected."""

    def setUp(self):
        from auth import token_required, make_token
        self.app, self.client = self._make_app(token_required)
        self.token = make_token(42)

    def test_header_passes(self):
        resp = self.client.get('/_protected', headers={'Authorization': f'Bearer {self.token}'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['user_id'], 42)

    def test_query_string_rejected(self):
        """Audit H1 : ?token= NE doit PAS authentifier sur token_required."""
        resp = self.client.get(f'/_protected?token={self.token}')
        self.assertEqual(resp.status_code, 401)

    def test_no_token_401(self):
        resp = self.client.get('/_protected')
        self.assertEqual(resp.status_code, 401)


class TokenRequiredQueryOkTest(_DecoratorTestBase):
    """Réservé aux téléchargements (window.open) — accepte les 2."""

    def setUp(self):
        from auth import token_required_query_ok, make_token
        self.app, self.client = self._make_app(token_required_query_ok)
        self.token = make_token(42)

    def test_header_passes(self):
        resp = self.client.get('/_protected', headers={'Authorization': f'Bearer {self.token}'})
        self.assertEqual(resp.status_code, 200)

    def test_query_string_passes(self):
        resp = self.client.get(f'/_protected?token={self.token}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['user_id'], 42)

    def test_no_token_401(self):
        resp = self.client.get('/_protected')
        self.assertEqual(resp.status_code, 401)


class AdminRequiredTest(_DecoratorTestBase):
    """admin_required : passe sur ADMIN_EMAIL, 403 sinon."""

    def setUp(self):
        # Patch ADMIN_EMAIL at module level for the duration of the test
        import auth
        self._patcher = patch.object(auth, 'ADMIN_EMAIL', 'admin@bonhome.ch')
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        from auth import admin_required, make_token
        self.app, self.client = self._make_app(admin_required)
        self.token = make_token(42)
        # Mock get_db so the email lookup returns a controllable email
        self._db_patcher = patch('auth.get_db')
        self._return_db_patcher = patch('auth.return_db')
        self.mock_get_db = self._db_patcher.start()
        self._return_db_patcher.start()
        self.addCleanup(self._db_patcher.stop)
        self.addCleanup(self._return_db_patcher.stop)

    def _set_db_email(self, email):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = {'email': email}
        self.mock_get_db.return_value = conn

    def test_admin_email_passes(self):
        self._set_db_email('admin@bonhome.ch')
        resp = self.client.get('/_protected', headers={'Authorization': f'Bearer {self.token}'})
        self.assertEqual(resp.status_code, 200)

    def test_non_admin_403(self):
        self._set_db_email('alice@example.com')
        resp = self.client.get('/_protected', headers={'Authorization': f'Bearer {self.token}'})
        self.assertEqual(resp.status_code, 403)

    def test_no_token_401(self):
        resp = self.client.get('/_protected')
        self.assertEqual(resp.status_code, 401)


# ============================================================
# Rate-limit : /api/signup (per-IP)
# ============================================================

class SignupRateLimitTest(unittest.TestCase):
    """Audit C2 : 3/min per-IP sur /api/signup."""

    def setUp(self):
        _reset_buckets()
        from flask import Flask
        import auth
        self.auth = auth
        self.app = Flask(__name__)
        self.app.register_blueprint(auth.auth_bp)
        self.client = self.app.test_client()

    def test_4th_signup_from_same_ip_429(self):
        """3 calls passent (la validation va échouer à 400 mais ce n'est pas
        429), le 4e est rate-limited avant même la validation."""
        # Force the same client IP for all calls
        for i in range(3):
            resp = self.client.post(
                '/api/signup', json={},
                environ_base={'REMOTE_ADDR': '1.2.3.4'},
            )
            # 400 (validation fail on empty body) ou autre — surtout PAS 429
            self.assertNotEqual(resp.status_code, 429,
                                f"call #{i+1} should not be rate-limited yet")
        # 4th call from same IP → 429
        resp = self.client.post(
            '/api/signup', json={},
            environ_base={'REMOTE_ADDR': '1.2.3.4'},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertIn('Retry-After', resp.headers)

    def test_signup_throttle_isolated_per_ip(self):
        for _ in range(3):
            self.client.post('/api/signup', json={},
                             environ_base={'REMOTE_ADDR': '1.2.3.4'})
        # Different IP → fresh bucket
        resp = self.client.post('/api/signup', json={},
                                environ_base={'REMOTE_ADDR': '5.6.7.8'})
        self.assertNotEqual(resp.status_code, 429)


# ============================================================
# Rate-limit : /api/login (per-IP + per-email)
# ============================================================

class LoginRateLimitTest(unittest.TestCase):
    """Audit C2 : 5/min per-IP ET 5/min per-email sur /api/login."""

    def setUp(self):
        _reset_buckets()
        from flask import Flask
        import auth
        self.auth = auth
        self.app = Flask(__name__)
        self.app.register_blueprint(auth.auth_bp)
        self.client = self.app.test_client()
        # Mock DB so login attempts don't hit a real Postgres
        self._db_patcher = patch('auth.get_db')
        self._return_db_patcher = patch('auth.return_db')
        self.mock_get_db = self._db_patcher.start()
        self._return_db_patcher.start()
        self.addCleanup(self._db_patcher.stop)
        self.addCleanup(self._return_db_patcher.stop)
        # Default DB response: user not found → 401, not 500
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = None
        self.mock_get_db.return_value = conn

    def _login(self, ip, email='alice@example.com', password='wrong'):
        return self.client.post(
            '/api/login',
            json={'email': email, 'password': password},
            environ_base={'REMOTE_ADDR': ip},
        )

    def test_6th_call_same_ip_429(self):
        for i in range(5):
            resp = self._login('10.0.0.1')
            self.assertEqual(resp.status_code, 401, f"call {i+1} should be 401 (bad creds)")
        # 6th from same IP → 429
        resp = self._login('10.0.0.1')
        self.assertEqual(resp.status_code, 429)

    def test_per_email_throttle_blocks_rotating_ips(self):
        """Même email, IPs différentes → toujours bloqué après 5 tentatives.
        Couvre l'attaque "credential stuffing distribué sur un compte cible"."""
        for i in range(5):
            resp = self._login(f'10.0.0.{i+1}', email='target@victim.com')
            self.assertEqual(resp.status_code, 401)
        # 6th attempt with a fresh IP → still 429 because of per-email cap
        resp = self._login('99.99.99.99', email='target@victim.com')
        self.assertEqual(resp.status_code, 429)


# ============================================================
# /api/login : security responses
# ============================================================

class LoginSecurityTest(unittest.TestCase):

    def setUp(self):
        _reset_buckets()
        from flask import Flask
        import auth
        self.auth = auth
        self.app = Flask(__name__)
        self.app.register_blueprint(auth.auth_bp)
        self.client = self.app.test_client()
        self._db_patcher = patch('auth.get_db')
        self._return_db_patcher = patch('auth.return_db')
        self.mock_get_db = self._db_patcher.start()
        self._return_db_patcher.start()
        self.addCleanup(self._db_patcher.stop)
        self.addCleanup(self._return_db_patcher.stop)

    def test_password_over_72_bytes_rejected(self):
        """bcrypt truncation guard : un password > 72 bytes doit retourner
        l'erreur générique sans même appeler bcrypt.checkpw."""
        # User exists in DB
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = {
            'id': 1, 'email': 'alice@example.com',
            'password_hash': '$2b$12$' + 'a' * 53,  # bogus hash, never reached
        }
        self.mock_get_db.return_value = conn

        resp = self.client.post(
            '/api/login',
            json={'email': 'alice@example.com', 'password': 'a' * 100},
            environ_base={'REMOTE_ADDR': '10.0.0.10'},
        )
        self.assertEqual(resp.status_code, 401)
        # Generic message — no oracle on which check failed
        self.assertEqual(resp.get_json()['error'], 'Identifiants incorrects')

    def test_unknown_email_returns_401_not_404(self):
        """No user-exists oracle : un email inconnu retourne le même 401 +
        message générique qu'un mauvais password."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = None  # user not found
        self.mock_get_db.return_value = conn

        resp = self.client.post(
            '/api/login',
            json={'email': 'ghost@nowhere.com', 'password': 'whatever'},
            environ_base={'REMOTE_ADDR': '10.0.0.20'},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], 'Identifiants incorrects')


if __name__ == '__main__':
    unittest.main()
