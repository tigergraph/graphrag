"""Unit tests for chat-history authorization and router error behavior."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response
from fastapi.security import HTTPBasicCredentials

from app.main import app  # noqa: F401 - initializes the production router layout

sys.modules.setdefault("app.routers.ui", sys.modules["routers.ui"])

from app.routers.ui import (  # noqa: E402
    _history_principal,
    _raise_history_http,
    get_user_conversations,
    load_conversation_history,
)
from common.chat_history.repository import (
    HistoryConflictError,
    HistoryNotFoundError,
    HistoryPayloadTooLargeError,
    HistoryUnavailableError,
)


def _auth_context():
    return (
        ["KnowledgeGraph", "GraphRAGChatHistory"],
        HTTPBasicCredentials(username="alice-login", password="secret"),
    )


class HistoryRouterTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "app.routers.ui._get_user_role_details",
        return_value=(
            ["superuser"],
            {"KnowledgeGraph": ["queryreader"]},
            "alice",
        ),
    )
    def test_canonical_principal_drops_operational_graph(self, _roles):
        principal = _history_principal(_auth_context())
        self.assertEqual(principal.user_id, "alice")
        self.assertEqual(principal.accessible_graphs, frozenset({"KnowledgeGraph"}))

    def test_typed_repository_errors_have_stable_http_mapping(self):
        cases = [
            (HistoryNotFoundError("hidden"), 404),
            (HistoryConflictError("collision"), 409),
            (HistoryPayloadTooLargeError("large"), 413),
            (HistoryUnavailableError("down"), 503),
            (PermissionError("no"), 403),
            (ValueError("bad"), 422),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(HTTPException) as caught:
                    _raise_history_http(error)
                self.assertEqual(caught.exception.status_code, expected)

    async def test_history_load_propagates_not_found_instead_of_empty_fallback(self):
        repository = SimpleNamespace(
            load_agent_history=AsyncMock(
                side_effect=HistoryNotFoundError("not owned")
            )
        )
        with self.assertRaises(HistoryNotFoundError):
            await load_conversation_history("conversation-1", repository)

    @patch("app.routers.ui.create_history_repository")
    @patch(
        "app.routers.ui._history_principal",
        return_value=SimpleNamespace(user_id="alice"),
    )
    async def test_list_route_forwards_graph_cursor_and_limit(
        self, _principal, create_repository
    ):
        page = SimpleNamespace(items=[{"conversation_id": "c1"}], next_cursor="next")
        repository = SimpleNamespace(
            list_conversations=AsyncMock(return_value=page)
        )
        create_repository.return_value = repository
        response = Response()
        result = await get_user_conversations(
            "alice",
            response,
            _auth_context(),
            graph_name="KnowledgeGraph",
            cursor="cursor",
            limit=25,
        )
        self.assertEqual(result, page.items)
        self.assertEqual(response.headers["X-Next-Cursor"], "next")
        repository.list_conversations.assert_awaited_once_with(
            graph_name="KnowledgeGraph",
            cursor="cursor",
            limit=25,
        )

    @patch(
        "app.routers.ui._history_principal",
        return_value=SimpleNamespace(user_id="alice"),
    )
    async def test_list_route_rejects_caller_selected_other_user(self, _principal):
        with self.assertRaises(HTTPException) as caught:
            await get_user_conversations(
                "mallory",
                Response(),
                _auth_context(),
            )
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
