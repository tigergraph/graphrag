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
from langchain_core.output_parsers import PydanticOutputParser

from agent.agentic_executor import cap_for_trace, retrieved_chunk_ids, _usage_since
from common.llm_services.base_llm import get_collected_usage
from common.py_schemas import GraphRAGAnswerOutput, GraphRAGResponse
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 30      # default; override per-graph via graphrag_config.agent_max_iterations

# User-facing progress labels for tool calls — never surface raw tool names
# (e.g. "graphrag__hybrid_search") in the chat. Unmapped tools (external / MCP)
# fall back to a generic phrase.
_TOOL_LABELS = {
    "graphrag__get_schema": "Reading the graph schema",
    "graphrag__structural_retrieve": "Searching the knowledge graph",
    "graphrag__hybrid_search": "Searching the documents",
    "graphrag__contextual_search": "Searching the documents",
    "graphrag__similarity_search": "Searching the documents",
    "graphrag__community_search": "Searching community summaries",
    "tg_run_query": "Running a graph query",
}


def _tool_label(name: str) -> str:
    if name in _TOOL_LABELS:
        return _TOOL_LABELS[name]
    if (name or "").startswith("graphrag__gsql__"):
        return f"Running registered query {name[len('graphrag__gsql__'):]}"
    return "Gathering information"

def run_react(ctx, llm, question, conversation=None) -> GraphRAGResponse:
    """Run the free tool-calling loop for one question and return a response."""
    emit = ctx.emit
    _cfg = ctx.graphrag_cfg or {}
    max_iters = int(_cfg.get("agent_max_iterations", MAX_ITERATIONS))

    # The graph schema is NOT pre-loaded. The model fetches it lazily via the
    # graphrag__get_schema tool, and only when it intends to use a structural
    # or unstructured query tool (per the system prompt). Questions answered by
    # other tools (e.g. external MCP tools) skip the schema read entirely.
    user = (
        f"## Question\n{question}\n\n"
        f"## Conversation\n{json.dumps(conversation or [])[:2000]}"
    )
    # Customizable system prompt (fixed rules + user "Additional Instructions");
    # the default lives in base_llm.
    system_prompt = llm.agentic_agent_prompt
    # The terminal turn returns a structured {generated_answer, citation}
    # object, so the trace records the selected citations and the chat gets a
    # clean answer. Inject the output contract + format instructions here; the
    # role/tool rules stay in the system prompt above. Also fold in the editable
    # answer guidance (the chatbot_response user portion) so style/focus stays
    # consistent — only the guidance text, never a role or JSON wrapper.
    answer_parser = PydanticOutputParser(pydantic_object=GraphRAGAnswerOutput)
    try:
        answer_style = llm.get_user_portion("chatbot_response.txt")
    except Exception:
        answer_style = ""
    final_answer_block = [
        "\n\n## Final Answer",
        "When the gathered context can answer the question, STOP calling tools "
        "and reply with a SINGLE JSON object (and no tool call) of this shape:",
        answer_parser.get_format_instructions(),
        "Put the full natural-language answer in `generated_answer`, and in "
        "`citation` list the keys/ids of the context parts you actually used "
        "to write it.",
    ]
    if answer_style:
        final_answer_block.append(
            "Follow these guidelines when writing `generated_answer`:\n" + answer_style
        )
    system_prompt += "\n".join(final_answer_block)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user)]

    tools = registry.lc_tools_spec(ctx)

    agent_steps = []

    final_answer = None
    # Two citation layers for the admin trace, both recorded by the agent from
    # the tool results it already holds: what it FETCHED (retrieved, the chunk
    # ids the retrievers returned) vs what it actually SELECTED to write the
    # answer (selected, from the final-turn citation field).
    retrieved_citations: list = []
    _retrieved_seen: set = set()
    selected_citations: list = []

    for i in range(max_iters):
        emit("Thinking")
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
            # Final turn: the model returns a structured {generated_answer,
            # citation} object. Parse it, recovering the prose answer if the
            # JSON is malformed (a plain-text turn recovers to the text itself).
            parsed = llm.parse_answer_output(ai_text)
            final_answer = (parsed.generated_answer or "").strip() or "(no answer produced)"
            selected_citations = list(parsed.citation or [])
            agent_steps.append({
                "node": f"iter {i + 1}: answer", "kind": "answer",
                "duration_s": iter_dur,
                "input": {"messages_so_far": len(messages) - 1},
                "output": {"answer": final_answer[:4000], "citations": selected_citations},
                "usage": _usage_since(usage_start),
            })
            break

        # Execute each tool call, append observations.
        per_call_traces = []
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            emit(_tool_label(name))
            tool_t0 = time.time()
            out = registry.run(name or "", args or {}, ctx)
            tool_dur = round(time.time() - tool_t0, 3)
            # Feed the FULL tool result to the model so it reasons and answers
            # on complete context (retrieval size is bounded by max_results).
            # The trace records only summaries and chunk ids (below), never the
            # raw retrieval text.
            obs = {"summary": out.get("summary", "")}
            if out.get("context") is not None:
                obs["result"] = out.get("context")
            messages.append(ToolMessage(
                content=json.dumps(obs, default=str),
                tool_call_id=tc_id or "",
            ))
            per_call_traces.append({
                "tool": name, "args": cap_for_trace(args),
                "ok": bool(out.get("ok")), "summary": out.get("summary", ""),
                "duration_s": tool_dur,
            })
            # Record the chunk ids this tool fetched (the agent's bookkeeping,
            # harvested from the context the tool returned), de-duped in order.
            for cid in retrieved_chunk_ids(out.get("context")):
                if cid not in _retrieved_seen:
                    _retrieved_seen.add(cid)
                    retrieved_citations.append(cid)

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
            "citations": selected_citations,
            "retrieved_citations": retrieved_citations,
        },
    )
