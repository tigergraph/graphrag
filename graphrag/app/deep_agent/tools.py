# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tools the Deep Agent can call.

Three families, built per request so each call runs as the logged-in user
(through that user's TigerGraph connection):

* GraphRAG tools — the existing retrievers and query generators: graph
  schema, natural-language graph query (generated pyTigerGraph call, with
  openCypher fallback), hybrid / similarity / contextual / community search.
* TigerGraph tools — direct, read-only pyTigerGraph calls: vertex and edge
  counts, vertex lookups, a vertex's edges, installed queries, and (when the
  deployment enables openCypher) a read-only openCypher query.
* MCP add-on tools — tools of the external MCP servers configured for the
  graph, filtered by each server's allowlist.

Every tool returns a JSON string the model reads, and records a trace entry
and the passages it retrieved in ``ToolEnv`` for the response.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_ROWS = 200
DEFAULT_ROWS = 20


# --------------------------------------------------------------------------
# Per-request environment
# --------------------------------------------------------------------------

@dataclass
class ToolEnv:
    """What the tools of one request operate on, and what they record."""

    graph_ctx: Any                      # tools.graphrag_tools.GraphRAGToolContext
    max_output_chars: int = 60000
    allow_installed_queries: bool = True
    installed_query_allowlist: List[str] = field(default_factory=lambda: ["*"])
    use_cypher: bool = False
    progress: Optional[Callable[[str], None]] = None
    trace: List[dict] = field(default_factory=list)
    retrieved: Dict[str, str] = field(default_factory=dict)   # passage id -> text

    @property
    def conn(self):
        return self.graph_ctx.conn

    def emit(self, msg: str) -> None:
        if self.progress is not None:
            try:
                self.progress(msg)
            except Exception:
                pass


def _dump(payload: Any, limit: int) -> str:
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return (text[:limit]
            + f"... [truncated: {len(text) - limit} of {len(text)} characters left out; "
              "narrow the request (filters, smaller limit/top_k) to see the rest]")


def _cap_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s[:500] if isinstance(s, str) else s
    return out


def _run_tool(env: ToolEnv, name: str, label: str, args: dict, fn: Callable[[], dict]) -> str:
    """Run one tool body, record its trace entry, and return the model-facing
    JSON. A tool never raises to the agent: errors come back as text so the
    agent can change its approach."""
    env.emit(label)
    t0 = time.time()
    try:
        out = fn()
    except Exception as exc:
        logger.warning(f"deep_agent tool {name} failed: {exc}", exc_info=True)
        out = {"ok": False, "summary": f"{name} failed: {exc}"}
    entry = {
        "tool": name, "args": _cap_args(args), "ok": bool(out.get("ok")),
        "summary": out.get("summary", ""), "duration_s": round(time.time() - t0, 3),
    }
    for key in ("query", "attempts"):
        if out.get(key):
            entry[key] = out[key]
    env.trace.append(entry)
    model_view = {k: v for k, v in out.items() if k not in ("attempts",)}
    return _dump(model_view, env.max_output_chars)


# --------------------------------------------------------------------------
# GraphRAG tools
# --------------------------------------------------------------------------

class _NoArgs(BaseModel):
    pass


class _GraphQuestion(BaseModel):
    question: str = Field(description="A standalone question about structured graph data "
                          "(counts, lookups by attribute or id, relationships, aggregations). "
                          "Resolve references like 'it' or 'that company' to full names first.")


class _SearchArgs(BaseModel):
    query: str = Field(description="Standalone search query. Quote exact labels, names, "
                       "years and units from the user's question.")
    top_k: Optional[int] = Field(default=None, description="Number of passages (default from "
                                 "the graph config). Use 10+ for tables, figures and comparisons.")


class _HybridArgs(_SearchArgs):
    num_hops: Optional[int] = Field(default=None, description="Graph-expansion hops from the "
                                    "matched passages (1-4). Raise to pull in related context.")


class _CommunityArgs(_SearchArgs):
    community_level: Optional[int] = Field(default=None, description="Community hierarchy level "
                                           "(1-5); higher levels are broader themes.")


def _passages(context: Any) -> Dict[str, str]:
    from common.utils.retrieval_stats import retrieved_entries
    result = context.get("result") if isinstance(context, dict) else None
    fr = result.get("final_retrieval") if isinstance(result, dict) else None
    if not isinstance(fr, dict):
        return {}
    return {str(k): str(v) for k, v in retrieved_entries(fr).items()}


def _search_output(env: ToolEnv, out: dict) -> dict:
    """Model view of a vector search: the passages by id, recorded for citations."""
    passages = _passages(out.get("context")) if out.get("ok") else {}
    for pid, text in passages.items():
        env.retrieved.setdefault(pid, text)
    view = {"ok": bool(out.get("ok")) and bool(passages), "summary": out.get("summary", "")}
    if passages:
        view["passages"] = passages
    elif out.get("ok") and out.get("context") is not None:
        # A retriever result without a final_retrieval map: pass it through.
        view["ok"] = True
        view["result"] = out.get("context")
    return view


def _graphrag_tools(env: ToolEnv) -> List[BaseTool]:
    from tools import graphrag_tools as gt

    ctx = env.graph_ctx

    def get_graph_schema() -> str:
        def body():
            out = gt.get_schema(ctx)
            if not out.get("ok"):
                return out
            c = out["context"]
            return {"ok": True, "summary": out["summary"], "schema": c.get("schema_rep"),
                    "vertex_types": c.get("vertex_types"), "edge_types": c.get("edge_types")}
        return _run_tool(env, "get_graph_schema", "Reading the graph schema", {}, body)

    def query_graph(question: str) -> str:
        def body():
            out = gt.structural_retrieve(ctx, question)
            view = {"ok": bool(out.get("ok")), "summary": out.get("summary", "")}
            c = out.get("context")
            if isinstance(c, dict):
                view["result"] = c.get("result")
                if c.get("reasoning"):
                    view["reasoning"] = c.get("reasoning")
            for key in ("query", "attempts"):
                if out.get(key):
                    view[key] = out[key]
            return view
        return _run_tool(env, "query_graph", "Querying the knowledge graph",
                         {"question": question}, body)

    def hybrid_search(query: str, top_k: Optional[int] = None, num_hops: Optional[int] = None) -> str:
        return _run_tool(env, "hybrid_search", "Searching the documents",
                         {"query": query, "top_k": top_k, "num_hops": num_hops},
                         lambda: _search_output(env, gt.hybrid_search(ctx, query, top_k=top_k, num_hops=num_hops)))

    def similarity_search(query: str, top_k: Optional[int] = None) -> str:
        return _run_tool(env, "similarity_search", "Searching the documents",
                         {"query": query, "top_k": top_k},
                         lambda: _search_output(env, gt.similarity_search(ctx, query, top_k=top_k)))

    def contextual_search(query: str, top_k: Optional[int] = None) -> str:
        return _run_tool(env, "contextual_search", "Searching the documents",
                         {"query": query, "top_k": top_k},
                         lambda: _search_output(env, gt.contextual_search(ctx, query, top_k=top_k)))

    def community_search(query: str, top_k: Optional[int] = None,
                         community_level: Optional[int] = None) -> str:
        return _run_tool(env, "community_search", "Searching community summaries",
                         {"query": query, "top_k": top_k, "community_level": community_level},
                         lambda: _search_output(env, gt.community_search(
                             ctx, query, top_k=top_k, community_level=community_level)))

    return [
        StructuredTool.from_function(
            get_graph_schema, name="get_graph_schema", args_schema=_NoArgs,
            description="Return the knowledge graph schema: vertex types with attributes, "
                        "edge types with endpoints, and type descriptions. Read it before "
                        "structural queries, and to answer what the knowledge base contains."),
        StructuredTool.from_function(
            query_graph, name="query_graph", args_schema=_GraphQuestion,
            description="Answer a question from structured graph data: GraphRAG maps it to the "
                        "schema, generates a query and runs it. Best for counts, lookups, "
                        "relationships and aggregations over typed data. May return no rows "
                        "when the question does not map to the schema."),
        StructuredTool.from_function(
            hybrid_search, name="hybrid_search", args_schema=_HybridArgs,
            description="Vector + graph-expansion search over the ingested documents. The best "
                        "default for questions answered from document text."),
        StructuredTool.from_function(
            similarity_search, name="similarity_search", args_schema=_SearchArgs,
            description="Plain vector search over document passages. For direct lookups "
                        "answered by a single passage."),
        StructuredTool.from_function(
            contextual_search, name="contextual_search", args_schema=_SearchArgs,
            description="Vector search that also returns neighbouring passages of the same "
                        "document. Good for tables, lists and passages that need surrounding text."),
        StructuredTool.from_function(
            community_search, name="community_search", args_schema=_CommunityArgs,
            description="Search the summaries of entity communities. For broad, thematic or "
                        "'overall' questions about the whole corpus."),
    ]


# --------------------------------------------------------------------------
# TigerGraph tools (read-only pyTigerGraph)
# --------------------------------------------------------------------------

class _VertexCountArgs(BaseModel):
    vertex_type: str = Field(default="*", description="Vertex type, or '*' for all types.")
    where: str = Field(default="", description="Optional filter, e.g. 'age>30,name=\"Bob\"'. "
                       "Requires a single vertex_type.")


class _EdgeCountArgs(BaseModel):
    edge_type: str = Field(default="*", description="Edge type, or '*' for all types.")
    source_vertex_type: str = Field(default="", description="Optional source vertex type.")
    target_vertex_type: str = Field(default="", description="Optional target vertex type.")


class _GetVerticesArgs(BaseModel):
    vertex_type: str = Field(description="Vertex type.")
    where: str = Field(default="", description="Optional filter, e.g. 'age>30'.")
    select: str = Field(default="", description="Optional comma-separated attributes to return.")
    sort: str = Field(default="", description="Optional sort, e.g. '-age' for descending.")
    limit: int = Field(default=DEFAULT_ROWS, description=f"Max vertices (1-{MAX_ROWS}).")


class _VerticesByIdArgs(BaseModel):
    vertex_type: str = Field(description="Vertex type.")
    vertex_ids: List[str] = Field(description="Primary ids of the vertices.")


class _GetEdgesArgs(BaseModel):
    source_vertex_type: str = Field(description="Type of the source vertex.")
    source_vertex_id: str = Field(description="Primary id of the source vertex.")
    edge_type: str = Field(default="", description="Optional edge type.")
    target_vertex_type: str = Field(default="", description="Optional target vertex type "
                                    "(requires edge_type).")
    where: str = Field(default="", description="Optional edge attribute filter.")
    limit: int = Field(default=DEFAULT_ROWS, description=f"Max edges (1-{MAX_ROWS}).")


class _RunInstalledArgs(BaseModel):
    query_name: str = Field(description="Name of an installed query from list_installed_queries.")
    params: Dict[str, Any] = Field(default_factory=dict, description="Query parameters.")


class _CypherArgs(BaseModel):
    cypher: str = Field(description="A read-only openCypher query (MATCH ... RETURN ...) "
                        "using the exact type and attribute names of the schema. Add LIMIT.")


def _limit(value) -> int:
    try:
        return max(1, min(int(value), MAX_ROWS))
    except (TypeError, ValueError):
        return DEFAULT_ROWS


# Installed queries GraphRAG itself uses to build and maintain the knowledge
# graph (some delete or rewrite data). The agent may never run them.
_GSQL_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common", "gsql")
_MUTATING_NAME = re.compile(r"(delete|remove|drop|clear|reset|update|upsert|insert|load|"
                            r"set_?epoch|processing|migrat|regenerat|init|create)", re.I)


def _internal_query_names() -> set:
    names = set()
    for root, _dirs, files in os.walk(_GSQL_ROOT):
        for f in files:
            if f.endswith(".gsql"):
                names.add(f[:-5].lower())
    return names


_INTERNAL_QUERIES = None


def query_is_runnable(name: str, allowlist: List[str]) -> bool:
    """Whether the agent may run the installed query ``name``: not one of
    GraphRAG's own maintenance queries, not named like a data change, and
    matching the configured allowlist."""
    global _INTERNAL_QUERIES
    if _INTERNAL_QUERIES is None:
        _INTERNAL_QUERIES = _internal_query_names()
    if not name or name.lower() in _INTERNAL_QUERIES or _MUTATING_NAME.search(name):
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in (allowlist or []))


def _installed_queries(conn) -> List[str]:
    """Names of the installed queries of the connection's graph, from its
    dynamic REST endpoints (``/query/<graph>/<name>``, in any key format)."""
    endpoints = conn.getEndpoints(dynamic=True) or {}
    pattern = re.compile(r"/query/" + re.escape(str(conn.graphname)) + r"/([A-Za-z0-9_]+)")
    names = set()
    for ep in endpoints:
        m = pattern.search(str(ep))
        if m:
            names.add(m.group(1))
    return sorted(names)


def _tigergraph_tools(env: ToolEnv) -> List[BaseTool]:
    conn = env.conn

    def get_vertex_count(vertex_type: str = "*", where: str = "") -> str:
        def body():
            res = conn.getVertexCount(vertex_type or "*", where=where or "")
            return {"ok": True, "summary": "vertex count", "result": res}
        return _run_tool(env, "get_vertex_count", "Counting vertices",
                         {"vertex_type": vertex_type, "where": where}, body)

    def get_edge_count(edge_type: str = "*", source_vertex_type: str = "",
                       target_vertex_type: str = "") -> str:
        def body():
            res = conn.getEdgeCount(edge_type or "*", sourceVertexType=source_vertex_type or "",
                                    targetVertexType=target_vertex_type or "")
            return {"ok": True, "summary": "edge count", "result": res}
        return _run_tool(env, "get_edge_count", "Counting edges",
                         {"edge_type": edge_type, "source_vertex_type": source_vertex_type,
                          "target_vertex_type": target_vertex_type}, body)

    def get_vertices(vertex_type: str, where: str = "", select: str = "", sort: str = "",
                     limit: int = DEFAULT_ROWS) -> str:
        def body():
            rows = conn.getVertices(vertex_type, select=select or "", where=where or "",
                                    limit=_limit(limit), sort=sort or "")
            return {"ok": bool(rows), "summary": f"{len(rows or [])} vertex(es)", "result": rows}
        return _run_tool(env, "get_vertices", "Looking up vertices",
                         {"vertex_type": vertex_type, "where": where, "select": select,
                          "sort": sort, "limit": limit}, body)

    def get_vertices_by_id(vertex_type: str, vertex_ids: List[str]) -> str:
        def body():
            rows = conn.getVerticesById(vertex_type, list(vertex_ids or [])[:MAX_ROWS])
            return {"ok": bool(rows), "summary": f"{len(rows or [])} vertex(es)", "result": rows}
        return _run_tool(env, "get_vertices_by_id", "Looking up vertices",
                         {"vertex_type": vertex_type, "vertex_ids": vertex_ids}, body)

    def get_edges(source_vertex_type: str, source_vertex_id: str, edge_type: str = "",
                  target_vertex_type: str = "", where: str = "", limit: int = DEFAULT_ROWS) -> str:
        def body():
            rows = conn.getEdges(source_vertex_type, source_vertex_id, edgeType=edge_type or "",
                                 targetVertexType=target_vertex_type or "", where=where or "",
                                 limit=_limit(limit))
            return {"ok": bool(rows), "summary": f"{len(rows or [])} edge(s)", "result": rows}
        return _run_tool(env, "get_edges", "Following relationships",
                         {"source_vertex_type": source_vertex_type,
                          "source_vertex_id": source_vertex_id, "edge_type": edge_type,
                          "target_vertex_type": target_vertex_type, "where": where,
                          "limit": limit}, body)

    out: List[BaseTool] = [
        StructuredTool.from_function(
            get_vertex_count, name="get_vertex_count", args_schema=_VertexCountArgs,
            description="Count vertices in TigerGraph, per type or for one type with an "
                        "optional attribute filter. Exact, cheap; prefer it for 'how many'."),
        StructuredTool.from_function(
            get_edge_count, name="get_edge_count", args_schema=_EdgeCountArgs,
            description="Count edges in TigerGraph, per type or for one type, optionally "
                        "restricted to source/target vertex types."),
        StructuredTool.from_function(
            get_vertices, name="get_vertices", args_schema=_GetVerticesArgs,
            description="Fetch vertices of one type with optional filter, selected attributes, "
                        "sort and limit."),
        StructuredTool.from_function(
            get_vertices_by_id, name="get_vertices_by_id", args_schema=_VerticesByIdArgs,
            description="Fetch vertices of one type by primary id."),
        StructuredTool.from_function(
            get_edges, name="get_edges", args_schema=_GetEdgesArgs,
            description="Fetch the edges (relationships) of one vertex, optionally of one edge "
                        "type and target vertex type."),
    ]

    if env.allow_installed_queries:
        def list_installed_queries() -> str:
            def body():
                names = [n for n in _installed_queries(conn)
                         if query_is_runnable(n, env.installed_query_allowlist)]
                described = []
                try:
                    for d in (conn.getQueryDescription(names) if names else []) or []:
                        described.append({
                            "name": d.get("queryName"),
                            "description": d.get("description", ""),
                            "parameters": [{"name": p.get("paramName"),
                                            "description": p.get("description", "")}
                                           for p in d.get("parameters") or []],
                        })
                except Exception as exc:
                    logger.info(f"deep_agent: query descriptions unavailable ({exc})")
                    described = [{"name": n} for n in names]
                return {"ok": bool(described), "summary": f"{len(described)} installed query(ies)",
                        "queries": described}
            return _run_tool(env, "list_installed_queries", "Listing installed queries", {}, body)

        def run_installed_query(query_name: str, params: Optional[Dict[str, Any]] = None) -> str:
            def body():
                if not query_is_runnable(query_name, env.installed_query_allowlist):
                    return {"ok": False, "summary": f"query {query_name!r} is not available to the agent"}
                res = conn.runInstalledQuery(query_name, params=params or {})
                return {"ok": True, "summary": f"ran {query_name}", "result": res,
                        "query": f"runInstalledQuery({query_name!r}, {json.dumps(params or {}, default=str)})"}
            return _run_tool(env, "run_installed_query", "Running an installed query",
                             {"query_name": query_name, "params": json.dumps(params or {}, default=str)},
                             body)

        out += [
            StructuredTool.from_function(
                list_installed_queries, name="list_installed_queries", args_schema=_NoArgs,
                description="List the installed (pre-written) GSQL queries of this graph with "
                            "their descriptions and parameters. Check it when a question looks "
                            "like an analytics task a query may already answer."),
            StructuredTool.from_function(
                run_installed_query, name="run_installed_query", args_schema=_RunInstalledArgs,
                description="Run one installed GSQL query listed by list_installed_queries."),
        ]

    if env.use_cypher:
        def run_cypher_query(cypher: str) -> str:
            from tools.generate_cypher import check_cypher, run_cypher, run_error_hints, strip_code_fence

            def body():
                q = strip_code_fence(cypher)
                errors = check_cypher(conn, q)
                if errors:
                    return {"ok": False, "summary": "the query was rejected", "errors": errors,
                            "query": q}
                try:
                    rows = run_cypher(conn, q)
                except Exception as exc:
                    return {"ok": False, "summary": f"the query failed: {str(exc)[:800]}",
                            "hints": run_error_hints(str(exc), q), "query": q}
                from common.utils.query_results import is_empty_result
                return {"ok": not is_empty_result(rows),
                        "summary": "no rows" if is_empty_result(rows) else "rows returned",
                        "result": rows, "query": q}
            return _run_tool(env, "run_cypher_query", "Querying the knowledge graph",
                             {"cypher": cypher}, body)

        out.append(StructuredTool.from_function(
            run_cypher_query, name="run_cypher_query", args_schema=_CypherArgs,
            description="Run your own read-only openCypher query on TigerGraph. Read "
                        "get_graph_schema first. Queries that change data are rejected. "
                        "Use it when query_graph does not return what you need."))
    return out


# --------------------------------------------------------------------------
# MCP add-on tools
# --------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "__", name)[:64]


def _mcp_tools(env: ToolEnv, external: Dict[str, Any]) -> List[BaseTool]:
    out = []
    for qualified, spec in (external or {}).items():
        if spec.fn is None:
            continue
        name = _safe_name(qualified)

        def _call(_spec=spec, _name=name, **kwargs) -> str:
            return _run_tool(env, _name, "Calling an external tool", kwargs,
                             lambda: _spec.fn(env.graph_ctx, **kwargs))

        schema = dict(spec.args_schema_json or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        out.append(StructuredTool(
            name=name, description=(spec.description or qualified)[:1024],
            args_schema=schema, func=_call,
        ))
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

GRAPHRAG_TOOL_NAMES = ("get_graph_schema", "query_graph", "hybrid_search", "similarity_search",
                       "contextual_search", "community_search")


def build_tools(env: ToolEnv, external_tools: Optional[Dict[str, Any]] = None) -> Dict[str, List[BaseTool]]:
    """All tools of one request, grouped: ``graphrag``, ``tigergraph``, ``mcp``."""
    return {
        "graphrag": _graphrag_tools(env),
        "tigergraph": _tigergraph_tools(env),
        "mcp": _mcp_tools(env, external_tools or {}),
    }
