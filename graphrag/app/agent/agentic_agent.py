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

"""Agentic chat agent (v2.0 deep-thinking mode).

Public-API twin of ``TigerGraphAgent``: same constructor shape and
``question_for_agent(question, conversation)`` returning a
``GraphRAGResponse``, and the same ``Q`` progress queue — so the WS and
REST entry points drive it identically. Internally it runs the
plan -> execute -> synthesize loop (``agentic_graph.run_agentic``) over the
GraphRAG tool layer instead of the fixed classic LangGraph.
"""

import logging
import json
import time
from typing import Dict, List

from pydantic import BaseModel, Field

from agent.Q import Q, DONE
from agent.agentic_graph import run_agentic
from agent.agentic_react import run_react
from tools import GenerateCypher, GenerateFunction, MapQuestionToSchema
from tools.graphrag_tools import GraphRAGToolContext

from common.config import get_graphrag_config
from common.llm_services.base_llm import (
    start_usage_collection, get_collected_usage, reset_usage_collection,
)
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics
from common.py_schemas import GraphRAGResponse

logger = logging.getLogger(__name__)


class _Triage(BaseModel):
    """Front-desk classification of a user message before any DB/MCP work."""
    needs_retrieval: bool = Field(
        description="True if answering requires looking up the user's data in the "
        "knowledge graph. False for greetings, small talk, thanks/goodbye, or "
        "questions about the assistant itself (who/what are you, what can you do)."
    )
    answer: str = Field(
        default="",
        description="When needs_retrieval is False, the complete direct answer to "
        "give the user. Empty when needs_retrieval is True.",
    )


def _triage_question(llm, question, convo):
    """One cheap classify-and-answer call. Returns a ``_Triage`` or ``None`` if
    triage itself fails (caller then proceeds with normal retrieval). Uses only
    the question + conversation — no schema, no MCP, no DB."""
    try:
        user = (
            f"## Conversation\n{json.dumps(convo or [])[:2000]}\n\n"
            f"## Message\n{question}"
        )
        # Customizable routing prompt (fixed contract + operator-editable
        # routing policy); the default lives in base_llm.
        return llm.invoke_structured(
            [("system", llm.agentic_triage_prompt), ("user", user)],
            _Triage, caller_name="agentic_triage",
        )
    except Exception as exc:
        logger.warning(f"agentic triage skipped ({exc}); proceeding with retrieval")
        return None


def _resolve_style(requested, config_style) -> str:
    """Resolve the agentic orchestrator to ``"planned"`` or ``"react"``.

    A per-request ``requested`` style wins unless it's ``"auto"``, in which
    case the graph's ``config_style`` applies. Only ``"planned"`` selects the
    planner DAG; everything else (``"reactive"`` from the UI, ``"react"`` from
    config, or any unknown value) is the free tool-calling loop.
    """
    requested = (requested or "auto").lower()
    chosen = config_style if requested == "auto" else requested
    return "planned" if str(chosen).lower() == "planned" else "react"


class AgenticAgent:
    def __init__(
        self,
        llm_provider,
        db_connection,
        embedding_model,
        embedding_store,
        use_cypher: bool = False,
        ws=None,
        supportai_retriever="auto",   # accepted for API parity; agentic plans dynamically
        agent_style="auto",           # "auto" (per config) | "planned" | "reactive"
    ):
        # Per-request orchestrator override. "auto" defers to the graph's
        # configured agent_style; "planned"/"reactive" force a style.
        self.agent_style = (agent_style or "auto").lower()
        self.conn = db_connection
        self.llm = llm_provider
        self.model_name = embedding_model.model_name
        self.embedding_model = embedding_model
        self.embedding_store = embedding_store
        if self.embedding_store.conn.graphname != self.conn.graphname:
            self.embedding_store.set_graphname(self.conn.graphname)

        self.mq2s = MapQuestionToSchema(self.conn, self.llm)
        self.gen_func = GenerateFunction(
            self.conn, self.llm, embedding_model, embedding_store
        )
        # Structural retrieval uses generate_function first and falls back to
        # cypher on empty results (matches classic capability), so wire cypher
        # whenever the deployment enables it.
        self.use_cypher = use_cypher
        self.cypher_gen = GenerateCypher(self.conn, self.llm) if use_cypher else None

        self.q = Q() if ws is not None else None
        self._ws = ws

        logger.debug(f"request_id={req_id_cv.get()} agentic agent initialized")

    def emit_progress(self, msg: str) -> None:
        if self.q is not None:
            self.q.put(msg)

    def question_for_agent(
        self, question: str, conversation: List[Dict[str, str]] = None
    ):
        start_time = time.time()
        metrics.llm_inprogress_requests.labels(self.model_name).inc()
        start_usage_collection()
        try:
            LogWriter.info(f"request_id={req_id_cv.get()} ENTRY agentic question_for_agent")
            # Emit an initial progress message as soon as the question arrives.
            self.emit_progress("Thinking")
            convo = [
                {"query": c["query"], "response": c["response"]}
                for c in (conversation or [])
            ]
            # Front-desk triage: answer greetings and questions about the
            # assistant itself directly, before any schema read, MCP discovery,
            # or retrieval. Only short-circuits when the model is confident no
            # knowledge-graph lookup is needed AND produced an answer.
            triage = _triage_question(self.llm, question, convo)
            if triage is not None and not triage.needs_retrieval and triage.answer.strip():
                self.emit_progress(DONE)
                LogWriter.info(
                    f"request_id={req_id_cv.get()} agentic triage answered directly "
                    "(no retrieval)"
                )
                return GraphRAGResponse(
                    natural_language_response=triage.answer.strip(),
                    answered_question=True,
                    response_type="agentic",
                    query_sources={
                        "engine": "triage",
                        "agent_steps": [{
                            "node": "triage", "kind": "answer",
                            "output": {"answer": triage.answer.strip()},
                        }],
                        "citations": [],
                    },
                )
            # Per-user creds for the tigergraph-mcp tools (when available), so
            # those tool calls run as the logged-in user too.
            tg_cfg = None
            try:
                from tools.tg_mcp_tools import conn_config_from_conn
                tg_cfg = conn_config_from_conn(self.conn, self.conn.graphname)
            except Exception:
                tg_cfg = None

            # Make sure any tarball-backed stdio servers configured for this
            # graph are installed before we try to launch them (a server saved
            # since the last restart may not have been installed at startup).
            try:
                from common.config import get_mcp_servers
                from common.mcp_config import ensure_libraries_installed
                ensure_libraries_installed(get_mcp_servers(self.conn.graphname))
            except Exception as exc:
                logger.warning(f"agentic: mcp library ensure-install skipped: {exc}")

            # Discover external MCP-addon tools for this graph. One bad
            # server doesn't blank the catalog; an empty config returns {}.
            external_tools: Dict[str, object] = {}
            mcp_manager = None
            try:
                from mcp_addons import discover_tools, get_manager, run_sync as _mcp_run_sync
                mcp_manager = _mcp_run_sync(get_manager(self.conn.graphname), timeout=10.0)
                if mcp_manager and mcp_manager.server_names:
                    external_tools = discover_tools(mcp_manager)
                    if external_tools:
                        logger.info(
                            f"agentic: mcp_addons discovered "
                            f"{len(external_tools)} external tool(s) for graph={self.conn.graphname}"
                        )
            except Exception as exc:
                logger.warning(f"agentic: mcp_addons discovery skipped: {exc}")

            # Logged-in user, when available (used for per-call _meta on MCP tools).
            user = getattr(self.conn, "username", None)

            ctx = GraphRAGToolContext(
                conn=self.conn,
                llm_provider=self.llm,
                embedding_model=self.embedding_model,
                embedding_store=self.embedding_store,
                mq2s=self.mq2s,
                gen_func=self.gen_func,
                graphrag_cfg=get_graphrag_config(self.conn.graphname),
                cypher_gen=self.cypher_gen,
                use_cypher=self.use_cypher,
                conversation=convo,
                progress=self.emit_progress,
                tg_connection_config=tg_cfg,
                external_tools=external_tools,
                mcp_manager=mcp_manager,
                user=user,
            )
            # agent_style picks the orchestrator: "planned" (planner ->
            # executor DAG) vs the free tool-calling loop ("autonomous",
            # internally ReAct). A per-request style overrides the graph
            # config; "auto" defers to the configured default.
            config_style = (ctx.graphrag_cfg or {}).get("agent_style", "planned")
            style = _resolve_style(self.agent_style, config_style)
            try:
                if style == "planned":
                    answer = run_agentic(ctx, self.llm, question, convo)
                else:
                    # "reactive" (UI) / "react" (config) -> free tool-calling loop
                    answer = run_react(ctx, self.llm, question, convo)
            except Exception as run_exc:
                # Runtime backstop (GML-2169): if the model turns out not to
                # support tool-calling, disable Agentic for it and answer via the
                # classic engine. Only trigger on a confident tool-support signal;
                # any other error propagates to the normal handler.
                from common.llm_services.capabilities import (
                    _looks_like_no_tool_support,
                    mark_tool_calling_unsupported,
                )
                if not _looks_like_no_tool_support(run_exc):
                    raise
                logger.warning(
                    f"request_id={req_id_cv.get()} agentic run hit a tool-calling "
                    f"failure ({str(run_exc)[:200]}); disabling Agentic for this "
                    "model and falling back to the classic engine"
                )
                mark_tool_calling_unsupported(self.llm.config)
                from agent.agent import make_agent
                classic = make_agent(
                    self.conn.graphname, self.conn, self.use_cypher,
                    ws=self._ws, mode="classic",
                )
                return classic.question_for_agent(question, conversation)

            # Aggregate usage across all LLM calls in this run for the UI.
            usage = get_collected_usage() or []
            total_usage = {
                "input_tokens": sum(int(u.get("input_tokens", 0) or 0) for u in usage),
                "output_tokens": sum(int(u.get("output_tokens", 0) or 0) for u in usage),
                "total_tokens": sum(int(u.get("total_tokens", 0) or 0) for u in usage),
                "cost": sum(float(u.get("cost", 0) or 0) for u in usage),
            }
            if answer.query_sources is None:
                answer.query_sources = {}
            answer.query_sources["token_usage"] = total_usage
            # Tag the orchestrator that ran so the UI can show the Agent style
            # consistently ("planned" | "react") rather than a retriever method.
            answer.query_sources["engine"] = style
            # Map plan steps onto the agent_steps shape the Trace UI renders.
            answer.query_sources.setdefault("agent_steps", [
                {"node": s.get("step_id"), "output": s.get("summary", "")}
                for s in answer.query_sources.get("steps", [])
            ])

            LogWriter.info(f"request_id={req_id_cv.get()} EXIT agentic question_for_agent")
            return answer
        except Exception as e:
            metrics.llm_query_error_total.labels(self.model_name).inc()
            LogWriter.error(f"request_id={req_id_cv.get()} FAILURE agentic question_for_agent")
            import traceback
            traceback.print_exc()
            raise e
        finally:
            self.emit_progress(DONE)
            reset_usage_collection()
            metrics.llm_request_total.labels(self.model_name).inc()
            metrics.llm_inprogress_requests.labels(self.model_name).dec()
            metrics.llm_request_duration_seconds.labels(self.model_name).observe(
                time.time() - start_time
            )
