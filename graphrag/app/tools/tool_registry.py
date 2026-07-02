# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool registry for the agentic engine.

Catalogs the read-only GraphRAG tools the planner may use and dispatches
calls by name. Each tool carries a pydantic argument schema that doubles
as (a) the catalog the planner is prompted with and (b) per-call argument
validation. The registry is the single place that maps a planned step's
``tool`` name to a callable; nothing outside this catalog is runnable, so
the agent's tool surface is read-only by construction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, Field

from tools import graphrag_tools as gt
from tools.graphrag_tools import GraphRAGToolContext

logger = logging.getLogger(__name__)


# --- LLM-facing argument schemas -------------------------------------------

class GetSchemaArgs(BaseModel):
    """No arguments — returns the live graph schema for planning."""
    pass


class StructuralRetrieveArgs(BaseModel):
    question: str = Field(description="The (possibly sub-) question to answer from structured graph data via a generated query.")


class HybridSearchArgs(BaseModel):
    question: str = Field(description="The query to search for.")
    top_k: Optional[int] = Field(default=None, description="Max chunks to return; raise on thin results.")
    num_hops: Optional[int] = Field(default=None, description="Graph-expansion hops from matched chunks; raise to widen context.")
    chunk_only: Optional[bool] = Field(default=None, description="Return only chunks (not parent documents).")
    similarity_threshold: Optional[float] = Field(default=None, description="Min cosine similarity [0-1]; lower to broaden recall.")


class SimilaritySearchArgs(BaseModel):
    question: str = Field(description="The query to search for.")
    top_k: Optional[int] = Field(default=None, description="Max chunks to return; raise on thin results.")


class ContextualSearchArgs(BaseModel):
    question: str = Field(description="The query to search for.")
    top_k: Optional[int] = Field(default=None, description="Max chunks to return; raise on thin results.")


class CommunitySearchArgs(BaseModel):
    question: str = Field(description="The query to search for.")
    top_k: Optional[int] = Field(default=None, description="Max community summaries to return.")
    community_level: Optional[int] = Field(default=None, description="Community hierarchy level; higher = broader themes.")
    with_chunk: Optional[bool] = Field(default=None, description="Also return chunks linked to the communities.")


class TgRunQueryArgs(BaseModel):
    query_text: str = Field(description="A complete interpreted GSQL query body to execute (read-only).")


class TgGetNeighborsArgs(BaseModel):
    vertex_type: str = Field(description="Vertex type of the starting vertex.")
    vertex_id: str = Field(description="Id of the starting vertex.")
    edge_type: Optional[str] = Field(default=None, description="Restrict expansion to this edge type.")
    target_vertex_type: Optional[str] = Field(default=None, description="Restrict neighbors to this vertex type.")
    limit: Optional[int] = Field(default=None, description="Max neighbors to return.")


@dataclass
class ToolSpec:
    """One callable tool the planner / react loop can dispatch.

    Built-in tools supply a pydantic ``args_model``; externally-loaded
    tools (e.g. those discovered from an MCP server) carry the JSON
    Schema directly in ``args_schema_json``. Exactly one of the two is
    set; the registry's ``catalog`` / ``run`` / ``lc_tools_spec`` branch
    on which is present.
    """
    name: str
    description: str
    args_model: Optional[Type[BaseModel]] = None
    args_schema_json: Optional[dict] = None
    fn: Optional[Callable[..., dict]] = None


# --- Registry ---------------------------------------------------------------

_TOOLS: dict[str, ToolSpec] = {}


def _register(name: str, description: str, args_model: Type[BaseModel], fn: Callable) -> None:
    _TOOLS[name] = ToolSpec(name=name, description=description, args_model=args_model, fn=fn)


def _spec_args_schema(spec: ToolSpec) -> dict:
    """Return the JSON Schema for ``spec``'s args, whichever form it's in."""
    if spec.args_model is not None:
        return spec.args_model.model_json_schema()
    return spec.args_schema_json or {}


def _passthrough_args_model(name: str, schema: dict):
    """A pydantic model that accepts any kwargs, but reports ``schema``
    as its JSON schema. Used to wrap externally-defined tools for
    LangChain's ``StructuredTool`` (which requires a pydantic class).
    The strict JSON-Schema validation happens in ``run()`` via
    ``jsonschema.validate``; the pydantic class is only a vessel.
    """
    from pydantic import ConfigDict
    safe = name.replace(".", "_").replace("-", "_") or "ext"
    frozen_schema = dict(schema or {})

    @classmethod
    def _override(cls, core_schema, handler):  # type: ignore[override]
        return frozen_schema

    cls = type(
        f"{safe}__Args",
        (BaseModel,),
        {
            "model_config": ConfigDict(extra="allow"),
            "__get_pydantic_json_schema__": _override,
        },
    )
    return cls


def _ctx_external_tools(ctx) -> dict:
    """Per-request external tools attached to ``ctx`` (e.g. by the agent
    after MCP discovery). Returns an empty dict when none are set.
    """
    if ctx is None:
        return {}
    return getattr(ctx, "external_tools", None) or {}


def _merged_specs(ctx) -> dict:
    """Built-ins overlaid with ``ctx.external_tools`` (external names win
    only via the namespaced ``<server>.<tool>`` form, so collisions with
    built-ins like ``graphrag__hybrid_search`` cannot happen in practice).
    """
    if ctx is None:
        return dict(_TOOLS)
    merged = dict(_TOOLS)
    merged.update(_ctx_external_tools(ctx))
    return merged


_register(
    "graphrag__get_schema",
    "Return the live graph schema (vertex/edge types, attributes, endpoints). "
    "Call first when you need to decide which structural queries are possible.",
    GetSchemaArgs, gt.get_schema,
)
_register(
    "graphrag__structural_retrieve",
    "Answer a sub-question from structured graph data by generating and executing "
    "a dynamic query (counts, lookups, relationships, aggregations).",
    StructuralRetrieveArgs, gt.structural_retrieve,
)
_register(
    "graphrag__hybrid_search",
    "Vector + graph-expansion search over document text. Use for questions needing "
    "supporting passages plus related context.",
    HybridSearchArgs, gt.hybrid_search,
)
_register(
    "graphrag__similarity_search",
    "Point vector search over document text. Use for direct lookups answerable from a single passage.",
    SimilaritySearchArgs, gt.similarity_search,
)
_register(
    "graphrag__contextual_search",
    "Vector search that also returns sibling chunks for surrounding context.",
    ContextualSearchArgs, gt.contextual_search,
)
_register(
    "graphrag__community_search",
    "Search thematic community summaries. Use for broad / aggregative / 'overall' questions.",
    CommunitySearchArgs, gt.community_search,
)

# tigergraph-mcp read tools — registered only when the package is
# installed (import-guarded). These run as the logged-in user via the
# per-request connection shim in tg_mcp_tools.
try:
    from tools import tg_mcp_tools as tgm
    if tgm.AVAILABLE:
        _register(
            "tg_run_query",
            "Execute an interpreted (dynamic) read-only GSQL query against the graph "
            "and return the rows. Use when a precise graph query is needed.",
            TgRunQueryArgs, tgm.tg_run_query,
        )
        _register(
            "tg_get_neighbors",
            "Return the neighbors of a given vertex (optionally filtered by edge / target "
            "type) without writing GSQL.",
            TgGetNeighborsArgs, tgm.tg_get_neighbors,
        )
        logger.info("tigergraph-mcp read tools registered")
except Exception as exc:  # pragma: no cover - import guard
    logger.info(f"tigergraph-mcp tools not registered: {exc}")


def tool_names(ctx=None) -> list[str]:
    return list(_merged_specs(ctx).keys())


def get_spec(name: str, ctx=None) -> Optional[ToolSpec]:
    return _merged_specs(ctx).get(name)


def catalog(ctx=None) -> list[dict]:
    """Planner-facing catalog: name, description, and JSON-schema of args.

    When ``ctx`` carries ``external_tools`` (per-request externals
    discovered from configured MCP servers), they appear in the catalog
    alongside the built-ins.
    """
    out = []
    for spec in _merged_specs(ctx).values():
        out.append({
            "name": spec.name,
            "description": spec.description,
            "args_schema": _spec_args_schema(spec),
        })
    return out


def _safe_tool_name(name: str) -> str:
    """Tool name accepted by chat-model function-calling APIs, which require
    ``^[a-zA-Z0-9_-]+$``. Built-in names (``graphrag__*``) already comply and
    pass through unchanged; external MCP tools use a ``<server>.<tool>``
    namespace whose ``.`` is illegal, so any run of invalid chars collapses to
    ``__``. ``run()`` resolves the safe name back to the real spec on dispatch.
    """
    return re.sub(r"[^a-zA-Z0-9_-]+", "__", name)


def lc_tools_spec(ctx=None) -> list:
    """LangChain ``StructuredTool`` list for ``bind_tools(...)``.

    Tool names are sanitized to the chat-model name pattern (see
    ``_safe_tool_name``); the model's emitted ``tool_calls`` carry the safe
    name, which ``run(name, ...)`` resolves back to the real spec. The wrapped
    functions are placeholders — the react loop intercepts tool_calls and
    dispatches through ``run`` so the per-user ``ctx`` is available; LangChain
    never actually invokes them.
    """
    from langchain_core.tools import StructuredTool

    def _noop(**_):  # pragma: no cover — never invoked
        raise RuntimeError(
            "tool_registry.lc_tools_spec(): tool execution is handled by "
            "the agentic react loop, not LangChain"
        )

    out = []
    for spec in _merged_specs(ctx).values():
        if spec.args_model is not None:
            args_schema = spec.args_model
        else:
            args_schema = _passthrough_args_model(spec.name, spec.args_schema_json or {})
        out.append(
            StructuredTool.from_function(
                func=_noop,
                name=_safe_tool_name(spec.name),
                description=spec.description,
                args_schema=args_schema,
            )
        )
    return out


def run(name: str, args: dict, ctx: GraphRAGToolContext) -> dict:
    """Validate ``args`` against the tool's schema and invoke it.

    Returns the tool's ``{ok, summary, context, citations}`` dict, or an
    error dict for an unknown tool / invalid args (never raises to the
    executor, so one bad step can't abort the plan).
    """
    specs = _merged_specs(ctx)
    spec = specs.get(name)
    if spec is None:
        # The react path binds sanitized names (no '.'); resolve back to the
        # real spec. Exact match above keeps the planner path (real names) fast.
        for s in specs.values():
            if _safe_tool_name(s.name) == name:
                spec, name = s, s.name
                break
    if spec is None:
        logger.warning(f"unknown tool requested: {name!r}")
        return {"ok": False, "summary": f"unknown tool {name!r}", "context": None, "citations": []}

    if spec.args_model is not None:
        # Built-in: pydantic-validated args.
        try:
            validated = spec.args_model(**(args or {}))
        except Exception as exc:
            logger.warning(f"invalid args for {name}: {exc}")
            return {"ok": False, "summary": f"invalid args for {name}: {exc}", "context": None, "citations": []}
        kwargs = validated.model_dump(exclude_none=True)
    else:
        # External: JSON-Schema-validated args.
        from jsonschema import validate as _js_validate, ValidationError
        try:
            _js_validate(instance=args or {}, schema=spec.args_schema_json or {})
        except ValidationError as exc:
            logger.warning(f"invalid args for {name}: {exc.message}")
            return {"ok": False, "summary": f"invalid args for {name}: {exc.message}", "context": None, "citations": []}
        kwargs = dict(args or {})

    if spec.fn is None:
        return {"ok": False, "summary": f"{name}: no dispatcher bound", "context": None, "citations": []}
    try:
        return spec.fn(ctx, **kwargs)
    except Exception as exc:
        logger.warning(f"tool {name} raised: {exc}", exc_info=True)
        return {"ok": False, "summary": f"{name} failed: {exc}", "context": None, "citations": []}
