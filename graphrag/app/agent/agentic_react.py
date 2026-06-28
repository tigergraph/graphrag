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

"""Agentic react orchestrator — free tool-calling loop.

The configured chat model freely calls registry tools in a reason-act loop:
each iteration is one LLM round-trip that may emit zero or more tool calls,
whose results are fed back as ``ToolMessage`` observations on the next
iteration. The loop ends when the model answers without tool calls, or
when the per-graph iteration cap is hit.

This is the alternative to the planner→executor engine in
``agentic_graph.run_agentic``; both are reachable from ``AgenticAgent``
based on ``graphrag_config.agent_style`` (default ``"planned"``).
"""

import json
import logging
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.agentic_executor import cap_for_trace, _usage_since
from common.llm_services.base_llm import get_collected_usage
from common.py_schemas import GraphRAGResponse
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 30      # default; override per-graph via graphrag_config.agent_max_iterations

_SYSTEM = """You are a GraphRAG agent answering questions over a TigerGraph knowledge graph.

You have a set of read-only tools (graph schema, structural query generation, several unstructured retrievers, raw GSQL via tg_run_query, neighbor expansion). The graph schema is provided in the user message.

PLAN, THEN ACT.

In your very first response, BEFORE issuing any tool calls, briefly state your plan in 1-3 sentences in the text portion of your response: what you intend to retrieve, in what order, and why. THEN issue your initial tool calls in the same response.

On later iterations, if observations change your strategy, briefly say so in the text portion before issuing new tool calls. Otherwise just act.

How to use the tools:
- ALWAYS run a vector search (graphrag__hybrid_search or graphrag__contextual_search) UNLESS you are highly confident the question is a pure structured-data request — an exact count, an attribute/id lookup, a relationship traversal, or an aggregation over typed graph data — that a generated graph query fully answers on its own. Structural query generation alone is NOT a safe sole source: it can return nothing or the wrong rows when the question doesn't map cleanly to typed data. Whenever the answer could plausibly live in document text (what/why/how/describe/summarize, definitions, explanations, figures, or anything a person would read from a passage), you MUST include a vector search. When unsure, use vector search — this matches the classic engine, which always retrieves from passages.
- Mix structural (graph queries) and unstructured (vector / community) retrieval as the question needs. Run independent tool calls in parallel within one response; chain dependent calls across iterations. When you do use a structural query, pair it with a vector search unless the question is a pure structured-data request as defined above.
- Stop iterating and give a final natural-language answer (no tool calls) once you have enough grounded context.
- If a retrieval returns thin or empty results, widen its parameters (top_k, num_hops) or switch method instead of repeating identical calls.

Be efficient: the smallest set of tool calls that answers the question is best. Cite specific findings from tool results in your final answer."""


def _gather_for_response(messages):
    """Collect the tool-message observations across the loop, capped, for
    inclusion in ``query_sources.result``. Lets the Trace Logs UI / chat
    history show what the agent saw, not just the final answer.
    """
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            try:
                content = json.loads(m.content) if isinstance(m.content, str) else m.content
            except Exception:
                content = m.content
            out.append({"tool_call_id": m.tool_call_id, "observation": content})
    return cap_for_trace(out)


def run_react(ctx, llm, question, conversation=None) -> GraphRAGResponse:
    """Run the free tool-calling loop for one question and return a response."""
    emit = ctx.emit
    _cfg = ctx.graphrag_cfg or {}
    max_iters = int(_cfg.get("agent_max_iterations", MAX_ITERATIONS))

    # Up-front schema read so the model has the graph layout in context.
    emit("Reading the graph schema")
    schema_step_usage = len(get_collected_usage() or [])
    schema_t0 = time.time()
    schema_out = registry.run("graphrag__get_schema", {}, ctx)
    schema_dur = round(time.time() - schema_t0, 3)
    schema_rep = ""
    if isinstance(schema_out.get("context"), dict):
        schema_rep = schema_out["context"].get("schema_rep", "")

    user = (
        f"## Question\n{question}\n\n"
        f"## Conversation\n{json.dumps(conversation or [])[:2000]}\n\n"
        f"## Graph schema\n{schema_rep[:6000] or '(unavailable)'}"
    )
    # Customizable system prompt (fixed rules + user "Additional Instructions");
    # falls back to the local default if the service lacks the property.
    system_prompt = getattr(llm, "agentic_agent_prompt", None) or _SYSTEM
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user)]

    tools = registry.lc_tools_spec(ctx)

    agent_steps = [{
        "node": "schema", "kind": "schema",
        "duration_s": schema_dur,
        "input": {},
        "output": {"summary": schema_out.get("summary", "")},
        "usage": _usage_since(schema_step_usage),
    }]

    final_answer = None
    citations: list = []

    for i in range(max_iters):
        emit(f"Thinking (step {i + 1})")
        usage_start = len(get_collected_usage() or [])
        t0 = time.time()
        try:
            resp = llm.invoke_with_tools(messages, tools, caller_name=f"react_iter_{i}")
        except Exception as exc:
            logger.warning(f"react iter {i} llm failed: {exc}")
            break
        iter_dur = round(time.time() - t0, 3)
        messages.append(resp)

        tool_calls = list(getattr(resp, "tool_calls", []) or [])
        ai_text = (resp.content if isinstance(resp.content, str) else
                   "".join(c.get("text", "") for c in (resp.content or []) if isinstance(c, dict)))

        if not tool_calls:
            # Final answer.
            final_answer = ai_text or "(no answer produced)"
            agent_steps.append({
                "node": f"iter {i + 1}: answer", "kind": "answer",
                "duration_s": iter_dur,
                "input": {"messages_so_far": len(messages) - 1},
                "output": {"answer": final_answer[:4000]},
                "usage": _usage_since(usage_start),
            })
            break

        # Execute each tool call, append observations.
        per_call_traces = []
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            emit(f"{name}")
            tool_t0 = time.time()
            out = registry.run(name or "", args or {}, ctx)
            tool_dur = round(time.time() - tool_t0, 3)
            obs = {"summary": out.get("summary", "")}
            if out.get("context") is not None:
                obs["result"] = out.get("context")
            obs_capped = cap_for_trace(obs)
            messages.append(ToolMessage(
                content=json.dumps(obs_capped, default=str),
                tool_call_id=tc_id or "",
            ))
            per_call_traces.append({
                "tool": name, "args": cap_for_trace(args),
                "ok": bool(out.get("ok")), "summary": out.get("summary", ""),
                "duration_s": tool_dur,
            })
            citations.extend(out.get("citations") or [])

        agent_steps.append({
            "node": f"iter {i + 1}: tool calls", "kind": "react",
            "duration_s": iter_dur,
            "input": {"reasoning_preview": ai_text[:600] if ai_text else ""},
            "output": {"tool_calls": per_call_traces},
            "usage": _usage_since(usage_start),
        })

    hit_cap = final_answer is None
    if final_answer is None:
        # Out of budget — synthesize an honest "couldn't finalize" answer.
        final_answer = (
            "I gathered some information but couldn't finalize an answer within "
            f"the iteration budget ({max_iters})."
        )

    return GraphRAGResponse(
        natural_language_response=final_answer,
        answered_question=bool(final_answer and not hit_cap),
        response_type="agentic",
        query_sources={
            "engine": "react",
            "agent_steps": agent_steps,
            "iterations": len([s for s in agent_steps if s["kind"] in ("react", "answer")]),
            "max_iterations": max_iters,
            "hit_iteration_cap": hit_cap,
            "result": _gather_for_response(messages),
            "citations": citations,
        },
    )
