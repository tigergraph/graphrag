"""Unit tests for common.db.connections.elevate_db_connection_to_token.

Since 1.4.1 a per-user connection authenticates as the signed-in user. The
configured ``apiToken`` is for the service's own connection only (see
``common/config.py`` and the README), so it must not reach a user's
connection.
"""

import unittest
from unittest.mock import patch

try:
    from pyTigerGraph import AsyncTigerGraphConnection, TigerGraphConnection
    import common.db.connections as connections
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


DB_CONFIG = {
    "hostname": "http://test-host",
    "restppPort": "9000",
    "gsPort": "14240",
    "getToken": True,
    "apiToken": "service_token",
    "default_timeout": 300,
}


class TestElevateDbConnection(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(connections.db_config, DB_CONFIG, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_user_connection_uses_the_user_credentials(self):
        conn = connections.elevate_db_connection_to_token("http://test-host", "alice", "pw", "TestGraph")
        self.assertIsInstance(conn, TigerGraphConnection)
        self.assertEqual((conn.username, conn.password), ("alice", "pw"))
        self.assertEqual((conn.host, conn.graphname), ("http://test-host", "TestGraph"))

    def test_service_token_is_not_used_for_a_user(self):
        sync_conn = connections.elevate_db_connection_to_token("http://test-host", "alice", "pw", "G")
        async_conn = connections.elevate_db_connection_to_token("http://test-host", "alice", "pw", "G",
                                                               async_conn=True)
        self.assertIsInstance(async_conn, AsyncTigerGraphConnection)
        for conn in (sync_conn, async_conn):
            self.assertNotEqual(conn.apiToken, "service_token")
            self.assertEqual(conn.username, "alice")

    def test_no_token_is_requested_up_front(self):
        """pyTigerGraph decides when a REST++ token is needed."""
        with patch.object(TigerGraphConnection, "getToken") as get_token:
            connections.elevate_db_connection_to_token("http://test-host", "alice", "pw", "G")
        get_token.assert_not_called()

    def test_ports_come_from_the_config(self):
        connections.db_config.update({"restppPort": "19000", "gsPort": "24240"})
        conn = connections.elevate_db_connection_to_token("http://test-host", "alice", "pw", "G")
        self.assertIn("19000", str(conn.restppPort))
        self.assertIn("24240", str(conn.gsPort))


if __name__ == "__main__":
    unittest.main()
