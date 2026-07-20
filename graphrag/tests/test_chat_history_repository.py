# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.chat_history.repository`` against a fake connection.

Covers the parts that are logic rather than storage: principal binding,
sequence assignment, partial updates, and the RETRIEVED target filter. A fake
connection keeps these runnable without TigerGraph; the traversal-level
isolation guarantees are exercised separately against a live database.
"""

from __future__ import annotations

import inspect

import pytest

from common.chat_history.repository import (
    ConversationRepository,
    NotConversationOwner,
    TraceRepository,
    _is_missing_vertex_error,
    _next_seq,
)


class FakeConn:
    """Records writes; returns canned reads."""

    def __init__(self, query_results=None, vertices_by_id=None):
        self.graphname = "Test"
        self.upserted_vertices = []
        self.upserted_edges = []
        self.queries = []
        self._query_results = query_results or {}
        self._vertices_by_id = vertices_by_id or {}

    def runInstalledQuery(self, name, params=None, **kw):
        self.queries.append((name, params))
        return self._query_results.get(name, [])

    def upsertVertex(self, vtype, vid, attrs):
        self.upserted_vertices.append((vtype, vid, attrs))

    def upsertEdge(self, st, sid, etype, tt, tid, attrs=None):
        self.upserted_edges.append((st, sid, etype, tt, tid))

    def getVerticesById(self, vtype, vids):
        if isinstance(vids, str):
            vids = [vids]
        known = self._vertices_by_id.get(vtype, set())
        return [{"v_id": v} for v in vids if v in known]


class TestPrincipalBinding:
    def test_principal_is_required(self):
        # A repository with no principal has no scope; defaulting to one would
        # silently widen it.
        with pytest.raises(ValueError):
            ConversationRepository(FakeConn(), "")

    def test_none_principal_rejected(self):
        with pytest.raises(ValueError):
            ConversationRepository(FakeConn(), None)

    def test_principal_is_exposed_readonly(self):
        assert ConversationRepository(FakeConn(), "alice").principal == "alice"

    def test_no_read_method_accepts_a_user_argument(self):
        # The property the agent tooling relies on: an LLM can be argued into
        # supplying any argument a tool exposes, so none of these may expose one.
        forbidden = {"user", "user_id", "username", "principal", "owner"}
        for name in ("list_conversations", "get_conversation", "search_messages",
                     "list_feedback", "delete_conversation"):
            params = set(inspect.signature(
                getattr(ConversationRepository, name)
            ).parameters)
            assert not (params & forbidden), f"{name} exposes a user argument"

    def test_trace_read_exposes_no_user_argument(self):
        params = set(inspect.signature(TraceRepository.get_trace).parameters)
        assert not (params & {"user", "user_id", "username", "principal"})

    def test_principal_is_passed_as_vertex_tuple(self):
        # pyTigerGraph wants a 1-tuple for VERTEX<T>; a bare string costs a
        # failed POST plus a GET retry on every read.
        conn = FakeConn()
        ConversationRepository(conn, "alice").list_conversations()
        _, params = conn.queries[0]
        assert params["u"] == ("alice",)


class TestFirstTimeUser:
    """A principal whose ChatUser vertex does not exist yet.

    The vertex is created on first write, so every read before that passes a
    VERTEX<ChatUser> naming nothing. TigerGraph rejects that rather than
    matching nothing — which made reads raise, and made the ownership probe
    inside the write path raise, so a new user's first message was refused and
    then swallowed by the caller's catch-all.
    """

    class MissingVertexConn(FakeConn):
        def runInstalledQuery(self, name, params=None, **kw):
            raise Exception("Failed to convert user vertex id for parameter u", None)

    def test_error_is_recognized(self):
        assert _is_missing_vertex_error(
            Exception("Failed to convert user vertex id for parameter u", None)
        )

    def test_unrelated_errors_are_not_swallowed(self):
        # Swallowing a real failure would be indistinguishable from "this user
        # has no conversations".
        assert not _is_missing_vertex_error(Exception("connection refused"))
        assert not _is_missing_vertex_error(Exception("REST-30000: query timed out"))

    def test_list_is_empty_not_an_error(self):
        repo = ConversationRepository(self.MissingVertexConn(), "newcomer")
        assert repo.list_conversations() == []

    def test_get_is_none_not_an_error(self):
        repo = ConversationRepository(self.MissingVertexConn(), "newcomer")
        assert repo.get_conversation("anything") is None

    def test_search_is_empty_not_an_error(self):
        repo = ConversationRepository(self.MissingVertexConn(), "newcomer")
        assert repo.search_messages("hi") == []

    def test_delete_is_false_not_an_error(self):
        repo = ConversationRepository(self.MissingVertexConn(), "newcomer")
        assert repo.delete_conversation("anything") is False

    def test_trace_is_none_not_an_error(self):
        repo = TraceRepository(self.MissingVertexConn(), "newcomer")
        assert repo.get_trace("anything") is None

    def test_first_message_is_not_refused(self):
        # The bug: the ownership probe raised before the user's vertex could be
        # created, so a first-time user's first message never persisted.
        repo = ConversationRepository(self.MissingVertexConn(), "newcomer")
        repo.append_message("c1", "m1", content="my first question", role="user")
        assert ("ChatMessage", "m1", {}) or True  # write happened without raising
        assert any(v == "m1" for _, v, _ in repo._conn.upserted_vertices)

    def test_real_query_failures_still_propagate(self):
        class Broken(FakeConn):
            def runInstalledQuery(self, name, params=None, **kw):
                raise RuntimeError("TigerGraph is on fire")

        repo = ConversationRepository(Broken(), "alice")
        with pytest.raises(RuntimeError):
            repo.list_conversations()


class TestNextSeq:
    def test_first_message_starts_at_one(self):
        assert _next_seq(None, "m1") == 1
        assert _next_seq({"messages": []}, "m1") == 1

    def test_appends_after_current_max(self):
        conv = {"messages": [{"message_id": "a", "seq": 1}, {"message_id": "b", "seq": 2}]}
        assert _next_seq(conv, "c") == 3

    def test_rewrite_keeps_its_place(self):
        # A retry or a feedback update must not reorder the thread.
        conv = {"messages": [{"message_id": "a", "seq": 1}, {"message_id": "b", "seq": 2}]}
        assert _next_seq(conv, "a") == 1

    def test_tolerates_missing_seq(self):
        conv = {"messages": [{"message_id": "a"}]}
        assert _next_seq(conv, "b") == 1


class TestAppendMessage:
    def _repo(self):
        # An owned, empty conversation.
        results = {"Chat_Get_Conversation": [{"Convs": [{"attributes": {"conversation_id": "c1"}}]}, {"Msgs": []}]}
        return ConversationRepository(FakeConn(query_results=results), "alice")

    def test_none_fields_are_not_written(self):
        # Feedback updates arrive with content=None; writing it through would
        # erase the message body.
        repo = self._repo()
        repo.append_message("c1", "m1", feedback=1)
        attrs = [a for t, v, a in repo._conn.upserted_vertices if v == "m1"][0]
        assert "content" not in attrs
        assert "role" not in attrs
        assert attrs["feedback"] == 1

    def test_create_epoch_is_insert_only(self):
        repo = self._repo()
        repo.append_message("c1", "m1", content="hi", role="user")
        attrs = [a for t, v, a in repo._conn.upserted_vertices if v == "m1"][0]
        assert attrs["create_epoch"][1] == "ignore_if_exists"

    def test_parent_id_becomes_an_edge(self):
        repo = self._repo()
        repo.append_message("c1", "m2", content="a", role="system", parent_id="m1")
        assert ("ChatMessage", "m2", "REPLIES_TO", "ChatMessage", "m1") in repo._conn.upserted_edges

    def test_no_parent_edge_without_parent(self):
        repo = self._repo()
        repo.append_message("c1", "m1", content="q", role="user")
        assert not [e for e in repo._conn.upserted_edges if e[2] == "REPLIES_TO"]

    def test_write_to_someone_elses_conversation_is_refused(self):
        # Not-yours and not-there read alike, so the write path probes by
        # primary id: an existing id the caller can't reach belongs to someone
        # else, and appending would attach a second OWNS_CONVERSATION edge.
        conn = FakeConn(
            query_results={"Chat_Get_Conversation": [{"Convs": []}, {"Msgs": []}]},
            vertices_by_id={"ChatConversation": {"bobs-conv"}},
        )
        repo = ConversationRepository(conn, "alice")
        with pytest.raises(NotConversationOwner):
            repo.append_message("bobs-conv", "evil", content="x", role="user")


class TestConversationUpsert:
    _OWNED = {"Chat_Get_Conversation": [{"Convs": [{"attributes": {"conversation_id": "c1"}}]}, {"Msgs": []}]}

    def _conv_attrs(self, conn):
        return [a for t, v, a in conn.upserted_vertices if t == "ChatConversation"]

    def test_append_does_not_blank_conversation_name(self):
        repo = ConversationRepository(FakeConn(query_results=self._OWNED), "alice")
        repo.append_message("c1", "m1", content="hi", role="user")
        for attrs in self._conv_attrs(repo._conn):
            assert "name" not in attrs

    def test_supplied_name_is_written(self):
        repo = ConversationRepository(FakeConn(query_results=self._OWNED), "alice")
        repo.upsert_conversation("c1", name="quarterly report chat")
        assert self._conv_attrs(repo._conn)[0]["name"] == "quarterly report chat"

    def test_conversation_create_epoch_is_insert_only(self):
        repo = ConversationRepository(FakeConn(query_results=self._OWNED), "alice")
        repo.append_message("c1", "m1", content="hi", role="user")
        for attrs in self._conv_attrs(repo._conn):
            if "create_epoch" in attrs:
                assert attrs["create_epoch"][1] == "ignore_if_exists"


class TestOwnershipProbeFailsClosed:
    class _NotYours(FakeConn):
        def __init__(self, probe_exc):
            super().__init__(
                query_results={"Chat_Get_Conversation": [{"Convs": []}, {"Msgs": []}]}
            )
            self._probe_exc = probe_exc

        def getVerticesById(self, vtype, vids):
            raise self._probe_exc

    def test_unknown_probe_failure_propagates(self):
        repo = ConversationRepository(
            self._NotYours(RuntimeError("connection reset")), "alice"
        )
        with pytest.raises(RuntimeError):
            repo.append_message("c1", "m1", content="x", role="user")
        assert repo._conn.upserted_edges == []

    def test_absent_id_error_still_allows_creation(self):
        repo = ConversationRepository(
            self._NotYours(
                Exception("The input id 'c1' is not a valid vertex id for vertex type = ChatConversation")
            ),
            "alice",
        )
        repo.append_message("c1", "m1", content="x", role="user")
        assert any(v == "m1" for _, v, _ in repo._conn.upserted_vertices)


class TestSetFeedback:
    _CONV = {
        "Chat_Get_Conversation": [
            {"Convs": [{"attributes": {"conversation_id": "c1"}}]},
            {"Msgs": [{"attributes": {"message_id": "mine"}}]},
        ]
    }

    def test_feedback_on_own_message_is_written(self):
        repo = ConversationRepository(FakeConn(query_results=self._CONV), "alice")
        repo.set_feedback("c1", "mine", feedback=1, comment="good")
        assert ("ChatMessage", "mine", {"feedback": 1, "comment": "good"}) in repo._conn.upserted_vertices

    def test_message_outside_conversation_is_refused(self):
        repo = ConversationRepository(FakeConn(query_results=self._CONV), "alice")
        with pytest.raises(NotConversationOwner):
            repo.set_feedback("c1", "bobs-message", feedback=1)
        assert repo._conn.upserted_vertices == []

    def test_unowned_conversation_is_refused(self):
        conn = FakeConn(query_results={"Chat_Get_Conversation": [{"Convs": []}, {"Msgs": []}]})
        repo = ConversationRepository(conn, "alice")
        with pytest.raises(NotConversationOwner):
            repo.set_feedback("bobs-conv", "any", feedback=1)
        assert conn.upserted_vertices == []


class TestRetrievedTargets:
    def test_unverified_targets_are_dropped(self):
        # upsertEdge fabricates a missing endpoint, so an unverified id would
        # write an empty DocumentChunk into the shared corpus.
        conn = FakeConn(vertices_by_id={"DocumentChunk": {"real-chunk"}})
        repo = TraceRepository(conn, "alice")
        live = repo._live_targets([
            {"id": "real-chunk", "type": "DocumentChunk"},
            {"id": "ghost-chunk", "type": "DocumentChunk"},
        ])
        assert live == [{"id": "real-chunk", "type": "DocumentChunk"}]

    def test_non_corpus_targets_are_skipped(self):
        conn = FakeConn(vertices_by_id={"ChatConversation": {"c1"}})
        repo = TraceRepository(conn, "alice")
        assert repo._live_targets([{"id": "c1", "type": "ChatConversation"}]) == []

    def test_malformed_targets_are_skipped(self):
        repo = TraceRepository(FakeConn(), "alice")
        assert repo._live_targets([{"id": None, "type": "DocumentChunk"}, {}]) == []

    def test_lookup_failure_drops_rather_than_fabricates(self):
        class Boom(FakeConn):
            def getVerticesById(self, vtype, vids):
                raise RuntimeError("lookup down")

        repo = TraceRepository(Boom(), "alice")
        assert repo._live_targets([{"id": "x", "type": "DocumentChunk"}]) == []


class TestSearchMessages:
    def test_limit_is_clamped(self):
        conn = FakeConn()
        repo = ConversationRepository(conn, "alice")
        repo.search_messages("q", limit=9999)
        assert conn.queries[0][1]["result_limit"] == 50
        repo.search_messages("q", limit=0)
        assert conn.queries[1][1]["result_limit"] == 10
