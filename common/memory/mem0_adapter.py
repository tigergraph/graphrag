# Copyright (c) 2024-2026 TigerGraph, Inc.
"""Adapters that keep mem0 payloads in PostgreSQL and vectors in TigerGraph."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from common.memory.store import PostgresMemoryStore, scope_hash

os.environ.setdefault("MEM0_TELEMETRY", "false")

try:
    from mem0.vector_stores.base import VectorStoreBase
except ImportError:  # Allows the application to start when memory is disabled.
    class VectorStoreBase:  # type: ignore[no-redef]
        pass


@dataclass
class MemoryVectorResult:
    id: str
    score: float
    payload: dict


def split_user_id(value: str) -> tuple[str, str]:
    try:
        graphname, username = value.split("\0", 1)
    except ValueError as exc:
        raise ValueError("mem0 user_id must include graph and username") from exc
    if not graphname or not username:
        raise ValueError("mem0 user_id contains an empty scope component")
    return username, graphname


class PostgresHistoryAdapter:
    """Drop-in replacement for mem0's hardcoded SQLiteManager."""

    def __init__(self, store: PostgresMemoryStore, message_limit: int = 10):
        self.store = store
        self.message_limit = message_limit

    def save_messages(self, messages: list[dict], session_scope: str) -> None:
        # The chat path synchronously persists the same messages before mem0
        # runs. Keeping another rolling-message table would duplicate STM.
        return None

    def get_last_messages(self, session_scope: str, limit: int = 10) -> list[dict]:
        scope = {}
        for part in session_scope.split("&"):
            key, separator, value = part.partition("=")
            if separator:
                scope[key] = unquote(value)
        user_id, run_id = scope.get("user_id"), scope.get("run_id")
        if not user_id or not run_id:
            return []
        username, graphname = split_user_id(user_id)
        messages = self.store.get_messages(username, graphname, run_id)
        return [
            {
                "role": item.get("role"),
                "content": item.get("content"),
                "created_at": str(item.get("create_ts") or ""),
            }
            for item in messages[-min(limit, self.message_limit) :]
        ]

    def add_history(
        self,
        memory_id,
        old_memory,
        new_memory,
        event,
        *,
        created_at=None,
        updated_at=None,
        is_deleted=0,
        actor_id=None,
        role=None,
    ) -> None:
        self.store.add_history(locals())

    def batch_add_history(self, records: list[dict]) -> None:
        for record in records:
            self.store.add_history(record)

    def get_history(self, memory_id: str) -> list[dict]:
        return self.store.get_history(memory_id)

    def reset(self) -> None:
        # Global reset is deliberately unsupported in a multi-tenant database.
        raise RuntimeError("Global mem0 history reset is disabled")

    def close(self) -> None:
        return None


class SplitMemoryVectorStore(VectorStoreBase):
    """mem0 VectorStoreBase with PostgreSQL payloads and TigerGraph vectors."""

    def __init__(
        self,
        store: PostgresMemoryStore,
        embedding_store_resolver: Any,
        collection_name: str = "graphrag_memory",
    ):
        self.store = store
        self.embedding_store_resolver = embedding_store_resolver
        self.collection_name = collection_name

    @staticmethod
    def _scope(payload_or_filters: dict) -> tuple[str, str, str | None]:
        user_id = payload_or_filters.get("user_id")
        if not user_id:
            raise ValueError("All memory operations require user_id")
        username, graphname = split_user_id(str(user_id))
        run_id = payload_or_filters.get("run_id")
        return username, graphname, str(run_id) if run_id else None

    def create_col(self, name, vector_size, distance):
        return None

    def insert(self, vectors, payloads=None, ids=None):
        payloads, ids = payloads or [], ids or []
        for vector, payload, fact_id in zip(vectors, payloads, ids):
            username, graphname, run_id = self._scope(payload)
            row = self.store.put_fact(
                str(fact_id),
                username,
                graphname,
                run_id,
                str(payload.get("data") or ""),
                dict(payload),
            )
            self.embedding_store_resolver(graphname).upsert_memory_embedding(
                row.index_id, vector
            )
            self.store.mark_fact_indexed(row.fact_id)

    def search(self, query, vectors, top_k=5, filters=None):
        username, graphname, run_id = self._scope(filters or {})
        hits = self.embedding_store_resolver(graphname).search_memory_embeddings(
            f"{scope_hash(username, graphname)}:%",
            vectors,
            top_k=max(top_k * 2, top_k),
        )
        score_by_id = {hit_id: score for hit_id, score in hits}
        rows = self.store.facts_by_index_ids(username, graphname, score_by_id)
        results = []
        for row in rows:
            if run_id and str(row.payload.get("run_id") or "") != run_id:
                continue
            results.append(
                MemoryVectorResult(
                    id=row.fact_id,
                    score=float(score_by_id[row.index_id]),
                    payload=dict(row.payload),
                )
            )
            if len(results) >= top_k:
                break
        return results

    def delete(self, vector_id):
        row = self.store.get_fact(str(vector_id))
        if row:
            self.store.delete_fact(row.fact_id)
            self.embedding_store_resolver(
                row.graphname
            ).delete_memory_embedding(row.index_id)
            self.store.mark_fact_unindexed(row.fact_id)

    def update(self, vector_id, vector=None, payload=None):
        existing = self.store.get_fact(str(vector_id))
        if not existing:
            return
        merged = dict(existing.payload)
        if payload:
            merged.update(payload)
        row = self.store.put_fact(
            existing.fact_id,
            existing.username,
            existing.graphname,
            existing.conversation_id,
            str(merged.get("data") or existing.text),
            merged,
        )
        if vector is not None:
            self.embedding_store_resolver(
                row.graphname
            ).upsert_memory_embedding(row.index_id, vector)
            self.store.mark_fact_indexed(row.fact_id)

    def get(self, vector_id):
        row = self.store.get_fact(str(vector_id))
        if not row:
            return None
        return MemoryVectorResult(row.fact_id, 1.0, dict(row.payload))

    def list(self, filters=None, top_k=None):
        username, graphname, run_id = self._scope(filters or {})
        rows = self.store.list_facts(username, graphname, run_id)
        if top_k is not None:
            rows = rows[:top_k]
        return [[
            MemoryVectorResult(row.fact_id, 1.0, dict(row.payload)) for row in rows
        ]]

    def list_cols(self):
        return ["memory_facts"]

    def delete_col(self):
        raise RuntimeError("Global memory collection deletion is disabled")

    def col_info(self):
        return {"name": "memory_facts"}

    def reset(self):
        raise RuntimeError("Global memory reset is disabled")
