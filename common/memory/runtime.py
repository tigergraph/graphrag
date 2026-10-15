# Copyright (c) 2024-2026 TigerGraph, Inc.
"""Pinned in-process mem0 runtime using GraphRAG storage adapters."""

from __future__ import annotations

import importlib.metadata

from common.config import get_chat_config, get_embedding_service, get_llm_service
from common.memory.mem0_adapter import PostgresHistoryAdapter
from common.memory.store import PostgresMemoryStore, memory_user_id

MEM0_VERSION = "2.1.0"


class GraphRAGMem0Embedder:
    """Reuse GraphRAG's configured embedding client inside mem0."""

    def __init__(self, config=None):
        self.config = config
        self.embedding_service = get_embedding_service()

    def embed(self, text: str, memory_action=None):
        return self.embedding_service.embed_query(text)

    def embed_batch(self, texts, memory_action=None):
        if not texts:
            return []
        if isinstance(texts, str):
            return [self.embed(texts, memory_action)]
        return self.embedding_service.embed_documents(list(texts))


class GraphRAGMem0LLM:
    """mem0 LLMBase stand-in that calls GraphRAG's langchain chat model.

    mem0's bundled LangchainLLM imports the uninstalled ``langchain`` meta
    package. This adapter uses the already-constructed GraphRAG model
    (AFC-disabled for Gemini) and speaks mem0's generate_response contract.
    """

    def __init__(self, config=None):
        self.config = config
        self.langchain_model = getattr(config, "model", None)
        if self.langchain_model is None:
            raise ValueError("GraphRAG mem0 LLM requires the GraphRAG chat model")

    def generate_response(
        self,
        messages,
        response_format=None,
        tools=None,
        tool_choice="auto",
        **kwargs,
    ):
        langchain_messages = []
        for message in messages:
            role = message["role"]
            content = message["content"]
            if role == "system":
                langchain_messages.append(("system", content))
            elif role == "user":
                langchain_messages.append(("human", content))
            elif role == "assistant":
                langchain_messages.append(("ai", content))
        if not langchain_messages:
            raise ValueError("No valid messages found in the messages list")

        model = self.langchain_model
        if tools:
            model = model.bind_tools(tools=tools, tool_choice=tool_choice)
        response = model.invoke(langchain_messages, **kwargs)
        if not tools:
            return response.content
        return {
            "content": response.content,
            "tool_calls": [
                {"name": call["name"], "arguments": call["args"]}
                for call in getattr(response, "tool_calls", []) or []
            ],
        }


class GraphRAGVectorConfig:
    def __init__(
        self,
        *,
        store,
        embedding_store_resolver,
        collection_name="graphrag_memory",
    ):
        self.store = store
        self.embedding_store_resolver = embedding_store_resolver
        self.collection_name = collection_name

    def model_dump(self):
        return {
            "store": self.store,
            "embedding_store_resolver": self.embedding_store_resolver,
            "collection_name": self.collection_name,
        }


class Mem0Runtime:
    """Uses mem0 public operations; only persistence is adapted."""

    def __init__(
        self,
        graphname: str,
        store: PostgresMemoryStore,
        embedding_store_resolver,
    ):
        installed = importlib.metadata.version("mem0ai")
        if installed != MEM0_VERSION:
            raise RuntimeError(
                f"Unsupported mem0ai {installed}; expected pinned {MEM0_VERSION}"
            )
        from mem0.configs.base import MemoryConfig
        from mem0.configs.llms.base import BaseLlmConfig
        from mem0.memory.main import Memory
        import mem0.memory.main as mem0_main
        from mem0.utils.factory import EmbedderFactory, LlmFactory, VectorStoreFactory

        chat = get_chat_config(graphname)
        # mem0's native gemini client enables AFC. Reuse GraphRAG's langchain
        # wrapper so extract/search share the same AFC-disabled model.
        # mem0's own "langchain" provider imports the uninstalled langchain
        # meta package, so the factory slot is pointed at GraphRAGMem0LLM.
        langchain_model = get_llm_service(chat).llm
        LlmFactory.provider_to_class["langchain"] = (
            "common.memory.runtime.GraphRAGMem0LLM",
            BaseLlmConfig,
        )

        config = MemoryConfig(
            **{
                "version": "v1.1",
                "history_db_path": ":memory:",
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": "graphrag_memory_bootstrap",
                        "path": "/tmp/graphrag-memory-bootstrap",
                    },
                },
                "llm": {
                    "provider": "langchain",
                    "config": {"model": langchain_model},
                },
                "embedder": {"provider": "openai", "config": {}},
                "custom_instructions": (
                    "Store only durable user facts, preferences, decisions, and goals. "
                    "Never store credentials, secrets, API keys, transient greetings, "
                    "copied graph evidence, or unsupported claims made by the assistant."
                ),
            }
        )
        VectorStoreFactory.provider_to_class["graphrag"] = (
            "common.memory.mem0_adapter.SplitMemoryVectorStore"
        )
        EmbedderFactory.provider_to_class["graphrag"] = (
            "common.memory.runtime.GraphRAGMem0Embedder"
        )
        config.vector_store.provider = "graphrag"
        config.vector_store.config = GraphRAGVectorConfig(
            store=store,
            embedding_store_resolver=embedding_store_resolver,
        )
        config.embedder.provider = "graphrag"
        config.embedder.config = {}
        # mem0 telemetry creates a second vector store; GraphRAG already has
        # application-level metrics and must not duplicate the memory index.
        mem0_main.MEM0_TELEMETRY = False
        self.memory = Memory(config)
        old_db = self.memory.db
        self.memory.db = PostgresHistoryAdapter(store)
        old_db.close()
        self.graphname = graphname

    def add(self, messages: list[dict], username: str, conversation_id: str):
        return self.memory.add(
            messages,
            user_id=memory_user_id(username, self.graphname),
            run_id=conversation_id,
            infer=True,
        )

    def search(self, query: str, username: str, *, top_k: int, threshold: float):
        return self.memory.search(
            query,
            top_k=top_k,
            threshold=threshold,
            filters={"user_id": memory_user_id(username, self.graphname)},
        )
