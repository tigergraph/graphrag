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

"""Agentic chat agent (v2.0 deep-thinking mode).

Public-API twin of ``TigerGraphAgent``: same constructor shape and
``question_for_agent(question, conversation)`` returning a
``GraphRAGResponse``, and the same ``Q`` progress queue — so the WS and
REST entry points drive it identically. Internally it runs the
plan -> execute -> synthesize loop (``agentic_graph.run_agentic``) over the
GraphRAG tool layer instead of the fixed classic LangGraph.
"""

import logging
import time
from typing import Dict, List

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

logger = logging.getLogger(__name__)


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
    ):
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
            convo = [
                {"query": c["query"], "response": c["response"]}
                for c in (conversation or [])
            ]
            # Per-user creds for the tigergraph-mcp tools (when available), so
            # those tool calls run as the logged-in user too.
            tg_cfg = None
            try:
                from tools.tg_mcp_tools import conn_config_from_conn
                tg_cfg = conn_config_from_conn(self.conn, self.conn.graphname)
            except Exception:
                tg_cfg = None

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
            # agent_style picks the orchestrator: "react" (default, free
            # tool-calling loop) vs "planned" (planner -> executor DAG).
            style = (ctx.graphrag_cfg or {}).get("agent_style", "react").lower()
            if style == "planned":
                answer = run_agentic(ctx, self.llm, question, convo)
            else:
                answer = run_react(ctx, self.llm, question, convo)

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
