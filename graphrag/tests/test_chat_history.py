"""Focused unit tests for principal-scoped TigerGraph chat history."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from common.config_env import resolve_environment_placeholders
from common.db.connection_utils import normalize_restpp_url
from common.chat_history.migrate_sqlite import migrate_sqlite
from common.chat_history.models import (
    HistoryMessage,
    TraceEnvelope,
    TraceStep,
)
from common.chat_history.principal import HistoryPrincipal
from common.chat_history.redaction import (
    build_trace_steps,
    prepare_query_sources,
    prepare_trace_payload,
    redact_value,
    truncate_utf8,
)
from common.chat_history.repository import (
    HistoryConflictError,
    HistoryNotFoundError,
    PrincipalHistoryRepository,
)
from common.chat_history.settings import HistorySettings, load_database_config


class FakeConnection:
    def __init__(self, responses=None):
        self.responses = {
            name: list(values)
            for name, values in (responses or {}).items()
        }
        self.calls = []

    def runInstalledQuery(self, query_name, params):
        self.calls.append((query_name, dict(params)))
        queue = self.responses.get(query_name, [])
        if not queue:
            raise AssertionError(f"Unexpected query {query_name}")
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _settings(**overrides):
    values = {
        "default_page_size": 2,
        "max_page_size": 5,
        "agent_page_size": 2,
        "transient_attempts": 1,
    }
    values.update(overrides)
    return HistorySettings(**values)


def _principal(*, superuser=False):
    return HistoryPrincipal.create(
        user_id="alice",
        accessible_graphs=("KnowledgeGraph", "GraphRAGChatHistory"),
        global_roles=("superuser",) if superuser else (),
        graph_roles={"KnowledgeGraph": ("queryreader",)},
    )


class PrincipalTests(unittest.TestCase):
    def test_principal_is_immutable_and_hides_operational_graph(self):
        principal = _principal()
        self.assertEqual(principal.user_id, "alice")
        self.assertEqual(principal.accessible_graphs, frozenset({"KnowledgeGraph"}))
        with self.assertRaises(FrozenInstanceError):
            principal.user_id = "mallory"
        with self.assertRaises(TypeError):
            principal.graph_roles["KnowledgeGraph"] = ("admin",)

    def test_repository_fails_closed_for_inaccessible_graph(self):
        with self.assertRaises(HistoryNotFoundError):
            PrincipalHistoryRepository(
                _principal(),
                current_graph="OtherGraph",
                connection=FakeConnection(),
                settings=_settings(),
            )


class HistoryMessageTests(unittest.TestCase):
    def test_nullable_ui_fields_use_history_defaults(self):
        message = HistoryMessage.model_validate(
            {
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "role": "user",
                "response_time": None,
                "feedback": None,
                "comment": None,
            }
        )

        self.assertEqual(message.response_time, 0.0)
        self.assertEqual(message.feedback, 0)
        self.assertEqual(message.comment, "")


class SettingsTests(unittest.TestCase):
    def test_database_config_loads_without_application_config_import(self):
        config = {
            "db_config": {
                "hostname": "http://tigergraph",
                "restppPort": "9000",
                "gsPort": "14240",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "server_config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(
                "os.environ", {"SERVER_CONFIG": str(config_path)}, clear=False
            ):
                self.assertEqual(load_database_config(), config["db_config"])

    def test_database_config_accepts_inline_json(self):
        config = {
            "db_config": {
                "hostname": "https://example.invalid",
                "gsPort": "443",
            }
        }
        with patch.dict(
            "os.environ", {"SERVER_CONFIG": json.dumps(config)}, clear=False
        ):
            self.assertEqual(load_database_config(), config["db_config"])

    def test_database_config_resolves_environment_reference(self):
        config = {
            "db_config": {
                "hostname": "${TIGERGRAPH_URL}",
                "restppPort": "443",
                "gsPort": "443",
            }
        }
        with patch.dict(
            "os.environ",
            {
                "SERVER_CONFIG": json.dumps(config),
                "TIGERGRAPH_URL": "https://example.invalid",
            },
            clear=False,
        ):
            self.assertEqual(
                load_database_config()["hostname"],
                "https://example.invalid",
            )

    def test_missing_environment_reference_fails_without_leaking_values(self):
        with self.assertRaisesRegex(
            ValueError, "Required environment variable MISSING_SECRET"
        ):
            resolve_environment_placeholders(
                {"password": "${MISSING_SECRET}"},
                environ={},
            )

    def test_cloud_shared_port_adds_restpp_prefix_once(self):
        connection = type(
            "Connection",
            (),
            {
                "restppPort": "443",
                "gsPort": "443",
                "restppUrl": "https://example.invalid:443",
            },
        )()
        normalize_restpp_url(connection)
        normalize_restpp_url(connection)
        self.assertEqual(
            connection.restppUrl,
            "https://example.invalid:443/restpp",
        )


class RedactionTests(unittest.TestCase):
    def test_recursive_redaction_and_omission(self):
        redacted = redact_value(
            {
                "Authorization": "Bearer abc",
                "nested": {"api_key": "secret", "document_body": "body"},
                "safe": "visible",
            }
        )
        self.assertEqual(redacted["Authorization"], "[redacted]")
        self.assertEqual(redacted["nested"]["api_key"], "[redacted]")
        self.assertEqual(redacted["nested"]["document_body"], "[omitted]")
        self.assertEqual(redacted["safe"], "visible")

    def test_utf8_and_trace_bounds_are_hard_limits(self):
        shortened, truncated = truncate_utf8("😀" * 100, 31)
        self.assertTrue(truncated)
        self.assertLessEqual(len(shortened.encode("utf-8")), 31)

        settings = _settings(max_trace_bytes=128)
        payload, was_cut = prepare_trace_payload(
            {
                "natural_language_response": "x" * 2_000,
                "query_sources": {"token": "do-not-store", "result": "y" * 2_000},
            },
            settings,
        )
        self.assertTrue(was_cut)
        self.assertLessEqual(
            len(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            settings.max_trace_bytes,
        )

    def test_trace_steps_are_redacted_bounded_and_dependency_scoped(self):
        settings = _settings(max_step_summary_bytes=32, max_trace_steps=2)
        steps, truncated = build_trace_steps(
            "trace-1",
            {
                "agent_steps": [
                    {"id": "schema", "tool": "schema"},
                    {
                        "id": "search",
                        "tool": "hybrid",
                        "input": {"password": "abc", "q": "x" * 100},
                        "output": "y" * 100,
                        "depends_on": ["schema"],
                    },
                    {"id": "unused"},
                ]
            },
            settings,
        )
        self.assertTrue(truncated)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1].step_id, "trace-1:search")
        self.assertEqual(steps[1].depends_on, ["trace-1:schema"])
        self.assertNotIn("abc", steps[1].input_summary)

    def test_persisted_query_sources_omit_raw_bodies_and_secrets(self):
        sources, truncated = prepare_query_sources(
            {
                "result": {
                    "final_retrieval": "raw document",
                    "authorization": "Bearer secret",
                    "vertices": [{"id": "visible"}],
                }
            },
            _settings(max_trace_bytes=1024),
        )
        self.assertFalse(truncated)
        self.assertEqual(sources["result"]["final_retrieval"], "[omitted]")
        self.assertEqual(sources["result"]["authorization"], "[redacted]")
        self.assertEqual(sources["result"]["vertices"], [{"id": "visible"}])


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_turn_uses_bound_principal_and_reusable_ids(self):
        connection = FakeConnection(
            {"Chat_Begin_Turn": [{"status": "created"}]}
        )
        repository = PrincipalHistoryRepository(
            _principal(),
            current_graph="KnowledgeGraph",
            connection=connection,
            settings=_settings(),
        )
        result = await repository.begin_turn(
            HistoryMessage(
                conversation_id="conversation-1",
                message_id="message-1",
                turn_id="turn-1",
                content="hello",
                role="user",
            ),
            create_if_missing=True,
            event_time=100,
        )
        self.assertEqual(result["status"], "created")
        query_name, params = connection.calls[0]
        self.assertEqual(query_name, "Chat_Begin_Turn")
        self.assertEqual(params["principal_id"], "alice")
        self.assertEqual(params["graph_name"], "KnowledgeGraph")
        self.assertEqual(params["turn_id"], "turn-1")
        self.assertNotIn("username", params)
        self.assertNotIn("password", params)

    async def test_idempotency_collision_is_typed(self):
        connection = FakeConnection(
            {"Chat_Begin_Turn": [{"status": "conflict"}]}
        )
        repository = PrincipalHistoryRepository(
            _principal(),
            current_graph="KnowledgeGraph",
            connection=connection,
            settings=_settings(),
        )
        with self.assertRaises(HistoryConflictError):
            await repository.begin_turn(
                HistoryMessage(
                    conversation_id="conversation-1",
                    message_id="message-1",
                    turn_id="turn-1",
                    content="changed",
                    role="user",
                )
            )

    async def test_conversation_serialization_and_keyset_cursor(self):
        connection = FakeConnection(
            {
                "Chat_Get_My_Conversation": [
                    {
                        "conversation": [
                            {
                                "v_id": "conversation-1",
                                "attributes": {
                                    "conversation_id": "conversation-1",
                                    "graph_name": "KnowledgeGraph",
                                },
                            }
                        ],
                        "rows": [
                            {
                                "v_id": "message-1",
                                "attributes": {
                                    "message_id": "message-1",
                                    "conversation_id": "conversation-1",
                                    "sequence_no": 0,
                                    "created_at": 100,
                                    "updated_at": 100,
                                    "role": "user",
                                    "content": "hello",
                                },
                            },
                            {
                                "v_id": "message-2",
                                "attributes": {
                                    "message_id": "message-2",
                                    "conversation_id": "conversation-1",
                                    "parent_message_id": "message-1",
                                    "sequence_no": 1,
                                    "created_at": 101,
                                    "updated_at": 101,
                                    "role": "system",
                                    "content": "hi",
                                },
                            },
                        ],
                    }
                ]
            }
        )
        repository = PrincipalHistoryRepository(
            _principal(),
            current_graph="KnowledgeGraph",
            connection=connection,
            settings=_settings(),
        )
        messages, cursor, graph = await repository.get_conversation(
            "conversation-1", limit=2
        )
        self.assertEqual(graph, "KnowledgeGraph")
        self.assertEqual([item["message_id"] for item in messages], ["message-1", "message-2"])
        self.assertIsNotNone(cursor)
        self.assertNotIn("_sequence_no", messages[0])
        self.assertNotIn("_created_at", messages[0])

    async def test_complete_turn_serializes_only_bounded_redacted_data(self):
        connection = FakeConnection(
            {"Chat_Complete_Turn": [{"status": "created"}]}
        )
        repository = PrincipalHistoryRepository(
            _principal(),
            current_graph="KnowledgeGraph",
            connection=connection,
            settings=_settings(max_trace_bytes=1024, max_step_summary_bytes=32),
        )
        result = await repository.complete_turn(
            user_message_id="user-message",
            assistant_message=HistoryMessage(
                conversation_id="conversation-1",
                message_id="assistant-message",
                turn_id="turn-1",
                parent_id="user-message",
                content="answer",
                role="system",
                query_sources={
                    "authorization": "Bearer secret",
                    "final_retrieval": "raw body",
                    "result": {"vertices": [{"id": "visible"}]},
                },
            ),
            trace=TraceEnvelope(
                trace_id="assistant-message",
                message_id="assistant-message",
                conversation_id="conversation-1",
                trace_data={"token": "secret", "safe": "visible"},
                provenance={"document_body": "raw body"},
                steps=[
                    TraceStep(
                        step_id="assistant-message:one",
                        ordinal=0,
                        input_summary="password=secret",
                        output_summary="x" * 100,
                    )
                ],
            ),
            event_time=100,
        )
        self.assertEqual(result["status"], "created")
        _, params = connection.calls[0]
        sources = json.loads(params["query_sources_json"])
        trace = json.loads(params["trace_json"])
        provenance = json.loads(params["provenance_json"])
        steps = json.loads(params["steps_json"])
        self.assertEqual(sources["authorization"], "[redacted]")
        self.assertEqual(sources["final_retrieval"], "[omitted]")
        self.assertEqual(trace["token"], "[redacted]")
        self.assertEqual(provenance["document_body"], "[omitted]")
        self.assertNotIn("secret", steps[0]["input_summary"])
        self.assertLessEqual(
            len(steps[0]["output_summary"].encode("utf-8")), 32
        )
        self.assertTrue(params["trace_truncated"])

    async def test_trace_requires_owner_who_is_also_superuser(self):
        denied_connection = FakeConnection()
        denied = PrincipalHistoryRepository(
            _principal(),
            current_graph="KnowledgeGraph",
            connection=denied_connection,
            settings=_settings(),
        )
        with self.assertRaises(PermissionError):
            await denied.get_trace("message-1")
        self.assertEqual(denied_connection.calls, [])

        allowed_connection = FakeConnection(
            {
                "Chat_Get_My_Trace": [
                    {
                        "trace": [
                            {
                                "attributes": {
                                    "trace_json": '{"status":"completed"}',
                                    "conversation_id": "conversation-1",
                                }
                            }
                        ]
                    }
                ]
            }
        )
        allowed = PrincipalHistoryRepository(
            _principal(superuser=True),
            current_graph="KnowledgeGraph",
            connection=allowed_connection,
            settings=_settings(),
        )
        trace = await allowed.get_trace("message-1")
        self.assertEqual(trace["username"], "alice")
        self.assertEqual(trace["message_id"], "message-1")


class MigrationTests(unittest.TestCase):
    def test_sqlite_dry_run_skips_soft_deleted_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.db"
            with sqlite3.connect(path) as database:
                database.execute(
                    "CREATE TABLE conversations ("
                    "conversation_id TEXT, user_id TEXT, name TEXT, deleted_at TEXT)"
                )
                database.execute(
                    "CREATE TABLE messages ("
                    "id INTEGER, conversation_id TEXT, message_id TEXT, "
                    "content TEXT, role TEXT, deleted_at TEXT)"
                )
                database.executemany(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?)",
                    [
                        ("c1", "alice", "active", None),
                        ("c2", "alice", "deleted", "2026-01-01"),
                    ],
                )
                database.executemany(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (1, "c1", "m1", "hello", "user", None),
                        (2, "c2", "m2", "gone", "user", "2026-01-01"),
                    ],
                )
            result = migrate_sqlite(
                path,
                default_graph="KnowledgeGraph",
                dry_run=True,
                backup=False,
                settings=_settings(),
            )
        self.assertEqual(result["conversations"], 1)
        self.assertEqual(result["messages"], 1)


class GSQLAuthorizationTests(unittest.TestCase):
    def test_every_user_query_is_principal_and_owner_scoped(self):
        query_dir = (
            Path(__file__).resolve().parents[2]
            / "common"
            / "gsql"
            / "chat_history"
        )
        user_queries = [
            "Chat_Begin_Turn.gsql",
            "Chat_Complete_Turn.gsql",
            "Chat_List_My_Conversations.gsql",
            "Chat_Get_My_Conversation.gsql",
            "Chat_Search_My_Messages.gsql",
            "Chat_Get_My_Feedback.gsql",
            "Chat_Update_My_Feedback.gsql",
            "Chat_Delete_My_Conversation.gsql",
            "Chat_Get_My_Trace.gsql",
        ]
        for filename in user_queries:
            source = (query_dir / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("STRING principal_id", source)
                self.assertIn("OWNS_CONVERSATION", source)
                self.assertIn("u.user_id == principal_id", source)


if __name__ == "__main__":
    unittest.main()
