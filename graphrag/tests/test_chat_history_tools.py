"""Agent-tool schema and principal-binding tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tools import tool_registry


class HistoryToolSchemaTests(unittest.TestCase):
    def test_history_tools_are_absent_without_authenticated_repository(self):
        catalog = tool_registry.catalog(SimpleNamespace(history_repository=None))
        names = {item["name"] for item in catalog}
        self.assertFalse(any(name.startswith("history__") for name in names))

    def test_history_tools_expose_no_identity_credentials_or_graph(self):
        catalog = tool_registry.catalog(
            SimpleNamespace(history_repository=object(), external_tools={})
        )
        history = {
            item["name"]: item
            for item in catalog
            if item["name"].startswith("history__")
        }
        self.assertEqual(
            set(history),
            {
                "history__list_my_conversations",
                "history__get_my_conversation",
                "history__search_my_messages",
            },
        )
        forbidden = {
            "user",
            "user_id",
            "username",
            "password",
            "credential",
            "token",
            "graph",
            "graph_name",
        }
        for name, item in history.items():
            with self.subTest(tool=name):
                properties = set(
                    item["args_schema"].get("properties", {})
                )
                self.assertTrue(properties.isdisjoint(forbidden))


if __name__ == "__main__":
    unittest.main()
