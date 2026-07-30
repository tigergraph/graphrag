"""Opt-in TigerGraph integration tests for chat history.

Set ``CHAT_HISTORY_INTEGRATION_GRAPH`` to a disposable, non-production graph
name and provide bootstrap credentials to enable this suite.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from common.chat_history.bootstrap import (
    _admin_connection,
    bootstrap_history_graph,
)
from common.chat_history.migrate_sqlite import migrate_sqlite
from common.chat_history.models import HistoryMessage, TraceEnvelope
from common.chat_history.principal import HistoryPrincipal
from common.chat_history.repository import (
    AdminHistoryRepository,
    HistoryNotFoundError,
    PrincipalHistoryRepository,
)
from common.chat_history.settings import HistorySettings


_GRAPH = os.getenv("CHAT_HISTORY_INTEGRATION_GRAPH", "").strip()
_ENABLED = bool(
    _GRAPH
    and _GRAPH != "GraphRAGChatHistory"
    and os.getenv("CHAT_HISTORY_BOOTSTRAP_USERNAME")
    and os.getenv("CHAT_HISTORY_BOOTSTRAP_PASSWORD")
)


@unittest.skipUnless(
    _ENABLED,
    "set a disposable CHAT_HISTORY_INTEGRATION_GRAPH and bootstrap credentials",
)
class TigerGraphHistoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = HistorySettings(
            graph_name=_GRAPH,
            retention_days=1,
            default_page_size=2,
            max_page_size=20,
            agent_page_size=5,
            transient_attempts=1,
            timeout_seconds=60,
        )
        cls.connection = _admin_connection(cls.settings)
        first = bootstrap_history_graph(
            connection=cls.connection,
            settings=cls.settings,
            configure_role=False,
        )
        second = bootstrap_history_graph(
            connection=cls.connection,
            settings=cls.settings,
            configure_role=False,
        )
        if first["queries"] != second["queries"]:
            raise AssertionError("idempotent bootstrap installed different queries")

    def _repository(self, user, graph="KnowledgeGraph", *, superuser=False):
        principal = HistoryPrincipal.create(
            user_id=user,
            accessible_graphs=(graph,),
            global_roles=("superuser",) if superuser else (),
            operational_graph=self.settings.graph_name,
        )
        return PrincipalHistoryRepository(
            principal,
            current_graph=graph,
            connection=self.connection,
            settings=self.settings,
        )

    async def _create_turn(self, repository, conversation_id, text, *, expires_at=0):
        turn_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        await repository.begin_turn(
            HistoryMessage(
                conversation_id=conversation_id,
                message_id=user_id,
                turn_id=turn_id,
                content=text,
                role="user",
            ),
            create_if_missing=True,
        )
        await repository.complete_turn(
            user_message_id=user_id,
            assistant_message=HistoryMessage(
                conversation_id=conversation_id,
                message_id=assistant_id,
                turn_id=turn_id,
                parent_id=user_id,
                content=f"answer: {text}",
                role="system",
            ),
            trace=TraceEnvelope(
                trace_id=assistant_id,
                message_id=assistant_id,
                conversation_id=conversation_id,
                trace_data={"status": "completed", "token": "must-redact"},
                expires_at=expires_at,
            ),
        )
        return turn_id, user_id, assistant_id

    async def test_crud_pagination_isolation_concurrency_and_new_connection(self):
        owner = f"owner-{uuid.uuid4()}"
        conversation_id = str(uuid.uuid4())
        repository = self._repository(owner)
        turn_id, user_id, assistant_id = await self._create_turn(
            repository, conversation_id, "first"
        )

        # Replaying the same immutable IDs is idempotent.
        replay = await repository.begin_turn(
            HistoryMessage(
                conversation_id=conversation_id,
                message_id=user_id,
                turn_id=turn_id,
                content="first",
                role="user",
            ),
        )
        self.assertIn(replay["status"], {"created", "replayed"})

        await asyncio.gather(
            *[
                repository.begin_turn(
                    HistoryMessage(
                        conversation_id=conversation_id,
                        message_id=str(uuid.uuid4()),
                        turn_id=str(uuid.uuid4()),
                        content=f"concurrent-{index}",
                        role="user",
                    )
                )
                for index in range(5)
            ]
        )
        first_page, cursor, graph = await repository.get_conversation(
            conversation_id, limit=2
        )
        self.assertEqual(graph, "KnowledgeGraph")
        self.assertEqual(len(first_page), 2)
        self.assertIsNotNone(cursor)
        second_page, _, _ = await repository.get_conversation(
            conversation_id, cursor=cursor, limit=20
        )
        self.assertTrue(second_page)

        with self.assertRaises(HistoryNotFoundError):
            await self._repository(f"other-{uuid.uuid4()}").get_conversation(
                conversation_id
            )
        with self.assertRaises(HistoryNotFoundError):
            await self._repository(owner, "OtherGraph").get_conversation(
                conversation_id
            )

        # A new repository/connection observes the persisted records.
        fresh_connection = _admin_connection(self.settings)
        fresh_repository = PrincipalHistoryRepository(
            repository.principal,
            current_graph="KnowledgeGraph",
            connection=fresh_connection,
            settings=self.settings,
        )
        persisted, _, _ = await fresh_repository.get_conversation(
            conversation_id, limit=20
        )
        self.assertTrue(
            any(item["message_id"] == assistant_id for item in persisted)
        )

    async def test_trace_expiry(self):
        owner = f"trace-owner-{uuid.uuid4()}"
        repository = self._repository(owner, superuser=True)
        conversation_id = str(uuid.uuid4())
        _, _, assistant_id = await self._create_turn(
            repository,
            conversation_id,
            "expire",
            expires_at=int(time.time()) - 1,
        )
        self.assertEqual(
            (await repository.get_trace(assistant_id))["message_id"],
            assistant_id,
        )
        admin = AdminHistoryRepository(
            repository.principal,
            connection=self.connection,
            settings=self.settings,
        )
        self.assertGreaterEqual(await admin.expire_traces(), 1)
        with self.assertRaises(HistoryNotFoundError):
            await repository.get_trace(assistant_id)

    async def test_sqlite_migration_is_resumable(self):
        owner = f"migration-owner-{uuid.uuid4()}"
        conversation_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.db"
            with sqlite3.connect(path) as database:
                database.execute(
                    "CREATE TABLE conversations (conversation_id TEXT, "
                    "user_id TEXT, name TEXT, deleted_at TEXT)"
                )
                database.execute(
                    "CREATE TABLE messages (id INTEGER, conversation_id TEXT, "
                    "message_id TEXT, parent_id TEXT, model_name TEXT, "
                    "content TEXT, role TEXT, response_time REAL, feedback INTEGER, "
                    "comment TEXT, created_at TEXT, updated_at TEXT, deleted_at TEXT)"
                )
                database.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, NULL)",
                    (conversation_id, owner, "migrated"),
                )
                database.execute(
                    "INSERT INTO messages VALUES "
                    "(1, ?, ?, NULL, 'model', 'hello', 'user', 0, 0, '', "
                    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)",
                    (conversation_id, message_id),
                )
            first = migrate_sqlite(
                path,
                default_graph="KnowledgeGraph",
                backup=False,
                connection=self.connection,
                settings=self.settings,
            )
            second = migrate_sqlite(
                path,
                default_graph="KnowledgeGraph",
                backup=False,
                connection=self.connection,
                settings=self.settings,
            )
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["replayed"], 1)


if __name__ == "__main__":
    unittest.main()
