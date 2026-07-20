# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the agent's conversation-history tools.

Loaded standalone since the surrounding ``tools`` package needs the
container's dependency set.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "tools" / "chat_history_tools.py"
)
_spec = importlib.util.spec_from_file_location("chat_history_tools", _MODULE_PATH)
cht = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cht)


class FakeRepo:
    def __init__(self, conversations=None, hits=None, conversation=None):
        self.conversations = conversations or []
        self.hits = hits or []
        self.conversation = conversation  # returned by get_conversation, or None
        self.search_calls = []
        self.get_calls = []

    def list_conversations(self):
        return self.conversations

    def search_messages(self, q, limit=10):
        self.search_calls.append((q, limit))
        return self.hits[:limit]

    def get_conversation(self, conversation_id):
        self.get_calls.append(conversation_id)
        return self.conversation


class FakeCtx:
    def __init__(self, chat_repo=None):
        self.chat_repo = chat_repo
        self.emitted = []

    def emit(self, msg):
        self.emitted.append(msg)


class TestNoSteerableArguments:
    def test_tools_expose_no_user_or_graph_argument(self):
        forbidden = {"user", "user_id", "username", "principal", "owner",
                     "graph", "graphname", "graph_name"}
        for fn in (cht.list_my_conversations, cht.search_my_messages,
                   cht.get_my_conversation):
            params = set(inspect.signature(fn).parameters) - {"ctx"}
            assert not (params & forbidden), f"{fn.__name__} exposes {params & forbidden}"


class TestListMyConversations:
    def test_returns_bound_repos_conversations(self):
        repo = FakeRepo(conversations=[
            {"conversation_id": "c1", "name": "budget chat",
             "create_epoch": 1784400000, "update_epoch": 1784400100},
        ])
        res = cht.list_my_conversations(FakeCtx(repo))
        assert res["ok"] is True
        rows = res["context"]["conversations"]
        assert rows[0]["conversation_id"] == "c1"
        assert rows[0]["name"] == "budget chat"
        assert rows[0]["created"].startswith("2026-")

    def test_limit_is_server_capped(self):
        repo = FakeRepo(conversations=[{"conversation_id": str(i)} for i in range(200)])
        res = cht.list_my_conversations(FakeCtx(repo), limit=9999)
        assert len(res["context"]["conversations"]) == cht._MAX_CONVERSATIONS

    def test_empty_history_is_ok_not_error(self):
        res = cht.list_my_conversations(FakeCtx(FakeRepo()))
        assert res["ok"] is True
        assert res["context"]["conversations"] == []

    def test_without_repo_reports_unavailable(self):
        res = cht.list_my_conversations(FakeCtx(chat_repo=None))
        assert res["ok"] is False
        assert "not available" in res["summary"]


class TestGetMyConversation:
    def test_returns_all_messages_in_order(self):
        repo = FakeRepo(conversation={
            "conversation_id": "c1", "name": "budget",
            "create_epoch": 1784400000, "update_epoch": 1784400100,
            "messages": [
                {"message_id": "m1", "role": "user", "content": "hi",
                 "create_epoch": 1784400000},
                {"message_id": "m2", "role": "system", "content": "hello",
                 "create_epoch": 1784400010},
            ],
        })
        res = cht.get_my_conversation(FakeCtx(repo), conversation_id="c1")
        assert res["ok"] is True
        msgs = res["context"]["messages"]
        assert [m["message_id"] for m in msgs] == ["m1", "m2"]
        assert res["context"]["conversation"]["conversation_id"] == "c1"
        assert repo.get_calls == ["c1"]

    def test_unowned_or_absent_id_is_the_same_empty_answer(self):
        repo = FakeRepo(conversation=None)
        res = cht.get_my_conversation(FakeCtx(repo), conversation_id="bobs-conv")
        assert res["ok"] is True
        assert res["context"]["messages"] == []
        assert "current user's own" in res["summary"]

    def test_message_content_is_size_capped(self):
        repo = FakeRepo(conversation={
            "conversation_id": "c1",
            "messages": [{"message_id": "m1", "role": "user",
                          "content": "x" * 100_000, "create_epoch": 0}],
        })
        res = cht.get_my_conversation(FakeCtx(repo), conversation_id="c1")
        assert len(res["context"]["messages"][0]["content"]) == cht._MAX_CONTENT_CHARS

    def test_without_repo_reports_unavailable(self):
        res = cht.get_my_conversation(FakeCtx(chat_repo=None), conversation_id="c1")
        assert res["ok"] is False
        assert "not available" in res["summary"]


class TestSearchMyMessages:
    def test_search_goes_through_bound_repo(self):
        repo = FakeRepo(hits=[
            {"conversation_id": "c1", "conversation_name": "", "message_id": "m1",
             "content": "about revenue", "role": "user", "create_epoch": 1784400000},
        ])
        res = cht.search_my_messages(FakeCtx(repo), q="revenue")
        assert res["ok"] is True
        assert res["context"]["messages"][0]["message_id"] == "m1"
        assert repo.search_calls == [("revenue", 10)]

    def test_limit_is_server_capped(self):
        repo = FakeRepo(hits=[{"message_id": str(i)} for i in range(100)])
        cht.search_my_messages(FakeCtx(repo), q="x", limit=9999)
        assert repo.search_calls == [("x", cht._MAX_SEARCH_HITS)]

    def test_without_repo_reports_unavailable(self):
        res = cht.search_my_messages(FakeCtx(chat_repo=None), q="anything")
        assert res["ok"] is False
        assert "not available" in res["summary"]
