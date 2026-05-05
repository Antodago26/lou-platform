"""
Tests pour rate_limit.py (extracted 2026-05 from routes_chat.py).

Lancer : cd backend-v2 && python3 -m unittest tests.test_rate_limit -v

Couvre :
  - check_rate_limit : True jusqu'à la cap, False ensuite
  - Buckets minute + hour : la cap horaire bloque même si la minute est OK
  - Expiration : un token vieux > 60s libère le slot minute
  - client_ip : X-Forwarded-First first hop, fallback remote_addr
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CheckRateLimitTest(unittest.TestCase):
    """Pure-logic tests on the in-memory bucket."""

    def setUp(self):
        # Reset module-level state between tests for isolation
        import rate_limit
        rate_limit._buckets_min.clear()
        rate_limit._buckets_hour.clear()
        self.rate_limit = rate_limit

    def test_allows_until_minute_cap(self):
        """5 calls allowed when per_min=5, 6th rejected."""
        for i in range(5):
            self.assertTrue(
                self.rate_limit.check_rate_limit('user:1', 5, 100),
                f"call #{i+1} should pass"
            )
        self.assertFalse(
            self.rate_limit.check_rate_limit('user:1', 5, 100),
            "6th call must be rate-limited"
        )

    def test_hour_cap_blocks_when_minute_ok(self):
        """If per_hour=3, even spaced requests get blocked at the 4th."""
        # Force timestamps: spread 3 successful calls 30s apart so minute bucket
        # has 0-1 entries (under the 100 cap), but hour bucket fills up.
        with patch('rate_limit.time.time') as mock_time:
            mock_time.return_value = 1000.0
            self.assertTrue(self.rate_limit.check_rate_limit('user:2', 100, 3))
            mock_time.return_value = 1030.0
            self.assertTrue(self.rate_limit.check_rate_limit('user:2', 100, 3))
            mock_time.return_value = 1060.0
            self.assertTrue(self.rate_limit.check_rate_limit('user:2', 100, 3))
            mock_time.return_value = 1090.0
            self.assertFalse(
                self.rate_limit.check_rate_limit('user:2', 100, 3),
                "4th call must be blocked by hour bucket even with minute=100 cap"
            )

    def test_old_minute_entries_get_evicted(self):
        """A token > 60s old frees a minute slot."""
        with patch('rate_limit.time.time') as mock_time:
            mock_time.return_value = 1000.0
            for _ in range(5):
                self.rate_limit.check_rate_limit('user:3', 5, 100)

            # 61s later → all minute entries should be evicted
            mock_time.return_value = 1061.0
            self.assertTrue(
                self.rate_limit.check_rate_limit('user:3', 5, 100),
                "After 61s, the minute bucket should have room again"
            )

    def test_independent_keys_dont_collide(self):
        """user:1 hitting cap doesn't block user:2."""
        for _ in range(5):
            self.rate_limit.check_rate_limit('user:1', 5, 100)
        self.assertFalse(self.rate_limit.check_rate_limit('user:1', 5, 100))
        # Different key → fresh bucket
        self.assertTrue(self.rate_limit.check_rate_limit('user:2', 5, 100))

    def test_failed_call_does_not_consume_slot(self):
        """When check returns False, no token is recorded — caller can keep
        trying without making the situation worse."""
        for _ in range(5):
            self.rate_limit.check_rate_limit('user:4', 5, 100)
        # Bucket is full
        before_min = len(self.rate_limit._buckets_min['user:4'])
        before_hour = len(self.rate_limit._buckets_hour['user:4'])
        # 10 rejected calls
        for _ in range(10):
            self.rate_limit.check_rate_limit('user:4', 5, 100)
        # Counts unchanged
        self.assertEqual(len(self.rate_limit._buckets_min['user:4']), before_min)
        self.assertEqual(len(self.rate_limit._buckets_hour['user:4']), before_hour)


class ClientIpTest(unittest.TestCase):
    """X-Forwarded-For parsing + remote_addr fallback."""

    def setUp(self):
        from flask import Flask
        from rate_limit import client_ip
        self.client_ip = client_ip
        self.app = Flask(__name__)

        @self.app.route('/_test')
        def _test():
            return self.client_ip()

        self.client = self.app.test_client()

    def test_first_xff_hop_returned(self):
        """X-Forwarded-For: 1.2.3.4, 5.6.7.8 → 1.2.3.4 (the real client)."""
        with self.app.test_request_context(
            '/_test', headers={'X-Forwarded-For': '1.2.3.4, 5.6.7.8, 9.0.1.2'}
        ):
            self.assertEqual(self.client_ip(), '1.2.3.4')

    def test_xff_strips_whitespace(self):
        with self.app.test_request_context(
            '/_test', headers={'X-Forwarded-For': '  1.2.3.4 ,  5.6.7.8'}
        ):
            self.assertEqual(self.client_ip(), '1.2.3.4')

    def test_falls_back_to_remote_addr(self):
        with self.app.test_request_context(
            '/_test', environ_base={'REMOTE_ADDR': '127.0.0.1'}
        ):
            self.assertEqual(self.client_ip(), '127.0.0.1')

    def test_unknown_when_nothing(self):
        with self.app.test_request_context('/_test', environ_base={'REMOTE_ADDR': ''}):
            self.assertEqual(self.client_ip(), 'unknown')


class RateLimitedResponseTest(unittest.TestCase):
    """Response shape sanity check."""

    def test_returns_429_with_retry_after(self):
        from flask import Flask
        from rate_limit import rate_limited_response
        app = Flask(__name__)
        with app.test_request_context('/'):
            body, status, headers = rate_limited_response(retry_after_seconds=42)
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '42')
        self.assertIn('error', body.get_json())


if __name__ == '__main__':
    unittest.main()
