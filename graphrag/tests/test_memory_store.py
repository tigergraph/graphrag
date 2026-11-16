from pathlib import Path
import importlib.metadata
import inspect

import pytest

from common.memory.mem0_adapter import (
    PostgresHistoryAdapter,
    SplitMemoryVectorStore,
)
from common.memory.compaction import select_compaction_range
from common.memory.store import PostgresMemoryStore, memory_user_id


@pytest.fixture
def store(tmp_path: Path):
    return PostgresMemoryStore(f"sqlite:///{tmp_path / 'memory.db'}")


def _message(conversation_id, message_id, role="user", content="hello", parent_id=None):
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "parent_id": parent_id,
        "role": role,
        "content": content,
    }


def test_has_facts_is_user_and_graph_scoped(store):
    assert store.has_facts("alice", "g1") is False
    store.put_fact(
        "fact-1",
        username="alice",
        graphname="g1",
        conversation_id="c1",
        text="prefers concise answers",
        payload={},
    )
    assert store.has_facts("alice", "g1") is True
    assert store.has_facts("bob", "g1") is False
    assert store.has_facts("alice", "g2") is False


def test_conversations_are_user_and_graph_scoped(store):
    store.save_message("alice", "g1", _message("c1", "m1"))

    assert len(store.list_conversations("alice", "g1")) == 1
    assert store.list_conversations("bob", "g1") == []
    assert store.list_conversations("alice", "g2") == []
    assert store.get_messages("bob", "g1", "c1") == []


def test_message_id_cannot_cross_conversations(store):
    store.save_message("alice", "g1", _message("c1", "m1"))
    with pytest.raises(PermissionError):
        store.save_message("alice", "g1", _message("c2", "m1"))


def test_mem0_history_reads_canonical_transcript_without_duplicate_table(store):
    store.save_message("alice", "g1", _message("c1", "m1", content="question"))
    store.save_message(
        "alice",
        "g1",
        _message("c1", "m2", role="system", content="answer", parent_id="m1"),
    )
    adapter = PostgresHistoryAdapter(store)
    scope = f"run_id=c1&user_id={memory_user_id('alice', 'g1')}"

    adapter.save_messages([{"role": "user", "content": "duplicate"}], scope)
    messages = adapter.get_last_messages(scope)

    assert [message["content"] for message in messages] == ["question", "answer"]


class _FakeEmbeddingStore:
    def __init__(self):
        self.vectors = {}

    def upsert_memory_embedding(self, index_id, vector):
        self.vectors[index_id] = vector

    def search_memory_embeddings(self, scope_pattern, query_vector, top_k):
        prefix = scope_pattern.removesuffix("%")
        return [
            (index_id, 0.9)
            for index_id in self.vectors
            if index_id.startswith(prefix)
        ][:top_k]

    def delete_memory_embedding(self, index_id):
        self.vectors.pop(index_id, None)


def test_split_store_keeps_payload_in_sql_and_vector_in_tigergraph(store):
    indexes = {}

    def resolver(graphname):
        return indexes.setdefault(graphname, _FakeEmbeddingStore())

    adapter = SplitMemoryVectorStore(store, resolver)
    payload = {
        "user_id": memory_user_id("alice", "g1"),
        "run_id": "c1",
        "data": "Alice prefers concise answers",
    }
    adapter.insert([[0.1, 0.2]], [payload], ["fact-1"])

    fact = store.get_fact("fact-1")
    assert fact.text == "Alice prefers concise answers"
    assert list(indexes["g1"].vectors.values()) == [[0.1, 0.2]]
    assert adapter.search(
        "concise", [0.1, 0.2], filters={"user_id": payload["user_id"]}
    )[0].id == "fact-1"
    assert adapter.search(
        "concise",
        [0.1, 0.2],
        filters={"user_id": memory_user_id("bob", "g1")},
    ) == []


def test_summary_advances_only_forward(store):
    store.save_message("alice", "g1", _message("c1", "m1"))
    store.update_summary(
        "alice",
        "g1",
        "c1",
        summary_text="first",
        through_sequence=1,
        source_tokens=10,
        summary_tokens=2,
        model="test",
    )
    store.update_summary(
        "alice",
        "g1",
        "c1",
        summary_text="stale",
        through_sequence=1,
        source_tokens=10,
        summary_tokens=2,
        model="test",
    )

    assert store.get_summary("alice", "g1", "c1").summary == "first"


def test_compaction_is_token_triggered_not_message_count():
    many_short_messages = [
        {"role": "user", "content": "ok"},
        {"role": "system", "content": "yes"},
    ] * 20
    assert select_compaction_range(
        "",
        many_short_messages,
        budget_tokens=1000,
    ) == []


def test_compaction_preserves_newest_turns_under_token_pressure():
    messages = [
        {
            "role": "user" if index % 2 == 0 else "system",
            "content": f"{index}-" + "x" * 400,
        }
        for index in range(12)
    ]
    selected = select_compaction_range(
        "",
        messages,
        budget_tokens=500,
    )

    assert selected
    assert len(selected) <= len(messages) - 4
    assert all(item not in selected for item in messages[-4:])


def test_pinned_mem0_vector_store_contract():
    pytest.importorskip("mem0")
    assert importlib.metadata.version("mem0ai") == "2.1.0"
    assert not inspect.isabstract(SplitMemoryVectorStore)


def test_graphrag_mem0_llm_uses_bound_langchain_model():
    from common.memory.runtime import GraphRAGMem0LLM

    class _FakeModel:
        def __init__(self):
            self.bound = None
            self.invoked = None

        def bind_tools(self, tools=None, tool_choice="auto"):
            self.bound = (tools, tool_choice)
            return self

        def invoke(self, messages, **kwargs):
            self.invoked = messages
            return type("Resp", (), {
                "content": "ok",
                "tool_calls": [{"name": "add_memory", "args": {"data": "x"}}],
            })()

    model = _FakeModel()
    llm = GraphRAGMem0LLM(type("Cfg", (), {"model": model})())
    result = llm.generate_response(
        [{"role": "user", "content": "hi"}],
        tools=[{"function": {"name": "add_memory"}}],
    )

    assert model.bound is not None
    assert model.invoked == [("human", "hi")]
    assert result["tool_calls"][0]["name"] == "add_memory"


def test_fact_cap_is_scoped_and_enqueues_vector_deletion(store):
    for index in range(3):
        store.put_fact(
            f"fact-{index}",
            username="alice",
            graphname="g1",
            conversation_id="c1",
            text=f"preference {index}",
            payload={},
        )
    store.put_fact(
        "other-user",
        username="bob",
        graphname="g1",
        conversation_id="c2",
        text="other",
        payload={},
    )

    assert store.enforce_fact_cap("alice", "g1", 2) == 1
    assert len(store.list_facts("alice", "g1")) == 2
    assert len(store.list_facts("bob", "g1")) == 1
    assert any(item.operation == "delete" for item in store.pending_outbox())
