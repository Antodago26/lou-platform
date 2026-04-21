"""
Tests ciblés pour db.return_db — fix v6.3.4 P0 SSL-poisoning.

Vérifie que les conns cassées (SSL bad record mac, rollback qui lève)
SORTENT du pool avec close=True, pour ne pas empoisonner le prochain
thread qui appelle get_db().

Lancer : cd backend-v2 && python -m pytest tests/test_return_db.py -v
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# On n'a pas besoin d'une vraie DB, mais db.py importe psycopg2 au module-level.
# DATABASE_URL peut rester vide — _init_pool n'est appelé qu'à la 1re get_db().
os.environ.setdefault('DATABASE_URL', '')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_module  # noqa: E402


def _fake_conn(closed=0, status=0, rollback_side_effect=None):
    """Conn psycopg2-like. status=0 → STATUS_READY ; =2 → STATUS_IN_TRANSACTION."""
    conn = MagicMock()
    conn.closed = closed
    conn.status = status
    if rollback_side_effect is not None:
        conn.rollback.side_effect = rollback_side_effect
    return conn


class ReturnDbTest(unittest.TestCase):
    def setUp(self):
        # Chaque test part avec un pool factice qu'on contrôle.
        self._pool = MagicMock()
        self._pool_patch = patch.object(db_module, '_db_pool', self._pool)
        self._pool_patch.start()

    def tearDown(self):
        self._pool_patch.stop()

    def test_healthy_conn_returns_to_pool_without_close(self):
        """Cas nominal : conn saine → rollback OK → putconn(close=False)."""
        conn = _fake_conn()
        db_module.return_db(conn)
        conn.rollback.assert_called_once()
        self._pool.putconn.assert_called_once_with(conn, close=False)

    def test_explicit_close_forces_putconn_close_true(self):
        """Caller sait que la conn est cassée → close=True explicite → pool la détruit."""
        conn = _fake_conn()
        db_module.return_db(conn, close=True)
        # Avec close=True explicite, on NE rollback PAS (conn supposée cassée)
        conn.rollback.assert_not_called()
        self._pool.putconn.assert_called_once_with(conn, close=True)

    def test_rollback_raises_triggers_auto_close(self):
        """CAS RÉEL (SSL bad record mac) : rollback lève → auto-close=True,
        même sans hint explicite du caller. Sans ce comportement, la conn
        pourrie re-rentre dans le pool et empoisonne le prochain thread."""
        import psycopg2
        conn = _fake_conn(
            rollback_side_effect=psycopg2.OperationalError(
                "SSL error: decryption failed or bad record mac"
            )
        )
        db_module.return_db(conn)  # pas de close=True explicite
        conn.rollback.assert_called_once()
        self._pool.putconn.assert_called_once_with(conn, close=True)

    def test_closed_conn_forces_close_true(self):
        """conn.closed != 0 → putconn(close=True). On ne rollback pas une
        conn déjà fermée (ça lève 'connection already closed')."""
        conn = _fake_conn(closed=1)
        db_module.return_db(conn)
        conn.rollback.assert_not_called()
        self._pool.putconn.assert_called_once_with(conn, close=True)

    def test_none_conn_is_noop(self):
        db_module.return_db(None)
        self._pool.putconn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
