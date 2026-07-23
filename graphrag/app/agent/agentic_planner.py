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

import json
import logging

from common.py_schemas import Plan
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


def plan_question(llm, question, conversation=None, schema_rep="", prior_results=None, ctx=None) -> Plan:
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
        f"## Conversation\n{json.dumps(conversation or [])[:2000]}",
    ]
    # Schema is normally not pre-loaded (the query tools load it themselves);
    # include it only if a caller explicitly supplied one.
    if schema_rep:
        user_parts.append(f"## Graph schema\n{schema_rep[:6000]}")
    user_parts.append(f"## Tools\n{catalog}")
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
        plan = llm.invoke_structured(messages, Plan, caller_name="agentic_plan")
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
