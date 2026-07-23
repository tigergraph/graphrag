# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import os
import unittest

import pytest
import requests


# Base URL of the running GraphRAG service. Override via env var when running
# against a non-default host/port (e.g. GRAPHRAG_URL=http://localhost:8000).
GRAPHRAG_URL = os.getenv("GRAPHRAG_URL", "http://localhost:80")

# Credentials loaded from DB_CONFIG (same pattern as test_supportai.py).
_db_config_path = os.getenv("DB_CONFIG", "./configs/db_config.json")
try:
    with open(_db_config_path) as _f:
        _db = json.load(_f)
    SUPERUSER = _db.get("username", "tigergraph")
    SUPERUSER_PASSWORD = _db.get("password", "tigergraph")
except Exception:
    SUPERUSER = os.getenv("TG_USERNAME", "tigergraph")
    SUPERUSER_PASSWORD = os.getenv("TG_PASSWORD", "tigergraph")

# A graph-admin user that exists on the running TigerGraph instance.
# Override via env vars when your test graph/user differ.
GRAPH_ADMIN_USER = os.getenv("GRAPH_ADMIN_USER", SUPERUSER)
GRAPH_ADMIN_PASSWORD = os.getenv("GRAPH_ADMIN_PASSWORD", SUPERUSER_PASSWORD)
GRAPH_ADMIN_GRAPH = os.getenv("GRAPH_ADMIN_GRAPH", "MyGraph")


@pytest.mark.skipif(not os.getenv("GRAPHRAG_URL"), reason="Integration tests require a live GraphRAG service. Set GRAPHRAG_URL to run.")
class TestUISetupIntegration(unittest.TestCase):
    """
    Integration tests for /ui/config and /ui/config/db/test.
    These hit the actual running GraphRAG service over HTTP and require:
      - A live TigerGraph + GraphRAG stack (docker-compose up)
      - GRAPHRAG_URL pointing at the service (default: http://localhost:80)
      - DB_CONFIG or TG_USERNAME / TG_PASSWORD env vars with valid superuser creds

    Run from outside Docker:
        GRAPHRAG_URL=http://localhost:80 python -m pytest tests/test_ui_setup_integration.py -v

    Test map
    --------
      1. test_superuser_can_save_and_retrieve_llm_config
      2. test_graph_admin_chatbot_only_access
      3. test_db_connection_valid_and_invalid_creds
      4. test_graph_admin_chat_service_save_and_inherit
    """

    # =========================================================================
    # Test 1 – Superuser saves full LLM config and the values persist
    # =========================================================================

    def test_superuser_can_save_and_retrieve_llm_config(self):
        """POST a new completion model name, then GET /ui/config and verify it persisted."""
        openai_key = os.getenv("OPENAI_API_KEY", "sk-test")
        payload = {
            "completion_service": {
                "llm_service": "openai",
                "llm_model": "gpt-4o-mini",
                "model_kwargs": {"temperature": 0},
                "authentication_configuration": {"OPENAI_API_KEY": openai_key},
            },
            "embedding_service": {
                "embedding_model_service": "openai",
                "model_name": "text-embedding-3-small",
                "authentication_configuration": {"OPENAI_API_KEY": openai_key},
            },
        }
        post_resp = requests.post(
            f"{GRAPHRAG_URL}/ui/config/llm",
            json=payload,
            auth=(SUPERUSER, SUPERUSER_PASSWORD),
        )
        self.assertEqual(post_resp.status_code, 200, post_resp.text)
        self.assertEqual(post_resp.json().get("status"), "success")

        get_resp = requests.get(
            f"{GRAPHRAG_URL}/ui/config",
            auth=(SUPERUSER, SUPERUSER_PASSWORD),
        )
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        completion = get_resp.json().get("llm_config", {}).get("completion_service", {})
        self.assertEqual(completion.get("llm_model"), "gpt-4o-mini")

    # =========================================================================
    # Test 2 – Graph admin: 403 on full config, success on chat_service
    # =========================================================================

    def test_graph_admin_chatbot_only_access(self):
        """Graph admin cannot POST a full LLM config (403) but can POST chat_service."""
        if GRAPH_ADMIN_USER == SUPERUSER:
            self.skipTest(
                "GRAPH_ADMIN_USER is the same as SUPERUSER — set GRAPH_ADMIN_USER "
                "and GRAPH_ADMIN_PASSWORD env vars to a graph-level admin to run this test."
            )
        full_payload = {
            "completion_service": {
                "llm_service": "openai",
                "llm_model": "gpt-4o",
                "authentication_configuration": {"OPENAI_API_KEY": "sk-test"},
            }
        }
        resp_full = requests.post(
            f"{GRAPHRAG_URL}/ui/config/llm",
            json=full_payload,
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_full.status_code, 403, resp_full.text)

        chatbot_payload = {
            "graphname": GRAPH_ADMIN_GRAPH,
            "chat_service": {
                "llm_model": "gpt-4o-mini",
            },
        }
        resp_chatbot = requests.post(
            f"{GRAPHRAG_URL}/ui/config/llm",
            json=chatbot_payload,
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_chatbot.status_code, 200, resp_chatbot.text)
        self.assertEqual(resp_chatbot.json().get("status"), "success")

    # =========================================================================
    # Test 4 – Graph admin: save custom chat_service and revert to inherit
    # =========================================================================

    def test_graph_admin_chat_service_save_and_inherit(self):
        """Graph admin saves a custom chat_service, verifies it in GET, then reverts to inherit."""
        if GRAPH_ADMIN_USER == SUPERUSER:
            self.skipTest(
                "GRAPH_ADMIN_USER is the same as SUPERUSER — set GRAPH_ADMIN_USER "
                "and GRAPH_ADMIN_PASSWORD env vars to a graph-level admin to run this test."
            )
        # Save custom chat_service
        save_payload = {
            "graphname": GRAPH_ADMIN_GRAPH,
            "chat_service": {
                "llm_service": "openai",
                "llm_model": "gpt-4o-mini",
                "model_kwargs": {"temperature": 0.5},
            },
        }
        resp_save = requests.post(
            f"{GRAPHRAG_URL}/ui/config/llm",
            json=save_payload,
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_save.status_code, 200, resp_save.text)

        # GET should return the custom config (without auth secrets)
        resp_get = requests.get(
            f"{GRAPHRAG_URL}/ui/config?graphname={GRAPH_ADMIN_GRAPH}",
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_get.status_code, 200, resp_get.text)
        data = resp_get.json()
        self.assertEqual(data.get("llm_config_access"), "chatbot_only")
        self.assertIsNotNone(data.get("chatbot_config"), "chatbot_config should be present after save")
        self.assertEqual(data["chatbot_config"].get("llm_model"), "gpt-4o-mini")
        self.assertNotIn("authentication_configuration", data.get("chatbot_config", {}))
        self.assertIn("global_chat_info", data)

        # Revert to inherit by sending null chat_service
        revert_payload = {
            "graphname": GRAPH_ADMIN_GRAPH,
            "chat_service": None,
        }
        resp_revert = requests.post(
            f"{GRAPHRAG_URL}/ui/config/llm",
            json=revert_payload,
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_revert.status_code, 200, resp_revert.text)

        # GET should now show chatbot_config as None (inheriting)
        resp_get2 = requests.get(
            f"{GRAPHRAG_URL}/ui/config?graphname={GRAPH_ADMIN_GRAPH}",
            auth=(GRAPH_ADMIN_USER, GRAPH_ADMIN_PASSWORD),
        )
        self.assertEqual(resp_get2.status_code, 200, resp_get2.text)
        self.assertIsNone(resp_get2.json().get("chatbot_config"))

    # =========================================================================
    # Test 3 – DB connection test: valid creds succeed, invalid creds fail cleanly
    # =========================================================================

    def test_db_connection_valid_and_invalid_creds(self):
        """POST /ui/config/db/test returns success for valid creds and error (not 500) for invalid."""
        valid_payload = {
            "hostname": os.getenv("TG_HOST", "http://tigergraph"),
            "username": SUPERUSER,
            "password": SUPERUSER_PASSWORD,
            "gsPort": os.getenv("TG_GS_PORT", "14240"),
            "restppPort": os.getenv("TG_RESTPP_PORT", "9000"),
        }
        valid_resp = requests.post(
            f"{GRAPHRAG_URL}/ui/config/db/test",
            json=valid_payload,
            auth=(SUPERUSER, SUPERUSER_PASSWORD),
        )
        self.assertEqual(valid_resp.status_code, 200, valid_resp.text)
        self.assertEqual(valid_resp.json().get("status"), "success")
        self.assertIn("Connection successful", valid_resp.json().get("message", ""))

        invalid_payload = {**valid_payload, "username": "wrong-user", "password": "wrong-pass"}
        invalid_resp = requests.post(
            f"{GRAPHRAG_URL}/ui/config/db/test",
            json=invalid_payload,
            auth=(SUPERUSER, SUPERUSER_PASSWORD),
        )
        self.assertEqual(invalid_resp.status_code, 200, invalid_resp.text)
        body = invalid_resp.json()
        self.assertEqual(body.get("status"), "error")
        self.assertIn("Connection failed", body.get("message", ""))
        self.assertNotIn("Traceback", body.get("message", ""))


if __name__ == "__main__":
    unittest.main()
