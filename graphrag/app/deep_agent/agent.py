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

"""The Deep Agent chat engine.

``DeepGraphRAGAgent`` has the chat-engine interface the routers already drive
(``question_for_agent(question, conversation) -> GraphRAGResponse``, a ``q``
progress queue, ``engine_note``), so the WebSocket chat, the UI query endpoint
and the REST query endpoints run it unchanged. Internally the whole answer is
produced by one LangChain Deep Agent:

* the model decides which tools to call, plans with ``write_todos``, and may
  delegate to two subagents (``graph-analyst``, ``document-researcher``);
* STM: the LangGraph checkpointer stores the conversation thread
  (``user + graph + conversation``); the Deep Agent's summarization
  middleware compacts long threads;
* LTM: ``/memories/`` is routed to the LangGraph store, namespaced per user and
  graph, and ``/memories/AGENTS.md`` is loaded into the system prompt of every
  conversation. Other files are per-thread scratch space in the agent state.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from typing import Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from deep_agent import memory as mem
from deep_agent import prompts
from deep_agent.settings import agent_settings
from deep_agent.tools import ToolEnv, build_tools

logger = logging.getLogger(__name__)

DONE = "DONE"   # same sentinel as agent.Q.DONE; the UI progress loop stops on it


class DeepAgentAnswer(BaseModel):
    """The final answer of the Deep Agent."""

    answer: str = Field(description="The complete answer for the user, in Markdown.")
    citations: List[str] = Field(
        default_factory=list,
        description="Ids of the passages, vertices or records the answer is based on.",
    )
    answered_question: bool = Field(
        default=True,
        description="False when the tools did not provide the information asked for.",
    )


class _ProgressQueue:
    """Thread-safe progress queue read by the WebSocket progress loop
    (``put`` / ``pop`` / ``clear``)."""

    def __init__(self):
        import threading
        self._items: list = []
        self._lock = threading.Lock()

    def put(self, item):
        with self._lock:
            if not self._items or self._items[-1] != item:
                self._items.append(item)

    def pop(self):
        with self._lock:
            return self._items.pop(0) if self._items else None

    def clear(self):
        with self._lock:
            self._items.clear()


class _UsageCallback(BaseCallbackHandler):
    """Collects token usage of every model call in the run, subagents included."""

    def __init__(self, llm_config: Optional[dict]):
        self.calls: list = []
        self._config = llm_config or {}

    def on_llm_end(self, response, **kwargs):
        try:
            for gens in response.generations or []:
                for gen in gens:
                    msg = getattr(gen, "message", None)
                    um = getattr(msg, "usage_metadata", None) or {}
                    if not um:
                        continue
                    inp = int(um.get("input_tokens", 0) or 0)
                    out = int(um.get("output_tokens", 0) or 0)
                    entry = {
                        "caller_name": "deep_agent",
                        "input_tokens": inp,
                        "output_tokens": out,
                        "total_tokens": int(um.get("total_tokens", inp + out) or 0),
                        "cost": 0.0,
                    }
                    try:
                        from common.llm_services.base_llm import estimate_cost_from_config
                        entry["cost"] = estimate_cost_from_config(self._config, inp, out) or 0.0
                    except Exception:
                        pass
                    self.calls.append(entry)
        except Exception as exc:   # usage is best-effort; never break the run
            logger.debug(f"deep_agent usage callback: {exc}")

    def totals(self) -> dict:
        return {
            "input_tokens": sum(c["input_tokens"] for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "total_tokens": sum(c["total_tokens"] for c in self.calls),
            "cost": sum(float(c["cost"] or 0) for c in self.calls),
            "calls": len(self.calls),
        }


def _connection_user(conn) -> Optional[str]:
    """The user a password connection authenticated as; None for token
    connections, whose user name is not the caller's."""
    if getattr(conn, "auth_mode", "pwd") == "token":
        return None
    return _usable_user(getattr(conn, "username", None))


def _usable_user(name) -> Optional[str]:
    """``name`` unless it is empty or a login sentinel shared by many users."""
    name = str(name or "").strip()
    if not name or name.startswith("__"):
        return None
    return name


def _metrics():
    try:
        from common.metrics.prometheus_metrics import metrics
        return metrics
    except Exception:
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def final_answer(messages: list) -> tuple:
    """``(answer, citations, answered)`` of the latest turn.

    Only messages after the turn's question are read: the thread's
    ``structured_response`` channel still holds the previous turn's answer
    when this turn ends without one. The structured answer is the
    ``DeepAgentAnswer`` tool call; a plain-text final message is accepted too.
    """
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls or []:
            if tc.get("name") == DeepAgentAnswer.__name__:
                try:
                    parsed = DeepAgentAnswer(**(tc.get("args") or {}))
                except Exception:
                    continue
                return parsed.answer.strip(), list(parsed.citations), parsed.answered_question
        if not m.tool_calls and _text(m.content).strip():
            text = _text(m.content).strip()
            return text, [], True
    return "", [], False


def history_messages(conversation) -> list:
    """Caller-supplied turns ``[{query, response}]`` as chat messages."""
    out = []
    for turn in conversation or []:
        q, r = turn.get("query"), turn.get("response")
        if q:
            out.append(HumanMessage(content=str(q)))
        if r:
            out.append(AIMessage(content=str(r)))
    return out


class DeepGraphRAGAgent:
    """Chat engine backed by a LangChain Deep Agent (see module docstring)."""

    def __init__(
        self,
        llm_provider,
        db_connection,
        embedding_model,
        embedding_store,
        use_cypher: bool = False,
        ws=None,
        conversation_id: Optional[str] = None,
        user: Optional[str] = None,
        graphrag_cfg: Optional[dict] = None,
    ):
        self.llm = llm_provider
        self.conn = db_connection
        self.graphname = db_connection.graphname
        self.embedding_model = embedding_model
        self.embedding_store = embedding_store
        self.model_name = getattr(embedding_model, "model_name", "unknown")
        if getattr(getattr(embedding_store, "conn", None), "graphname", self.graphname) != self.graphname:
            embedding_store.set_graphname(self.graphname)
        self.use_cypher = use_cypher
        # Memory is scoped by user. ``user=None`` derives it from a password
        # connection; ``user=""`` means the caller is not known. A token or
        # secret login carries no usable user name (pyTigerGraph reports its
        # default, or a shared sentinel), so such a request gets neither a
        # persistent thread nor long-term memory.
        self.user = _connection_user(db_connection) if user is None else _usable_user(user)
        self.conversation_id = conversation_id if self.user else None
        self._graphrag_cfg = graphrag_cfg
        self.q = _ProgressQueue() if ws is not None else None
        self.engine_note = None
        self._last_agent_steps = None
        self._tool_helpers = None

    # ------------------------------------------------------------------ setup

    def emit_progress(self, msg: str) -> None:
        if self.q is not None:
            self.q.put(msg)

    @property
    def graphrag_cfg(self) -> dict:
        if self._graphrag_cfg is None:
            from common.config import get_graphrag_config
            self._graphrag_cfg = get_graphrag_config(self.graphname) or {}
        return self._graphrag_cfg

    def _graph_tool_context(self, conversation):
        """The context the existing GraphRAG tools run against."""
        from tools import GenerateCypher, GenerateFunction, MapQuestionToSchema
        from tools.graphrag_tools import GraphRAGToolContext

        if self._tool_helpers is None:
            self._tool_helpers = (
                MapQuestionToSchema(self.conn, self.llm),
                GenerateFunction(self.conn, self.llm, self.embedding_model, self.embedding_store),
                GenerateCypher(self.conn, self.llm) if self.use_cypher else None,
            )
        mq2s, gen_func, cypher_gen = self._tool_helpers
        return GraphRAGToolContext(
            conn=self.conn, llm_provider=self.llm,
            embedding_model=self.embedding_model, embedding_store=self.embedding_store,
            mq2s=mq2s, gen_func=gen_func, graphrag_cfg=self.graphrag_cfg,
            cypher_gen=cypher_gen, use_cypher=self.use_cypher,
            conversation=conversation, progress=self.emit_progress,
            user=self.user or getattr(self.conn, "username", None),
        )

    def _external_tools(self, graph_ctx) -> Dict[str, object]:
        """MCP add-on tools configured for the graph (empty when none)."""
        try:
            from common.config import get_mcp_servers
            from common.mcp_config import ensure_libraries_installed
            ensure_libraries_installed(get_mcp_servers(self.graphname))
        except Exception as exc:
            logger.warning(f"deep_agent: mcp library install skipped: {exc}")
        try:
            from mcp_addons import discover_tools, get_manager, run_sync
            manager = run_sync(get_manager(self.graphname), timeout=10.0)
            if manager and manager.server_names:
                graph_ctx.mcp_manager = manager
                tools = discover_tools(manager)
                graph_ctx.external_tools = tools
                return tools
        except Exception as exc:
            logger.warning(f"deep_agent: mcp tool discovery skipped: {exc}")
        return {}

    def _instructions(self) -> tuple:
        """Operator-customized prompt text: the answer guidance of the chatbot
        response prompt and the additional instructions of the agent prompt."""
        style = extra = ""
        for name, attr in (("chatbot_response.txt", "style"), ("agentic_agent.txt", "extra")):
            try:
                text = self.llm.get_user_portion(name) or ""
            except Exception:
                text = ""
            if attr == "style":
                style = text
            else:
                extra = text
        return style, extra

    def build(self, env: ToolEnv, external_tools: Dict[str, object], checkpointer, store):
        """Assemble the Deep Agent graph for one request."""
        from deepagents import create_deep_agent
        from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
        from langchain.agents.structured_output import ToolStrategy

        settings = agent_settings(self.graphrag_cfg)
        groups = build_tools(env, external_tools)
        graphrag_tools, tg_tools, mcp_tools = groups["graphrag"], groups["tigergraph"], groups["mcp"]
        all_tools = graphrag_tools + tg_tools + mcp_tools

        style, extra = self._instructions()
        ltm = bool(settings.long_term_memory and store is not None and self.user)
        system_prompt = prompts.main_prompt(
            self.graphname, date.today().isoformat(), env.use_cypher, style, extra,
            long_term_memory=ltm,
        )

        subagents = []
        if settings.subagents:
            by_name = {t.name: t for t in graphrag_tools}
            subagents = [
                {
                    "name": "graph-analyst",
                    "description": "Answers one focused question from the structured graph "
                                   "data (counts, lookups, relationships, aggregations, "
                                   "installed analytics queries).",
                    "system_prompt": prompts.graph_analyst_prompt(self.graphname, env.use_cypher),
                    "tools": [by_name["get_graph_schema"], by_name["query_graph"]] + tg_tools,
                },
                {
                    "name": "document-researcher",
                    "description": "Answers one focused question from the ingested documents "
                                   "and community summaries, returning facts with passage ids.",
                    "system_prompt": prompts.document_researcher_prompt(self.graphname),
                    "tools": [by_name[n] for n in ("hybrid_search", "contextual_search",
                                                   "similarity_search", "community_search")],
                },
            ]

        kwargs = {}
        if ltm:
            ns = mem.memory_namespace(self.graphname, self.user)
            kwargs["backend"] = CompositeBackend(
                default=StateBackend(),
                routes={mem.MEMORIES_ROUTE: StoreBackend(store=store, namespace=lambda _rt: ns)},
            )
            kwargs["memory"] = [mem.MEMORIES_ROUTE.rstrip("/") + mem.AGENTS_MD]
            kwargs["store"] = store
        else:
            kwargs["backend"] = StateBackend()

        return create_deep_agent(
            model=self.llm.llm,
            tools=all_tools,
            system_prompt=system_prompt,
            subagents=subagents or None,
            response_format=ToolStrategy(DeepAgentAnswer),
            checkpointer=checkpointer,
            name="graphrag-deep-agent",
            **kwargs,
        )

    # -------------------------------------------------------------------- run

    def question_for_agent(self, question: str, conversation: List[Dict[str, str]] = None):
        start = time.time()
        settings = agent_settings(self.graphrag_cfg)
        self.emit_progress("Thinking")
        usage = _UsageCallback(getattr(self.llm, "config", None))
        env = None
        metrics = _metrics()
        if metrics:
            metrics.llm_inprogress_requests.labels(self.model_name).inc()
        try:
            backends = mem.get_backends()
            convo = [{"query": c.get("query", ""), "response": c.get("response", "")}
                     for c in (conversation or [])]
            graph_ctx = self._graph_tool_context(convo)
            env = ToolEnv(
                graph_ctx=graph_ctx,
                max_output_chars=settings.max_tool_output_chars,
                allow_installed_queries=settings.allow_installed_queries,
                installed_query_allowlist=list(
                    (self.graphrag_cfg.get("deep_agent") or {}).get("installed_query_allowlist") or ["*"]),
                use_cypher=self.use_cypher,
                progress=self.emit_progress,
            )
            external = self._external_tools(graph_ctx)

            # STM: a conversation id maps to a persistent thread. A request
            # without one (REST /query, /query_with_history) runs on a private
            # in-memory thread seeded with the caller's own history.
            if self.conversation_id:
                tid = mem.thread_id(self.graphname, self.user, self.conversation_id)
                checkpointer = backends.checkpointer
                prior = mem.thread_messages(tid)
            else:
                from langgraph.checkpoint.memory import InMemorySaver
                tid = f"ephemeral:{uuid.uuid4()}"
                checkpointer = InMemorySaver(serde=mem.serializer())
                prior = []
            seed = history_messages(convo) if not prior else []

            agent = self.build(env, external, checkpointer, backends.store)
            config = {
                "configurable": {"thread_id": tid},
                "recursion_limit": settings.recursion_limit,
                "callbacks": [usage],
                "metadata": {"graph": self.graphname, "user": self.user or ""},
            }
            inputs = {"messages": seed + [HumanMessage(content=question)]}

            hit_limit = False
            try:
                for _chunk in agent.stream(inputs, config=config, stream_mode="updates", subgraphs=True):
                    self._observe(_chunk)
            except Exception as exc:
                from langgraph.errors import GraphRecursionError
                if not isinstance(exc, GraphRecursionError):
                    raise
                hit_limit = True
                logger.warning(f"deep_agent: recursion limit {settings.recursion_limit} reached")

            values = agent.get_state(config).values or {}
            resp = self._response(values, env, usage, tid, hit_limit, time.time() - start)
            self._last_agent_steps = resp.query_sources.get("agent_steps")
            return resp
        except Exception:
            # Keep the tool calls made before the failure for the trace log.
            self._last_agent_steps = self._steps(env, usage, None) if env else None
            if metrics:
                metrics.llm_query_error_total.labels(self.model_name).inc()
            raise
        finally:
            self.emit_progress(DONE)
            if metrics:
                metrics.llm_request_total.labels(self.model_name).inc()
                metrics.llm_inprogress_requests.labels(self.model_name).dec()
                metrics.llm_request_duration_seconds.labels(self.model_name).observe(time.time() - start)

    def _observe(self, chunk) -> None:
        """Progress labels for the planning and delegation steps (tools emit
        their own)."""
        try:
            _ns, update = chunk if isinstance(chunk, tuple) and len(chunk) == 2 else ((), chunk)
            for _node, value in (update or {}).items():
                for msg in (value or {}).get("messages", []) if isinstance(value, dict) else []:
                    for tc in getattr(msg, "tool_calls", None) or []:
                        name = tc.get("name")
                        if name == "write_todos":
                            self.emit_progress("Planning the steps")
                        elif name == "task":
                            sub = (tc.get("args") or {}).get("subagent_type", "a subagent")
                            self.emit_progress(f"Delegating to {sub}")
                        elif name in ("write_file", "edit_file") and \
                                str((tc.get("args") or {}).get("file_path", "")).startswith(mem.MEMORIES_ROUTE):
                            self.emit_progress("Updating memory")
        except Exception:
            pass

    # --------------------------------------------------------------- response

    @staticmethod
    def _steps(env: Optional[ToolEnv], usage: _UsageCallback, final: Optional[dict]) -> list:
        steps = []
        for i, entry in enumerate(env.trace if env else [], 1):
            output = {"ok": entry["ok"], "summary": entry["summary"]}
            for key in ("query", "attempts"):
                if entry.get(key):
                    output[key] = entry[key]
            steps.append({
                "node": f"{i}: {entry['tool']}", "kind": "tool",
                "duration_s": entry["duration_s"],
                "input": entry["args"], "output": output,
            })
        if final is not None:
            steps.append({
                "node": f"{len(steps) + 1}: answer", "kind": "answer",
                "duration_s": final.pop("duration_s", 0),
                "input": {"model_calls": len(usage.calls)},
                "output": final,
                "usage": usage.totals(),
            })
        return steps

    def _response(self, values: dict, env: ToolEnv, usage: _UsageCallback, tid: str,
                  hit_limit: bool, elapsed: float):
        from common.py_schemas import GraphRAGResponse

        answer, citations, answered = final_answer(values.get("messages") or [])
        if hit_limit and not answer:
            answer = ("I gathered some information but could not finish the answer within the "
                      "step budget. Please narrow the question or ask about one part at a time.")
            answered = False
        if not answer:
            answer = "I could not produce an answer. Please rephrase the question."
            answered = False

        retrieved = list(env.retrieved.keys())
        cited = set(citations)
        # The chat UI's "Explain" view lists sources; a "* " prefix marks the cited ones.
        reasoning = [("* " if pid in cited else "") + f"{pid}: {env.retrieved[pid][:500]}"
                     for pid in retrieved]
        structured_rows = next((e for e in reversed(env.trace)
                                if e["tool"] in ("query_graph", "run_cypher_query", "get_vertices",
                                                 "run_installed_query") and e["ok"]), None)
        todos = values.get("todos") or []
        final = {"answer": answer[:4000], "citations": citations,
                 "answered_question": answered, "duration_s": round(elapsed, 3)}
        query_sources = {
            "engine": "deep_agent",
            "thread_id": tid if not tid.startswith("ephemeral:") else None,
            "agent_steps": self._steps(env, usage, final),
            "todos": todos,
            "citations": citations,
            "retrieved_citations": retrieved,
            "reasoning": reasoning,
            "token_usage": usage.totals(),
            "hit_step_limit": hit_limit,
        }
        if structured_rows is not None:
            query_sources["structured_source"] = {"tool": structured_rows["tool"],
                                                  "query": structured_rows.get("query", "")}
        return GraphRAGResponse(
            natural_language_response=answer,
            answered_question=answered,
            response_type="deep_agent",
            query_sources=query_sources,
        )
