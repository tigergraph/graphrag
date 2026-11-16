# Copyright (c) 2024-2026 TigerGraph, Inc.
"""Single application service for transcript, STM compaction, and mem0 LTM."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import defaultdict

from common.config import (
    get_chat_config,
    get_embedding_service,
    get_embedding_store,
    get_llm_service,
    get_memory_config,
)
from common.memory.compaction import message_text, select_compaction_range, token_count
from common.memory.runtime import Mem0Runtime
from common.memory.store import PostgresMemoryStore

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(
    r"(?:api[_ -]?key|password|secret|bearer\s+[A-Za-z0-9._-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

_stores: dict[str, PostgresMemoryStore] = {}
_runtimes: dict[tuple[str, str], Mem0Runtime] = {}
_global_lock = threading.Lock()
_compaction_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class MemoryService:
    def __init__(self, graphname: str):
        self.graphname = graphname
        self.config = get_memory_config(graphname)
        if not self.config["enabled"]:
            raise RuntimeError("Agent memory is not enabled")
        database_url = self.config["database_url"]
        with _global_lock:
            store = _stores.get(database_url)
            if store is None:
                store = PostgresMemoryStore(database_url)
                _stores[database_url] = store
            self.store = store

    @classmethod
    def enabled(cls, graphname: str) -> bool:
        return bool(get_memory_config(graphname)["enabled"])

    def _runtime(self) -> Mem0Runtime:
        key = (self.config["database_url"], self.graphname)
        with _global_lock:
            runtime = _runtimes.get(key)
            if runtime is None:
                runtime = Mem0Runtime(
                    self.graphname, self.store, get_embedding_store
                )
                _runtimes[key] = runtime
            return runtime

    def list_conversations(self, username: str) -> list[dict]:
        return self.store.list_conversations(username, self.graphname)

    def get_messages(self, username: str, conversation_id: str) -> list[dict]:
        return self.store.get_messages(username, self.graphname, conversation_id)

    def save_message(self, username: str, message: dict) -> dict:
        return self.store.save_message(username, self.graphname, message)

    def feedback_messages(self, username: str, include_all: bool) -> list[dict]:
        return self.store.feedback_messages(
            username, self.graphname, include_all=include_all
        )

    def delete_conversation(self, username: str, conversation_id: str) -> None:
        self.store.delete_conversation(username, self.graphname, conversation_id)
        self.flush_outbox()

    def history_pairs(self, username: str, conversation_id: str) -> list[dict]:
        messages = self.get_messages(username, conversation_id)
        by_parent = {
            item.get("parent_id"): item
            for item in messages
            if item.get("role") == "system" and item.get("parent_id")
        }
        return [
            {
                "query": item.get("content", ""),
                "response": by_parent[item["message_id"]].get("content", ""),
                "create_ts": by_parent[item["message_id"]].get("create_ts"),
                "update_ts": by_parent[item["message_id"]].get("update_ts"),
            }
            for item in messages
            if item.get("role") == "user" and item.get("message_id") in by_parent
        ]

    async def compact_if_needed(self, username: str, conversation_id: str) -> bool:
        lock = _compaction_locks[
            f"{self.graphname}\0{username}\0{conversation_id}"
        ]
        async with lock:
            summary, messages = self.store.unsummarized_messages(
                username, self.graphname, conversation_id
            )
            old_summary = summary.summary if summary else ""
            selected = select_compaction_range(
                old_summary,
                messages,
                budget_tokens=self.config["stm_context_tokens"],
            )
            if not selected:
                return False
            selected_text = message_text(selected)
            prompt = (
                "Update the rolling conversation summary. Preserve user goals, "
                "decisions, unresolved questions, referenced entities, and constraints. "
                "Do not invent facts. Return only the updated summary.\n\n"
                f"Existing summary:\n{old_summary or '(none)'}\n\n"
                f"New conversation range:\n{selected_text}"
            )
            llm = get_llm_service(get_chat_config(self.graphname))
            response = await asyncio.to_thread(llm.llm.invoke, prompt)
            summary_text = llm._message_text(response).strip()
            self.store.update_summary(
                username,
                self.graphname,
                conversation_id,
                summary_text=summary_text,
                through_sequence=selected[-1].sequence,
                source_tokens=token_count(selected_text),
                summary_tokens=token_count(summary_text),
                model=str(get_chat_config(self.graphname).get("llm_model", "")),
            )
            return True

    def short_term_context(self, username: str, conversation_id: str) -> str:
        summary, messages = self.store.unsummarized_messages(
            username, self.graphname, conversation_id
        )
        # The in-flight user turn is already the agent question. Feeding it
        # back as STM makes the planner treat the question as "provided
        # context" and skip graph retrieval.
        if messages and getattr(messages[-1], "role", None) == "user":
            messages = messages[:-1]
        sections = []
        if summary and summary.summary:
            sections.append("Conversation summary:\n" + summary.summary)
        budget = self.config["stm_context_tokens"] - sum(
            token_count(section) for section in sections
        )
        selected = []
        for item in reversed(messages):
            rendered = f"{item.role}: {item.content}"
            tokens = token_count(rendered)
            if selected and tokens > budget:
                break
            if tokens <= budget:
                selected.append(rendered)
                budget -= tokens
        if selected:
            sections.append("Recent exact turns:\n" + "\n".join(reversed(selected)))
        return "\n\n".join(sections)

    def recall(self, username: str, question: str) -> list[dict]:
        if self.store.expire_facts(username, self.graphname):
            self.flush_outbox()
        # LTM is user+graph, not conversation. Skip mem0 entirely when this
        # scope has never stored a fact — first chat in a new graph must not
        # pay Mem0Runtime + TigerGraph index startup for an empty search.
        if not self.store.has_facts(username, self.graphname):
            return []
        result = self._runtime().search(
            question,
            username,
            top_k=self.config["ltm_top_k"],
            threshold=self.config["ltm_score_threshold"],
        )
        return result.get("results", [])

    def memory_context(
        self, username: str, conversation_id: str, question: str
    ) -> str:
        sections = []
        stm = self.short_term_context(username, conversation_id)
        if stm:
            sections.append(stm)
        try:
            facts = self.recall(username, question)
        except Exception as exc:
            logger.warning("Long-term memory recall unavailable: %s", exc)
            facts = []
        if facts:
            sections.append(
                "Relevant long-term user memory (not graph evidence):\n"
                + "\n".join(f"- {item.get('memory', '')}" for item in facts)
            )
        return "\n\n".join(sections)

    def extract(self, username: str, conversation_id: str, messages: list[dict]):
        safe_messages = [
            message
            for message in messages
            if not _SECRET_PATTERN.search(str(message.get("content") or ""))
        ]
        if not safe_messages:
            return {"results": []}
        result = self._runtime().add(safe_messages, username, conversation_id)
        self.store.apply_retention(
            username, self.graphname, self.config["ltm_retention_days"]
        )
        self.store.enforce_fact_cap(
            username, self.graphname, self.config["max_facts_per_scope"]
        )
        self.flush_outbox()
        return result

    def flush_outbox(self) -> None:
        for item in self.store.pending_outbox():
            fact = self.store.get_fact(item.fact_id, include_deleted=True)
            try:
                if item.operation == "delete":
                    if fact:
                        get_embedding_store(
                            fact.graphname
                        ).delete_memory_embedding(fact.index_id)
                    self.store.complete_outbox(item.id, indexed=False)
                elif fact:
                    if not fact.indexed:
                        vector = get_embedding_service().embed_query(fact.text)
                        get_embedding_store(
                            fact.graphname
                        ).upsert_memory_embedding(fact.index_id, vector)
                    self.store.complete_outbox(item.id, indexed=True)
            except Exception as exc:
                logger.warning("Memory index outbox retry failed: %s", exc)
                self.store.fail_outbox(item.id, str(exc))
