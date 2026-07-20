# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.chat_history.guard``.

Covers what keeps conversation data off the agent's reachable surface:
schema filtering, the argument scan, and query-name matching. No database —
the policy is pure so it can be regression-tested without one.

The case that matters most is ``getVertices('ChatConversation')``:
``validate_function_call`` checks only the function *name* against the
registered-document set, and ``getVertices`` is a legitimate registered
function, so the call passes validation and reaches ``exec``. The argument
scan is the check that stops it.
"""

from __future__ import annotations

import pytest

from common.chat_history.guard import (
    CHAT_HISTORY_QUERY_PREFIX,
    ChatHistoryAccessDenied,
    assert_agent_may_call,
    filter_schema,
    filter_types,
    is_chat_history_query,
    is_chat_history_type,
    mentions_chat_history,
)


class TestIsChatHistoryType:
    @pytest.mark.parametrize(
        "name",
        ["ChatUser", "ChatConversation", "ChatMessage", "ChatTrace", "ChatTraceStep"],
    )
    def test_vertex_types_are_recognized(self, name):
        assert is_chat_history_type(name)

    @pytest.mark.parametrize(
        "name",
        ["OWNS_CONVERSATION", "HAS_MESSAGE", "REPLIES_TO", "HAS_TRACE", "HAS_STEP",
         "NEXT_STEP", "RETRIEVED"],
    )
    def test_edge_types_are_recognized(self, name):
        assert is_chat_history_type(name)

    @pytest.mark.parametrize(
        "name",
        ["DocumentChunk", "Document", "Entity", "Community", "Content",
         "HAS_CONTENT", "CONTAINS_ENTITY"],
    )
    def test_corpus_types_are_not(self, name):
        assert not is_chat_history_type(name)

    def test_match_is_case_insensitive(self):
        # A generated call naming 'chatconversation' is an attempt at the same
        # thing; refuse it rather than let it fail obscurely downstream.
        assert is_chat_history_type("chatconversation")
        assert is_chat_history_type("CHATMESSAGE")

    def test_non_strings_are_not_types(self):
        assert not is_chat_history_type(None)
        assert not is_chat_history_type(42)


class TestFilterTypes:
    def test_drops_chat_types_keeps_corpus(self):
        assert filter_types(
            ["DocumentChunk", "ChatConversation", "Entity", "ChatUser"]
        ) == ["DocumentChunk", "Entity"]

    def test_drops_retrieved_edge(self):
        # RETRIEVED terminates on corpus vertices but traversing it in reverse
        # is the path from shared corpus into another user's trace.
        assert "RETRIEVED" not in filter_types(["HAS_CONTENT", "RETRIEVED"])

    def test_empty_input(self):
        assert filter_types([]) == []
        assert filter_types(None) == []

    def test_corpus_only_input_is_unchanged(self):
        types = ["Document", "DocumentChunk", "Entity"]
        assert filter_types(types) == types


class TestFilterSchema:
    def test_strips_chat_vertex_types(self):
        out = filter_schema(
            {"VertexTypes": [{"Name": "DocumentChunk"}, {"Name": "ChatUser"}]}
        )
        assert [v["Name"] for v in out["VertexTypes"]] == ["DocumentChunk"]

    def test_strips_chat_edge_types(self):
        out = filter_schema(
            {"EdgeTypes": [{"Name": "HAS_CONTENT"}, {"Name": "OWNS_CONVERSATION"}]}
        )
        assert [e["Name"] for e in out["EdgeTypes"]] == ["HAS_CONTENT"]

    def test_leaves_other_keys_alone(self):
        out = filter_schema({"GraphName": "Test", "VertexTypes": []})
        assert out["GraphName"] == "Test"

    def test_non_dict_passes_through(self):
        assert filter_schema(None) is None


class TestMentionsChatHistory:
    def test_finds_type_in_positional_arg(self):
        assert mentions_chat_history(("ChatConversation",), {}) == "ChatConversation"

    def test_finds_type_in_kwarg(self):
        assert mentions_chat_history((), {"vertexType": "ChatMessage"}) == "ChatMessage"

    def test_searches_nested_dicts(self):
        assert mentions_chat_history((), {"params": {"vtype": "ChatUser"}}) == "ChatUser"

    def test_searches_lists(self):
        assert mentions_chat_history((["Entity", "ChatTrace"],), {}) == "ChatTrace"

    def test_ignores_corpus_types(self):
        assert mentions_chat_history(("DocumentChunk", "Entity"), {}) is None

    def test_ignores_none(self):
        assert mentions_chat_history((None,), {}) is None

    def test_word_boundaries_avoid_false_positives(self):
        # A chunk whose text happens to contain a longer word starting with a
        # type name must not trip the guard.
        assert mentions_chat_history(("ChatConversationalist",), {}) is None


class TestIsChatHistoryQuery:
    @pytest.mark.parametrize(
        "name",
        ["Chat_List_Conversations", "Chat_Get_Conversation", "Chat_Get_All_Feedback",
         "Chat_Search_My_Messages", "Chat_Expire_Traces"],
    )
    def test_matches_installed_chat_queries(self, name):
        assert is_chat_history_query(name)

    @pytest.mark.parametrize(
        "name",
        ["GraphRAG_Hybrid_Vector_Search", "Chunk_Sibling_Search", "Scan_For_Updates"],
    )
    def test_does_not_match_retrievers(self, name):
        assert not is_chat_history_query(name)

    def test_prefix_covers_queries_added_later(self):
        # Matching on the prefix rather than an enumerated list means a query
        # added later is covered without editing the guard.
        assert is_chat_history_query(CHAT_HISTORY_QUERY_PREFIX + "Something_New")


class TestAssertAgentMayCall:
    def test_refuses_the_getvertices_bypass(self):
        # The exact call validate_function_call lets through: 'getVertices' is a
        # registered function, and only the name is checked there.
        with pytest.raises(ChatHistoryAccessDenied):
            assert_agent_may_call("getVertices", ("ChatConversation",), {})

    def test_refuses_lowercase_evasion(self):
        with pytest.raises(ChatHistoryAccessDenied):
            assert_agent_may_call("getVertices", ("chatconversation",), {})

    def test_refuses_nested_argument(self):
        with pytest.raises(ChatHistoryAccessDenied):
            assert_agent_may_call("runInstalledQuery", ({"v": "ChatMessage"},), {})

    def test_allows_corpus_access(self):
        assert_agent_may_call("getVertices", ("DocumentChunk",), {})

    def test_allows_empty_args(self):
        assert_agent_may_call("getVertexTypes", (), {})

    def test_error_names_the_type(self):
        with pytest.raises(ChatHistoryAccessDenied, match="ChatConversation"):
            assert_agent_may_call("getVertices", ("ChatConversation",), {})
