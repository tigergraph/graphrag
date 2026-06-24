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

_SYSTEM = """You are the planner for a GraphRAG question-answering agent over a TigerGraph knowledge graph.

Produce a PLAN: a small DAG of tool steps that gathers exactly the context needed to answer the user's question, then ends with one final "answer" step.

You have two kinds of retrieval:
- STRUCTURAL (graphrag__structural_retrieve): generates and runs a graph query. Best for counts, lookups by attribute/id, relationships, and aggregations over typed data.
- UNSTRUCTURED (graphrag__hybrid_search / similarity_search / contextual_search / community_search): vector search over document text. Best for "what/why/how/describe/summarize" questions answered from passages. community_search suits broad/overall questions.

Planning rules:
- Use BOTH kinds when a question needs facts from the graph AND supporting text. They are not limited to one each — you may run several structural and/or several unstructured steps, in any order.
- A later step may depend on an earlier one: set depends_on and use arg_bindings to pull a value from a prior step's result, e.g. {"question": "S1.context.result"}.
- Prefer the smallest plan that will work. Trivial/greeting questions need only the final answer step (no retrieval).
- Retrieval params (top_k, num_hops, community_level) are optional; omit them to use defaults, or set higher values when you expect a broad answer.
- The final step MUST have kind="answer" and tool="" (the orchestrator synthesizes the answer from gathered context); it should depend_on all retrieval steps.

Tabular / numeric questions (ask for a specific value, a row, a column total, a ranking, or a year-over-year comparison from a table or chart):
- Prefer graphrag__contextual_search or graphrag__hybrid_search with top_k>=10. These return atomic table chunks (chunk_kind="table") that preserve the full row/column structure.
- Avoid graphrag__similarity_search alone for these — it returns isolated vectors and often misses the table when the table's surrounding prose isn't a close vector match to the question.
- Quote any specific table label, column header, year, or unit from the question so the retriever can match it (e.g. "ROE 2023", "預貯金 残高", "従業員数 男性").
- When the question is "compare X across years/regions/categories", set top_k>=15 to ensure all relevant rows come back together rather than scattered across calls.

Return ONLY the structured plan.
"""


def _catalog_text(ctx=None) -> str:
    lines = []
    for t in registry.catalog(ctx):
        props = (t["args_schema"].get("properties") or {})
        params = ", ".join(props.keys()) or "(none)"
        lines.append(f"- {t['name']}({params}): {t['description']}")
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
        f"## Graph schema\n{schema_rep[:6000] or '(unavailable)'}",
        f"## Tools\n{catalog}",
    ]
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
    messages = [("system", _SYSTEM), ("user", "\n\n".join(user_parts))]
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
