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

"""Agentic orchestration: plan -> execute -> (evaluate & maybe extend) ->
synthesize.

Implemented as a bounded control loop rather than a LangGraph StateGraph —
the flow is linear with a single replan loop, so a plain loop is clearer
and fully testable. The classic engine keeps its LangGraph; this one can
adopt LangGraph later if checkpointing/streaming-graph features are needed.
"""

import logging
import time

from agent.agentic_executor import cap_for_trace, execute_plan, _usage_since
from agent.agentic_planner import plan_question
from agent.agentic_synthesizer import _gather, has_context, synthesize
from common.llm_services.base_llm import get_collected_usage
from common.py_schemas import GraphRAGResponse

logger = logging.getLogger(__name__)

MAX_REPLANS = 3          # how many times the planner may extend the plan
MAX_TOTAL_STEPS = 20     # hard cap on executed retrieval steps across replans
# Both are overridable per-graph via ``graphrag_config`` keys
# ``agent_max_replans`` and ``agent_max_total_steps``. Raise them for
# complex-system graphs (e.g. multi-hop what-if simulation).


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

    # The schema is loaded lazily by the query tools at run time, so a question
    # that needs no graph data does not trigger a schema read.
    emit("Planning an approach")

    results: dict = {}
    agent_steps: list = []

    # plan (timed + usage-attributed for the trace)
    _u0 = len(get_collected_usage() or [])
    _t0 = time.time()
    plan = plan_question(llm, question, conversation, ctx=ctx)
    agent_steps.append({
        "node": "plan", "kind": "plan",
        "duration_s": round(time.time() - _t0, 3),
        "input": {"question": question, "conversation": conversation or []},
        "output": {"strategy": plan.strategy,
                   "steps": [s.model_dump() for s in plan.steps]},
        "usage": _usage_since(_u0),
    })
    if plan.strategy:
        emit(plan.strategy)

    replans = 0
    while True:
        new_results, step_traces = execute_plan(plan, ctx)
        results.update(new_results)
        agent_steps.extend(step_traces)

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
        plan = plan_question(llm, question, conversation, prior_results=prior, ctx=ctx)
        agent_steps.append({
            "node": f"replan {replans}", "kind": "plan",
            "duration_s": round(time.time() - _t0, 3),
            "input": {"results_so_far": prior},
            "output": {"strategy": plan.strategy,
                       "steps": [s.model_dump() for s in plan.steps]},
            "usage": _usage_since(_u0),
        })

    emit("Writing the answer")
    _u0 = len(get_collected_usage() or [])
    _t0 = time.time()
    resp = synthesize(llm, question, results, plan=plan, conversation=conversation)
    agent_steps.append({
        "node": "synthesize", "kind": "answer",
        "duration_s": round(time.time() - _t0, 3),
        # Input is the combined context fed to the answer LLM (what actually
        # grounded the answer); output is the answer + citations.
        "input": cap_for_trace(_gather(results)),
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
