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

import logging

from common.llm_services.capabilities import model_capabilities
from common.py_schemas import ConversationCheck, Plan
from agent.conversation_window import recent_conversation
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

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
            logger.info(f"planner: dropping step with unknown tool {s.tool!r}")
    if not any(s.kind == "answer" or s.tool == "" for s in steps):
        retrieval_ids = [s.id for s in steps]
        from common.py_schemas import PlanStep
        steps.append(PlanStep(id="A", kind="answer", tool="", depends_on=retrieval_ids,
                              rationale="Synthesize the final answer."))
    plan.steps = steps
    return plan


# A normal plan is a few hundred tokens; the cap only ends a runaway
# generation early (GML-2324). Reasoning models spend output tokens on
# thinking, so they get a higher floor.
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


def check_conversation(llm, question, conversation, ctx=None):
    """Ask whether a plan that skips retrieval can answer from the conversation.

    Returns a ``ConversationCheck``, or ``None`` when the check fails (the
    caller then keeps the plan).
    """
    try:
        messages = [
            ("system", llm._AGENTIC_CONVERSATION_CHECK_SYSTEM),
            ("user", f"## Question\n{question}\n\n## Conversation\n{recent_conversation(conversation)}"),
        ]
        return llm.invoke_structured(
            messages, ConversationCheck, caller_name="agentic_conversation_check",
            max_output_tokens=_planner_output_limit(llm, ctx),
        )
    except Exception as exc:
        logger.warning(f"conversation check failed ({exc}); keeping the plan")
        return None


def plan_question(llm, question, conversation=None, schema_rep="", prior_results=None, ctx=None,
                  missing_from_conversation: str = "") -> Plan:
    """Draft (or extend) a plan for ``question``.

    ``prior_results`` (a list of ``StepResult``-like dicts) is supplied on
    replan so the model can append follow-up steps from what's been gathered.

    ``ctx`` (optional ``GraphRAGToolContext``) lets the planner see external
    MCP tools attached to the per-request context; when omitted the catalog
    is just the built-ins.
    """
    catalog = _catalog_text(ctx)
    user_parts = [
        f"## Question\n{question}",
        f"## Conversation\n{recent_conversation(conversation)}",
    ]
    # Schema is normally not pre-loaded (the query tools load it themselves);
    # include it only if a caller explicitly supplied one.
    if schema_rep:
        user_parts.append(f"## Graph schema\n{schema_rep[:6000]}")
    user_parts.append(f"## Tools\n{catalog}")
    if missing_from_conversation:
        user_parts.append(
            "## Missing from the conversation\n"
            f"## Conversation does not state: {missing_from_conversation}\n\n"
            "Plan retrieval steps for these facts, then the final answer step."
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
