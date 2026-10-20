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

"""Planner node for the agentic engine.

The configured chat model drafts a ``Plan`` — a small DAG of tool steps —
for a question, given the live schema and the tool catalog. Structural and
unstructured steps may each appear multiple times and in any order; a later
step can consume an earlier one via ``arg_bindings``. On replan the planner
receives the results-so-far and may append follow-up steps (bounded by the
orchestrator).
"""

import hashlib
import json
import logging
import threading
import time

from common.llm_services.capabilities import model_capabilities
from common.py_schemas import Plan
from agent.conversation_window import recent_turns
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

# The planner decides from the schema whether a question needs structural
# retrieval. It gets a compact summary: type names, attribute
# names, edge endpoints and descriptions. The query tools still load the
# full schema themselves.
_SCHEMA_SUMMARY_MAX_CHARS = 12000
# How long a cached summary is used before the schema version and the type
# descriptions are read again.
_SCHEMA_RECHECK_SECONDS = 60
_schema_cache: dict = {}
_schema_cache_lock = threading.Lock()


def _is_bookkeeping_attr(name: str, tg_type) -> bool:
    """GraphRAG processing timestamps and embedding vectors, which no
    question asks about."""
    t = str(tg_type or "").upper()
    return name.startswith("epoch_") or "LIST<FLOAT" in t or "VECTOR" in t


def render_schema_summary(rep) -> str:
    """A compact schema for the planner, from an ``AllowedSchema``."""
    lines = ["Vertex types (attributes):"]
    for vt in rep.vertex_types:
        attrs = [a for a, t in (rep.vertex_attributes.get(vt) or {}).items()
                 if not _is_bookkeeping_attr(a, t)]
        desc = (rep.vertex_definitions or {}).get(vt)
        lines.append(f"- {vt}({', '.join(attrs)})" + (f": {desc}" if desc else ""))
    lines.append("Edge types (from -> to):")
    for et in rep.edge_types:
        pairs = (rep.edge_endpoints or {}).get(et) or []
        ends = ", ".join(f"{f} -> {t}" for f, t in pairs)
        attrs = [a for a, t in (rep.edge_attributes.get(et) or {}).items()
                 if not _is_bookkeeping_attr(a, t)]
        desc = (rep.edge_definitions or {}).get(et)
        line = f"- {et}: {ends}" if ends else f"- {et}"
        if attrs:
            line += f" ({', '.join(attrs)})"
        if desc:
            line += f": {desc}"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > _SCHEMA_SUMMARY_MAX_CHARS:
        text = text[:_SCHEMA_SUMMARY_MAX_CHARS].rsplit("\n", 1)[0] + "\n(more types not shown)"
    return text


def _descriptions_fingerprint(conn) -> str:
    """A short hash of the graph's type descriptions, or "" when they cannot
    be read."""
    try:
        from common.db.schema_utils import read_type_metadata
        entity_descs, rel_defs = read_type_metadata(conn)
    except Exception:
        return ""
    text = json.dumps([entity_descs, rel_defs], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def schema_summary(conn) -> str:
    """The planner's schema summary for the graph of ``conn``.

    Cached per graph. After ``_SCHEMA_RECHECK_SECONDS`` the schema version
    is read again, and the summary is rebuilt only when it changed. Returns
    the last known summary (or "") when the schema cannot be read.
    """
    if conn is None:
        return ""
    key = (str(getattr(conn, "host", "")), getattr(conn, "graphname", ""))
    now = time.time()
    with _schema_cache_lock:
        entry = _schema_cache.get(key)
    if entry and now - entry["checked"] < _SCHEMA_RECHECK_SECONDS:
        return entry["text"]
    try:
        from common.db.connections import get_schema_ver
        version = get_schema_ver(conn)
        # Descriptions can change without a schema version change.
        descriptions = _descriptions_fingerprint(conn)
        if (entry and version is not None and version == entry["version"]
                and descriptions == entry.get("descriptions")):
            with _schema_cache_lock:
                entry["checked"] = now
            return entry["text"]
        from common.db.schema_utils import render_schema_rep
        rep = render_schema_rep(conn)
        text = render_schema_summary(rep)
        graphname = getattr(conn, "graphname", "")
        if graphname:
            # Names the graph the chat is scoped to, for questions such as
            # "what graphs do we have".
            text = f"Graph: {graphname}\n{text}"
        with _schema_cache_lock:
            _schema_cache[key] = {
                "checked": now,
                "version": rep.schema_version if rep.schema_version is not None else version,
                "descriptions": descriptions,
                "text": text,
            }
        return text
    except Exception as exc:
        logger.warning(f"planner: schema summary unavailable ({exc})")
        return entry["text"] if entry else ""

def _param_type(pinfo: dict) -> str:
    """Render a JSON-schema property's type for the catalog. Falls back through
    enum / anyOf (common in external MCP tool schemas) to ``any``."""
    if not isinstance(pinfo, dict):
        return "any"
    t = pinfo.get("type")
    if t:
        return t if isinstance(t, str) else "/".join(str(x) for x in t)
    if pinfo.get("enum"):
        return "enum"
    if pinfo.get("anyOf"):
        types = [a.get("type") for a in pinfo["anyOf"] if isinstance(a, dict) and a.get("type")]
        return "/".join(types) or "any"
    return "any"


def _catalog_text(ctx=None) -> str:
    """Render the tool catalog for the planner: each tool's name, description,
    and a typed parameter list (name: type, required/optional, per-arg
    description). Built-ins have simple args, but external MCP tools can carry
    richer schemas, so surface types/required/descriptions — not bare parameter
    names — so the planner binds their arguments correctly.
    """
    lines = []
    for t in registry.catalog(ctx):
        schema = t.get("args_schema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        lines.append(f"- {t['name']}: {t['description']}")
        if not props:
            lines.append("    params: (none)")
            continue
        for pname, pinfo in props.items():
            pinfo = pinfo if isinstance(pinfo, dict) else {}
            flag = "required" if pname in required else "optional"
            seg = f"    - {pname} ({_param_type(pinfo)}, {flag})"
            desc = pinfo.get("description")
            if desc:
                seg += f": {desc}"
            lines.append(seg)
    return "\n".join(lines)


def _sanitize(plan: Plan, ctx=None) -> Plan:
    """Drop steps referencing unknown tools; guarantee a final answer step."""
    known = set(registry.tool_names(ctx))
    steps = []
    for s in plan.steps or []:
        if s.kind == "answer" or s.tool == "" or s.tool in known:
            steps.append(s)
        else:
            logger.warning(f"planner: dropping step with unknown tool {s.tool!r}")
            plan._dropped_steps.append({"id": s.id, "tool": s.tool})
    if not any(s.kind == "answer" or s.tool == "" for s in steps):
        retrieval_ids = [s.id for s in steps]
        from common.py_schemas import PlanStep
        steps.append(PlanStep(id="A", kind="answer", tool="", depends_on=retrieval_ids,
                              rationale="Synthesize the final answer."))
    plan.steps = steps
    return plan


# Output cap for one plan; reasoning models get a higher floor.
_PLANNER_MAX_OUTPUT_TOKENS = 4000
_REASONING_PLANNER_MIN_OUTPUT_TOKENS = 16000


def _planner_output_limit(llm, ctx) -> int:
    cfg = getattr(ctx, "graphrag_cfg", None) or {}
    try:
        limit = int(cfg.get("planner_max_output_tokens") or _PLANNER_MAX_OUTPUT_TOKENS)
    except (TypeError, ValueError):
        limit = _PLANNER_MAX_OUTPUT_TOKENS
    if model_capabilities(getattr(llm, "config", None)).get("supports_thinking"):
        limit = max(limit, _REASONING_PLANNER_MIN_OUTPUT_TOKENS)
    return limit


def context_sections(conversation=None, schema_rep: str = "") -> dict:
    """The context the planner is given, as ``{key: (heading, text, parts)}``.

    The planner reviews all of it, and names in the answer step's
    ``args.context`` the sections that answer the question (``key``), or one
    numbered part of a section (``key#n``, e.g. ``conversation#2`` for the
    second turn shown). The engine passes those to the answer step as the
    planner saw them. ``parts`` maps a part number to its content, empty for
    a section without parts. A new kind of planner context is one more entry
    here.
    """
    turns = recent_turns(conversation)
    numbered = [{"turn": i, **t} for i, t in enumerate(turns, 1)]
    return {
        "conversation": ("Conversation", json.dumps(numbered, ensure_ascii=False),
                         {str(i): t for i, t in enumerate(turns, 1)}),
        "graph_schema": ("Graph schema", schema_rep[:_SCHEMA_SUMMARY_MAX_CHARS + 100], {}),
    }


def plan_question(llm, question, conversation=None, schema_rep="", prior_results=None, ctx=None,
                  plan_problem: str = "") -> Plan:
    """Draft (or extend) a plan for ``question``.

    ``prior_results`` (a list of ``StepResult``-like dicts) is supplied on
    replan so the model can append follow-up steps from what's been gathered.

    ``ctx`` (optional ``GraphRAGToolContext``) lets the planner see external
    MCP tools attached to the per-request context; when omitted the catalog
    is just the built-ins.
    """
    catalog = _catalog_text(ctx)
    user_parts = [f"## Question\n{question}"]
    for key, (heading, text, _) in context_sections(conversation, schema_rep).items():
        user_parts.append(f"## {heading} (context: {key})\n{text or 'Not available.'}")
    user_parts.append(f"## Tools\n{catalog}")
    if plan_problem:
        user_parts.append(
            "## Previous plan\n"
            f"{plan_problem}\n\n"
            "Write a complete plan: the retrieval steps the question needs, then the final answer step."
        )
    if prior_results:
        summary = "\n".join(
            f"- {r.get('step_id')}: ok={r.get('ok')} — {r.get('summary')}"
            for r in prior_results
        )
        user_parts.append(
            "## Results so far (the previous plan was insufficient)\n"
            f"{summary}\n\nAppend follow-up steps (e.g. widen a retrieval's "
            "top_k/num_hops, switch method, or add a dependent query) to close "
            "the gap, then the final answer step."
        )
    # Use the customizable planner prompt (fixed DAG-planning rules + the
    # editable "Additional Instructions" portion); the default lives in base_llm.
    messages = [("system", llm.agentic_planner_prompt), ("user", "\n\n".join(user_parts))]
    try:
        # Plan steps carry free-form ``args`` maps. Where a provider's default
        # structured output is strict JSON schema (the OpenAI family), it
        # rejects them, so every plan paid for a failed request before falling
        # back to parsing (GML-2302). Tool calling accepts them there; other
        # providers keep their default.
        plan = llm.invoke_structured(
            messages, Plan, caller_name="agentic_plan",
            method=(
                "function_calling"
                if getattr(llm, "strict_structured_output", False) else None
            ),
            max_output_tokens=_planner_output_limit(llm, ctx),
        )
    except Exception as exc:
        logger.warning(f"planner failed ({exc}); falling back to single hybrid step")
        from common.py_schemas import PlanStep
        plan = Plan(
            strategy="Fallback: hybrid search then answer.",
            steps=[
                PlanStep(id="S1", kind="unstructured", tool="graphrag__hybrid_search",
                         args={"question": question}, rationale="Fallback retrieval."),
                PlanStep(id="A", kind="answer", tool="", depends_on=["S1"],
                         rationale="Answer from retrieved context."),
            ],
        )
    return _sanitize(plan, ctx)
