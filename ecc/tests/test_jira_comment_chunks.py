from __future__ import annotations

import pytest

from common.embeddings.tigergraph_embedding_store import (
    TigerGraphEmbeddingStore,
)
from graphrag import workers


@pytest.mark.asyncio
async def test_jira_comment_chunk_links_comment_and_issue(monkeypatch):
    captured: dict = {}

    async def capture_group(conn, vertices, edges):
        captured["vertices"] = vertices
        captured["edges"] = edges

    monkeypatch.setattr(workers.util, "upsert_group", capture_group)

    await workers.upsert_chunk(
        object(),
        "jira:cloud-1:issue:10422:comment-doc:9001",
        "chunk-1",
        "Short Jira comment",
        0,
        "jira_comment",
    )

    assert (
        "DocumentChunk",
        "chunk-1",
        "CONTAINS_ENTITY",
        "JiraIssue",
        "jira:cloud-1:issue:10422",
        None,
    ) in captured["edges"]
    assert (
        "DocumentChunk",
        "chunk-1",
        "CONTAINS_ENTITY",
        "JiraComment",
        "jira:cloud-1:comment:9001",
        None,
    ) in captured["edges"]


def test_exhausted_embedding_retries_raise():
    provider_error = RuntimeError("500 INTERNAL")

    with pytest.raises(RuntimeError, match="Failed to embed chunk-1"):
        TigerGraphEmbeddingStore._log_embed_failure(
            "chunk-1",
            provider_error,
        )


@pytest.mark.asyncio
async def test_embedding_worker_propagates_store_failure():
    class FailingStore:
        async def aadd_embeddings(self, *args, **kwargs):
            raise RuntimeError("embedding provider unavailable")

    workers.util.loading_event.set()

    with pytest.raises(RuntimeError, match="embedding provider unavailable"):
        await workers.embed(
            object(),
            FailingStore(),
            ("chunk-1", "DocumentChunk"),
            "chunk content",
        )
