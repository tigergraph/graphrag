"""Unit tests for common.db.connections apiToken support."""

import sys
import unittest
from unittest.mock import patch, MagicMock

# Mock heavy dependencies before importing the module under test
_mock_modules = {}
for mod_name in [
    "common.config",
    "common.logs",
    "common.logs.logwriter",
    "common.logs.log",
    "common.metrics",
    "common.metrics.tg_proxy",
    "common.metrics.prometheus_metrics",
    "common.embeddings",
    "common.embeddings.embedding_services",
    "common.embeddings.tigergraph_embedding_store",
    "common.llm_services",
    "common.session",
    "common.status",
    "langchain_core",
    "langchain_core.embeddings",
    "prometheus_client",
]:
    if mod_name not in sys.modules:
        _mock_modules[mod_name] = MagicMock()
        sys.modules[mod_name] = _mock_modules[mod_name]

# Provide the values that connections.py reads at import time
sys.modules["common.config"].security = MagicMock()

# Provide TigerGraphConnectionProxy
mock_proxy_cls = MagicMock()
sys.modules["common.metrics.tg_proxy"].TigerGraphConnectionProxy = mock_proxy_cls

# Provide LogWriter
mock_logwriter = MagicMock()
sys.modules["common.logs.logwriter"].LogWriter = mock_logwriter

from pyTigerGraph import TigerGraphConnection, AsyncTigerGraphConnection


MOCK_DB_CONFIG_BASE = {
    "hostname": "http://test-host",
    "restppPort": "9000",
    "gsPort": "14240",
    "getToken": False,
    "default_timeout": 300,
}


class TestElevateDbConnectionWithApiToken(unittest.TestCase):
    """Test that elevate_db_connection_to_token honours apiToken from db_config."""

    def _import_and_patch(self, db_config_override):
        """Import elevate_db_connection_to_token with a patched db_config."""
        sys.modules["common.config"].db_config = db_config_override
        # Re-import to pick up fresh db_config reference
        import importlib
        import common.db.connections as conn_mod
        importlib.reload(conn_mod)
        return conn_mod.elevate_db_connection_to_token

    def test_static_api_token_used_directly(self):
        cfg = {**MOCK_DB_CONFIG_BASE, "apiToken": "static_tok_123"}
        elevate = self._import_and_patch(cfg)

        conn = elevate("http://test-host", "user", "pass", "TestGraph")

        self.assertEqual(conn.apiToken, "static_tok_123")
        self.assertEqual(conn.host, "http://test-host")
        self.assertEqual(conn.graphname, "TestGraph")

    def test_api_token_skips_get_token(self):
        cfg = {**MOCK_DB_CONFIG_BASE, "apiToken": "tok", "getToken": True}
        elevate = self._import_and_patch(cfg)

        with patch.object(TigerGraphConnection, "getToken") as mock_get:
            conn = elevate("http://test-host", "user", "pass", "TestGraph")
            mock_get.assert_not_called()

        self.assertEqual(conn.apiToken, "tok")

    def test_static_api_token_async_conn(self):
        cfg = {**MOCK_DB_CONFIG_BASE, "apiToken": "async_tok"}
        elevate = self._import_and_patch(cfg)

        conn = elevate("http://test-host", "user", "pass", "TestGraph", async_conn=True)

        self.assertIsInstance(conn, AsyncTigerGraphConnection)
        self.assertEqual(conn.apiToken, "async_tok")

    def test_empty_api_token_falls_through(self):
        cfg = {**MOCK_DB_CONFIG_BASE, "apiToken": ""}
        elevate = self._import_and_patch(cfg)

        conn = elevate("http://test-host", "user", "pass", "TestGraph")

        # Empty token treated as not set — password auth
        self.assertIsInstance(conn, TigerGraphConnection)
        self.assertEqual(conn.username, "user")

    def test_no_api_token_key_falls_through(self):
        cfg = {**MOCK_DB_CONFIG_BASE}
        elevate = self._import_and_patch(cfg)

        conn = elevate("http://test-host", "user", "pass", "TestGraph")

        self.assertIsInstance(conn, TigerGraphConnection)
        self.assertEqual(conn.username, "user")


if __name__ == "__main__":
    unittest.main()
