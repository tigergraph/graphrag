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

"""Agentic orchestration: plan -> execute -> (evaluate & maybe extend) ->
synthesize.

Implemented as a bounded control loop rather than a LangGraph StateGraph —
the flow is linear with a single replan loop, so a plain loop is clearer
and fully testable. The classic engine keeps its LangGraph; this one can
adopt LangGraph later if checkpointing/streaming-graph features are needed.
"""

import logging
import time

from agent.agentic_executor import execute_plan, fit_for_trace, with_trace_note, _run_step, _usage_since
from agent.agentic_planner import plan_question, schema_summary
from agent.agentic_synthesizer import _gather, has_context, synthesize, with_conversation
from common.llm_services.base_llm import get_collected_usage
from common.py_schemas import GraphRAGResponse, Plan, PlanStep

logger = logging.getLogger(__name__)

MAX_REPLANS = 3          # how many times the planner may extend the plan
MAX_TOTAL_STEPS = 20     # hard cap on executed retrieval steps across replans
# Both are overridable per-graph via ``graphrag_config`` keys
# ``agent_max_replans`` and ``agent_max_total_steps``. Raise them for
# complex-system graphs (e.g. multi-hop what-if simulation).

# Deterministic fallback: a structural query that returns no rows leaves the
# answer with nothing to stand on. Rather than depend on the LLM planner to
# add an unstructured step on replan (it does so inconsistently), we guarantee
# a hybrid search runs before giving up.
_STRUCTURAL_TOOL = "graphrag__structural_retrieve"
_HYBRID_TOOL = "graphrag__hybrid_search"


def _hybrid_fallback_step() -> PlanStep:
    return PlanStep(
        id="fallback_hybrid",
        kind="unstructured",
        tool=_HYBRID_TOOL,
        rationale="Structural query returned no rows; falling back to hybrid search",
    )


def _search_plan(question: str) -> Plan:
    """A structural query and a hybrid search for the question, then the
    answer. Used when the planner gives no retrieval for a question that
    needs data; a structural query that finds nothing just returns no rows."""
    return Plan(
        strategy="Query the graph and search the documents for the question, then answer.",
        steps=[
            PlanStep(id="S1", kind="structural", tool=_STRUCTURAL_TOOL,
                     args={"question": question},
                     rationale="The plan had no usable retrieval steps, so query the graph."),
            PlanStep(id="S2", kind="unstructured", tool=_HYBRID_TOOL,
                     args={"question": question},
                     rationale="The plan had no usable retrieval steps, so search the documents."),
            PlanStep(id="A", kind="answer", tool="", depends_on=["S1", "S2"],
                     rationale="Answer from the retrieved context."),
        ],
    )


def _plan_output(plan) -> dict:
    """A plan as shown in the trace."""
    review = getattr(plan, "conversation_review", None)
    out = {
        "conversation_review": review.model_dump() if review is not None else None,
        "strategy": plan.strategy,
        "steps": [s.model_dump() for s in plan.steps],
    }
    dropped = getattr(plan, "_dropped_steps", None)
    if dropped:
        out["dropped_steps"] = dropped
    if not _retrieves(plan):
        out["routing"] = ("answered without retrieval" if _answer_only_is_justified(plan)
                          else "no retrieval steps")
    return out


def _answer_only_is_justified(plan) -> bool:
    """A plan with no retrieval steps is a decision only when its review
    backs it: a good earlier answer states every fact asked for, or the
    question needs no data. Anything else is an incomplete plan."""
    review = getattr(plan, "conversation_review", None)
    if review is None:
        return False
    if not review.needs_data:
        return True
    return review.coverage == "all" and review.earlier_answer == "good"


def _plan_problem(plan) -> str:
    """Why a plan cannot be used as it is, for the replan request."""
    review = plan.conversation_review
    missing = [f for f in review.facts_asked if f not in review.stated_by_good_answer]
    detail = f" ## Conversation does not answer: {'; '.join(missing)}." if missing else ""
    return ("The previous plan had no retrieval steps, but its conversation_review does not "
            "support answering without retrieval." + detail)


def _retrieves(plan) -> bool:
    """Whether the plan has a step that runs a tool. A plan with only the
    answer step is the planner's decision to answer from the conversation
    (or without data), not a retrieval that found nothing."""
    return any(s.kind != "answer" and s.tool for s in plan.steps)


def run_agentic(ctx, llm, question, conversation=None) -> GraphRAGResponse:
    """Run the agentic workflow for one question and return a response.

    ``ctx`` is a ``GraphRAGToolContext`` (carries the per-user conn, the
    retrievers/structural tools, config, and the progress emitter).
    """
    emit = ctx.emit

    # Per-graph overrides for the agentic depth knobs.
    _cfg = ctx.graphrag_cfg or {}
    max_replans = int(_cfg.get("agent_max_replans", MAX_REPLANS))
    max_total_steps = int(_cfg.get("agent_max_total_steps", MAX_TOTAL_STEPS))

    emit("Planning an approach")
    # The planner decides from the schema whether the question needs
    # structural retrieval. Questions that need no data are answered by triage
    # before this point.
    schema = schema_summary(getattr(ctx, "conn", None))

    results: dict = {}
    agent_steps: list = []

    # plan (timed + usage-attributed for the trace)
    _u0 = len(get_collected_usage() or [])
    _t0 = time.time()
    plan = plan_question(llm, question, conversation, schema_rep=schema, ctx=ctx)
    agent_steps.append({
        "node": "plan", "kind": "plan",
        "duration_s": round(time.time() - _t0, 3),
        "input": {"question": question, "conversation": conversation or [],
                  "graph_schema": schema},
        "output": _plan_output(plan),
        "usage": _usage_since(_u0),
    })
    # Every plan must either retrieve or justify answering without retrieval
    # (its conversation_review). An incomplete plan gets one replan; if that
    # is incomplete too, the fallback plan retrieves.
    if not _retrieves(plan) and not _answer_only_is_justified(plan):
        problem = _plan_problem(plan)
        emit("Refining the plan")
        _u0 = len(get_collected_usage() or [])
        _t0 = time.time()
        plan = plan_question(llm, question, conversation, schema_rep=schema, ctx=ctx,
                             plan_problem=problem)
        agent_steps.append({
            "node": "replan (incomplete plan)", "kind": "plan",
            "duration_s": round(time.time() - _t0, 3),
            "input": {"problem": problem},
            "output": _plan_output(plan),
            "usage": _usage_since(_u0),
        })
        if not _retrieves(plan) and not _answer_only_is_justified(plan):
            logger.info("agentic: replan is incomplete too; using the fallback plan")
            plan = _search_plan(question)
            agent_steps.append({
                "node": "fallback plan", "kind": "plan", "duration_s": 0,
                "input": {"reason": "The plan had no usable retrieval steps after a replan."},
                "output": _plan_output(plan),
                "usage": {},
            })

    if plan.strategy:
        emit(plan.strategy)

    replans = 0
    while _retrieves(plan):
        new_results, step_traces = execute_plan(plan, ctx)
        results.update(new_results)
        agent_steps.extend(step_traces)

        # Deterministic safety net: if a structural retrieve was attempted and
        # produced no context, fall back to a hybrid search directly rather
        # than relying on the planner to add one on replan. Runs at most once.
        # Shares the classic engine's ``enable_router_fallback`` knob (default
        # True) so both engines fall back — or don't — consistently.
        used = {t.get("tool") for t in agent_steps}
        if (_cfg.get("enable_router_fallback", True)
                and not has_context(results)
                and _STRUCTURAL_TOOL in used
                and _HYBRID_TOOL not in used):
            emit("No structured results; falling back to hybrid search")
            fb_traces: list = []
            _run_step(_hybrid_fallback_step(), {"question": question},
                      ctx, results, fb_traces)
            agent_steps.extend(fb_traces)

        if has_context(results) or replans >= max_replans or len(results) >= max_total_steps:
            break

        # Insufficient context and budget remains: ask the planner to extend.
        replans += 1
        emit("Refining the plan")
        prior = [
            {"step_id": sr.step_id, "ok": sr.ok, "summary": sr.summary}
            for sr in results.values()
        ]
        _u0 = len(get_collected_usage() or [])
        _t0 = time.time()
        plan = plan_question(llm, question, conversation, schema_rep=schema,
                             prior_results=prior, ctx=ctx)
        agent_steps.append({
            "node": f"replan {replans}", "kind": "plan",
            "duration_s": round(time.time() - _t0, 3),
            "input": {"results_so_far": prior},
            "output": _plan_output(plan),
            "usage": _usage_since(_u0),
        })

    emit("Writing the answer")
    _u0 = len(get_collected_usage() or [])
    _t0 = time.time()
    resp = synthesize(llm, question, results, plan=plan, conversation=conversation)
    # Input is the combined context fed to the answer LLM (what actually
    # grounded the answer); output is the answer + citations.
    agent_steps.append({
        "node": "synthesize", "kind": "answer",
        "duration_s": round(time.time() - _t0, 3),
        "input": with_trace_note(*fit_for_trace(with_conversation(_gather(results), conversation))),
        "output": {
            "answer": resp.natural_language_response,
            "citations": (resp.query_sources or {}).get("citations", []),
        },
        "usage": _usage_since(_u0),
    })

    # Rich per-step trace for the Trace Logs UI (durations + per-node usage).
    if resp.query_sources is None:
        resp.query_sources = {}
    resp.query_sources["agent_steps"] = agent_steps
    return resp
