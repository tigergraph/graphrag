# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""GraphRAG tools for the agentic engine.

Thin wrappers that expose the existing chat-workflow capabilities —
structural query generation, the four unstructured retrievers, and
schema introspection — as uniform, agent-callable tools. Each returns a
plain dict ``{ok, summary, context, citations}`` that the executor lifts
into a ``StepResult``.

Retrieval parameters (`top_k`, `num_hops`, `community_level`, …) are
accepted per call and default to the graph's ``graphrag_config`` values,
clamped by ``tool_guards``. Execution runs through the per-user
``conn`` — the agent acts as the logged-in user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from tools import tool_guards as guards
from tools.validation_utils import MapQuestionToSchemaException

logger = logging.getLogger(__name__)


@dataclass
class GraphRAGToolContext:
    """Per-request handles the GraphRAG tools operate against.

    Built once per question (mirrors what ``TigerGraphAgentGraph`` holds)
    and passed to every tool call so the tools stay stateless functions.
    """

    conn: Any                      # per-user TigerGraphConnection(Proxy)
    llm_provider: Any              # resolved chat LLM_Model
    embedding_model: Any
    embedding_store: Any
    mq2s: Any                      # MapQuestionToSchema instance
    gen_func: Any                  # GenerateFunction instance
    graphrag_cfg: dict
    cypher_gen: Optional[Any] = None     # GenerateCypher (when use_cypher)
    use_cypher: bool = False
    conversation: Optional[list] = None
    progress: Optional[Callable[[str], None]] = None
    tg_connection_config: Optional[dict] = None   # per-user creds for tg-mcp tools
    # External MCP-addon tools discovered for this request and the manager
    # that dispatches them. Populated by the agent setup; consumed by
    # ``tool_registry`` (catalog / run / lc_tools_spec). Empty when no
    # external MCP servers are configured for the graph.
    external_tools: Dict[str, Any] = field(default_factory=dict)
    mcp_manager: Optional[Any] = None
    user: Optional[str] = None       # logged-in user, for MCP per-call _meta

    def emit(self, msg: str) -> None:
        if self.progress is not None:
            try:
                self.progress(msg)
            except Exception:
                pass


def _ok(summary: str, context: Any, citations: Optional[list] = None) -> dict:
    return {"ok": True, "summary": summary, "context": context, "citations": citations or []}


def _empty(summary: str) -> dict:
    return {"ok": False, "summary": summary, "context": None, "citations": []}


def _result_is_empty(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, (list, dict, str)) and len(result) == 0:
        return True
    return False


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def get_schema(ctx: GraphRAGToolContext) -> dict:
    """Return a compact, LLM-ready rendering of the live graph schema
    (vertex types + attributes, edge types + endpoints, domain
    definitions). Feeds the planner so it can decide which structural /
    unstructured steps a question needs.
    """
    ctx.emit("Reading the graph schema")
    from common.db.schema_utils import render_schema_rep
    try:
        rep = render_schema_rep(ctx.conn)
        return _ok(
            f"schema v{rep.schema_version}: "
            f"{len(rep.vertex_types)} vertex / {len(rep.edge_types)} edge types",
            {
                "schema_rep": rep.schema_rep,
                "vertex_types": rep.vertex_types,
                "edge_types": rep.edge_types,
                "schema_version": rep.schema_version,
            },
        )
    except Exception as exc:
        logger.warning(f"get_schema failed: {exc}")
        return _empty(f"schema unavailable: {exc}")


# --------------------------------------------------------------------------
# Structural retrieval (dynamic query generation, executed via conn)
# --------------------------------------------------------------------------

def structural_retrieve(ctx: GraphRAGToolContext, question: str) -> dict:
    """Answer a question against the structured graph by generating and
    executing a dynamic query. Maps the question to concrete schema
    elements, then generates a pyTigerGraph function call (or, when
    ``use_cypher``, an openCypher query) and executes it through the
    per-user connection. Returns the structured rows.
    """
    ctx.emit("Mapping the question to the schema")
    try:
        mapping = ctx.mq2s._run(question, ctx.conversation)
    except MapQuestionToSchemaException as exc:
        return _empty(f"question does not map to the schema: {exc}")
    except Exception as exc:
        logger.warning(f"structural_retrieve mq2s failed: {exc}")
        return _empty(f"schema mapping failed: {exc}")

    ctx.emit("Generating a query to answer the question")
    try:
        step = ctx.gen_func._run(
            question,
            mapping.target_vertex_types,
            mapping.target_vertex_attributes,
            mapping.target_vertex_ids,
            mapping.target_edge_types,
            mapping.target_edge_attributes,
        )
    except Exception as exc:
        logger.warning(f"structural_retrieve generate_function failed: {exc}")
        step = None

    result = step.get("result") if isinstance(step, dict) else None
    if not _result_is_empty(result):
        return _ok("structural query returned rows", step)

    # Optional cypher fallback when configured and available.
    if ctx.use_cypher and ctx.cypher_gen is not None:
        cy = _cypher_retrieve(ctx, question)
        if cy["ok"]:
            return cy

    return _empty("structural query returned no rows")


def _cypher_retrieve(ctx: GraphRAGToolContext, question: str) -> dict:
    import json
    ctx.emit("Generating a graph query")
    gen_history: list = []
    for i in range(3):
        try:
            cypher = ctx.cypher_gen._run(question, gen_history)
        except ValueError as exc:
            gen_history.append(f"{i}: Error: {exc}\n")
            continue
        response = ctx.conn.gsql(cypher)
        json_str = "\n".join(response.split("\n")[1:])
        try:
            parsed = json.loads(json_str)
        except Exception:
            gen_history.append(f"{i}: {cypher}\n\tError: {json_str}\n")
            continue
        rows = parsed.get("results", [{}])
        first = rows[0] if rows else None
        if not _result_is_empty(first):
            return _ok(
                "graph query returned rows",
                {"result": first, "cypher": cypher,
                 "reasoning": f"The following openCypher query was executed:\n{cypher}"},
            )
    return _empty("graph query returned no rows after retries")


# --------------------------------------------------------------------------
# Unstructured retrieval (agent-tunable params, clamped by tool_guards)
# --------------------------------------------------------------------------

def hybrid_search(
    ctx: GraphRAGToolContext,
    question: str,
    top_k: Optional[int] = None,
    num_hops: Optional[int] = None,
    chunk_only: Optional[bool] = None,
    similarity_threshold: Optional[float] = None,
    max_results: Optional[int] = None,
) -> dict:
    """Hybrid vector + graph-expansion search over document chunks.
    Good for questions needing supporting passages plus related context.
    """
    from supportai.retrievers import HybridRetriever
    cfg = ctx.graphrag_cfg
    ctx.emit("Running hybrid search")
    retriever = HybridRetriever(
        ctx.embedding_model, ctx.embedding_store, ctx.llm_provider, ctx.conn
    )
    step = retriever.search(
        question,
        indices=["DocumentChunk"],
        top_k=guards.clamp_top_k(top_k, cfg.get("top_k", 5)),
        num_seen_min=cfg.get("num_seen_min", 2),
        num_hops=guards.clamp_num_hops(num_hops, cfg.get("num_hops", 2)),
        chunk_only=cfg.get("chunk_only", True) if chunk_only is None else chunk_only,
        doc_only=cfg.get("doc_only", False),
        max_results=max_results or 0,  # 0 -> retriever resolves from graphrag_config
    )
    return _unstructured_result("GraphRAG_Hybrid_Vector_Search", step)


def similarity_search(
    ctx: GraphRAGToolContext, question: str, top_k: Optional[int] = None
) -> dict:
    """Point vector-similarity search over document chunks. Best for
    direct lookups where the answer is in one passage.
    """
    from supportai.retrievers import SimilarityRetriever
    ctx.emit("Running similarity search")
    retriever = SimilarityRetriever(
        ctx.embedding_model, ctx.embedding_store, ctx.llm_provider, ctx.conn
    )
    step = retriever.search(
        question, index="DocumentChunk",
        top_k=guards.clamp_top_k(top_k, ctx.graphrag_cfg.get("top_k", 5)),
    )
    return _unstructured_result("Content_Similarity_Vector_Search", step)


def contextual_search(
    ctx: GraphRAGToolContext, question: str, top_k: Optional[int] = None
) -> dict:
    """Sibling/contextual search — retrieves matching chunks plus their
    sibling chunks from the same document for surrounding context.
    """
    from supportai.retrievers import SiblingRetriever
    ctx.emit("Running contextual search")
    retriever = SiblingRetriever(
        ctx.embedding_model, ctx.embedding_store, ctx.llm_provider, ctx.conn
    )
    step = retriever.search(
        question, index="DocumentChunk",
        top_k=guards.clamp_top_k(top_k, ctx.graphrag_cfg.get("top_k", 5)),
    )
    return _unstructured_result("Chunk_Sibling_Vector_Search", step)


def community_search(
    ctx: GraphRAGToolContext,
    question: str,
    top_k: Optional[int] = None,
    community_level: Optional[int] = None,
    with_chunk: Optional[bool] = None,
    max_results: Optional[int] = None,
) -> dict:
    """Community-summary search — retrieves thematic community summaries
    (and optionally their chunks). Best for broad / aggregative questions.
    """
    from supportai.retrievers import CommunityRetriever
    cfg = ctx.graphrag_cfg
    ctx.emit("Running community search")
    retriever = CommunityRetriever(
        ctx.embedding_model, ctx.embedding_store, ctx.llm_provider, ctx.conn
    )
    step = retriever.search(
        question,
        community_level=guards.clamp_community_level(community_level, cfg.get("community_level", 2)),
        top_k=guards.clamp_top_k(top_k, cfg.get("top_k", 5)),
        with_chunk=cfg.get("with_chunk", True) if with_chunk is None else with_chunk,
        max_results=max_results or 0,  # 0 -> retriever resolves from config / top_k*2 floor
    )
    return _unstructured_result("GraphRAG_Community_Vector_Search", step)


def _unstructured_result(query_name: str, step) -> dict:
    result = step[0] if isinstance(step, (list, tuple)) and step else step
    if _result_is_empty(result):
        return _empty(f"{query_name} returned no chunks")
    n = len(result) if hasattr(result, "__len__") else "?"
    return _ok(f"{query_name} returned {n} item(s)",
               {"function_call": query_name, "result": result})


# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------

def combine_context(ctx: GraphRAGToolContext, parts: list[dict]) -> dict:
    """Normalize, dedupe, and order a set of step contexts into a single
    context block for the synthesizer. ``parts`` is a list of the
    ``context`` payloads from prior structural / unstructured steps.
    """
    structural, unstructured = [], []
    for p in parts or []:
        if not p:
            continue
        if "function_call" in p and "Vector_Search" in str(p.get("function_call", "")):
            unstructured.append(p)
        else:
            structural.append(p)
    return _ok(
        f"combined {len(structural)} structural + {len(unstructured)} unstructured context(s)",
        {"structural": structural, "unstructured": unstructured},
    )
