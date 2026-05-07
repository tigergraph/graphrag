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

import boto3
import os
import re
import json
import logging
from typing import Dict, List, Optional

from agent.agent_generation import TigerGraphAgentGenerator
from agent.agent_hallucination_check import TigerGraphAgentHallucinationCheck
from agent.agent_rewrite import TigerGraphAgentRewriter
from agent.agent_router import TigerGraphAgentRouter
from agent.agent_usefulness_check import TigerGraphAgentUsefulnessCheck
from agent.method_selector import (
    CHUNK_BASED_METHODS,
    INLANE_FALLBACK_TABLE,
    RetrieverSelector,
    has_insufficient_context,
)
from agent.Q import DONE, Q
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langgraph.graph import END, StateGraph
from pyTigerGraph.common.exception import TigerGraphException
from supportai.retrievers import (HybridRetriever, SimilarityRetriever,
                                  SiblingRetriever, CommunityRetriever)
from tools import MapQuestionToSchemaException
from typing_extensions import TypedDict

from common.logs.log import req_id_cv
from common.metrics.prometheus_metrics import metrics as pmetrics
from common.py_schemas import GraphRAGResponse, MapQuestionToSchemaResponse
from common.llm_services.aws_bedrock_service import AWSBedrock
from common.config import get_graphrag_config

logger = logging.getLogger(__name__)


class GraphState(TypedDict):
    """
    Represents the state of the agent graph.

    """

    question: str
    conversation: Optional[List[Dict[str, str]]]
    generation: str
    context: str
    answer: Optional[GraphRAGResponse]
    lookup_source: Optional[str]
    schema_mapping: Optional[MapQuestionToSchemaResponse]
    error_history: list[dict] = []
    question_retry_count: int = 0
    # Auto-selection (populated when supportai_retriever == "auto"; also written
    # for manual mode so the UI can render which retriever ran). The "source"
    # field distinguishes "rules"/"llm"/"fallback" (auto) from "manual".
    chosen_retriever: Optional[str]
    chosen_retriever_reason: Optional[str]
    chosen_retriever_source: Optional[str]
    # Cross-lane fallback: set by route_question after generate_function/cypher
    # retries are exhausted, so supportai_search knows to flip the source label
    # to "router_fallback" and force auto-selection.
    router_fallback_attempted: Optional[bool]
    # In-lane fallback: set by supportai_search when the first chunk-based method
    # returned fewer than top_k chunks and we ran a second method via
    # INLANE_FALLBACK_TABLE. The "_from" field records the original method.
    inlane_fallback_attempted: Optional[bool]
    inlane_fallback_from: Optional[str]


class TigerGraphAgentGraph:
    def __init__(
        self,
        llm_provider,
        db_connection,
        embedding_model,
        embedding_store,
        mq2s_tool,
        gen_func_tool,
        cypher_gen_tool=None,
        enable_human_in_loop=False,
        q: Q = None,
        supportai_retriever="auto",
    ):
        self.workflow = StateGraph(GraphState)
        self.llm_provider = llm_provider
        self.db_connection = db_connection
        self.embedding_model = embedding_model
        self.embedding_store = embedding_store
        self.mq2s = mq2s_tool
        self.gen_func = gen_func_tool
        self.cypher_gen = cypher_gen_tool
        self.enable_human_in_loop = enable_human_in_loop
        self.q = q

        self._graphrag_cfg = get_graphrag_config(db_connection.graphname)
        self.supportai_enabled = True
        self.supportai_retriever = supportai_retriever.lower().replace(" ", "")
        try:
            vtypes = self.db_connection.getVertexTypes()
            if "DocumentChunk" not in vtypes:
                raise ValueError("DocumentChunk vertex type not found")
        except Exception as e:
            logger.info(f"SupportAI schema not found in graph {self.db_connection.graphname}. Disabling supportai.")
            self.supportai_enabled = False

    def emit_progress(self, msg):
        if self.q is not None:
            self.q.put(msg)

    def entry(self, state):
        if state.get("question_retry_count") is None:
            state["question_retry_count"] = 0
        else:
            state["question_retry_count"] += 1
        return state

    _GREETING_PATTERNS = re.compile(
        r"^("
        r"h(i|ello|ey|owdy|iya)(\s+there)?|"
        r"yo+|sup|what'?s\s*up|"
        r"good\s+(morning|afternoon|evening|night|day)|"
        r"greetings|"
        r"thanks?(\s+you)?|thank\s+you(\s+so\s+much)?|"
        r"bye|goodbye|see\s+you|take\s+care"
        r")$",
        re.IGNORECASE,
    )

    def _is_greeting(self, question: str) -> bool:
        """Check if the question is a simple greeting or non-question."""
        normalized = question.strip().rstrip("!?.,;")
        return bool(self._GREETING_PATTERNS.match(normalized))

    def greet(self, state):
        """Respond to greetings and ask the user to provide a real question."""
        self.emit_progress(DONE)
        state["answer"] = GraphRAGResponse(
            natural_language_response="Hello! I'm your knowledge graph assistant. Please ask a question about your data and I'll do my best to help.",
            answered_question=False,
            response_type="greeting",
            query_sources={},
        )
        return state

    def route_question(self, state):
        """
        Run the agent router.

        When generate_function / generate_cypher have failed to produce a usable
        answer through 3 rewrite cycles (`question_retry_count > 2`), instead of
        going straight to `apologize`, fall back to vector search if available.
        `supportai_search` reads `state["router_fallback_attempted"]` to flip its
        source label and re-run auto-selection regardless of configured method.
        """
        if state["question_retry_count"] > 2:
            if self.supportai_enabled and not state.get("router_fallback_attempted"):
                state["router_fallback_attempted"] = True
                self.emit_progress("Trying a different approach…")
                return "supportai_lookup"
            return "apologize"
        if self._is_greeting(state["question"]):
            return "greeting"
        self.emit_progress("Thinking")
        step = TigerGraphAgentRouter(self.llm_provider, self.db_connection)
        logger.debug_pii(
            f"request_id={req_id_cv.get()} Routing question: {state['question']}"
        )
        source = step.route_question(state["question"], state["conversation"])
        logger.debug_pii(
            f"request_id={req_id_cv.get()} Routing question to: {source}"
        )
        if self.supportai_enabled and source.datasource == "vectorstore":
            return "supportai_lookup"
        elif source.datasource == "history":
            return "history_lookup"
        else:
            return "inquiryai_lookup"

    def apologize(self, state):
        """
        Apologize for not being able to answer the question.
        """
        self.emit_progress(DONE)
        state["answer"] = GraphRAGResponse(
            natural_language_response="I'm sorry, there isn't enough context to answer your question. Please try rephrasing it.",
            answered_question=False,
            response_type="error",
            query_sources={"error": True, "error_history": state["error_history"]},
        )
        return state

    def contextualize_question(self, question: str, conversation) -> str:
        """Rewrite *question* into a self-contained search query by
        incorporating relevant context from *conversation*.  Falls back to
        the original question on any error."""
        if not conversation:
            return question
        try:
            history_lines = []
            for turn in conversation[-4:]:
                if isinstance(turn, dict):
                    q = turn.get("query", "")
                    a = turn.get("response", "")
                    if q:
                        history_lines.append(f"User: {q}")
                    if a:
                        history_lines.append(f"Assistant: {a}")
            if not history_lines:
                return question

            history_text = "\n".join(history_lines)
            prompt = PromptTemplate(
                template=self.llm_provider.contextualize_question_prompt,
                input_variables=["history", "question"],
            )
            standalone = self.llm_provider.invoke_with_parser(
                prompt, StrOutputParser(),
                {"history": history_text, "question": question},
                caller_name="contextualize_question",
            ).strip()
            logger.info(f"Contextualized question for KG search: {standalone}")
            return standalone or question
        except Exception as e:
            logger.warning(f"Failed to contextualize question, using original: {e}")
            return question

    def lookup_history(self, state):
        """
        Prepare for a history-based answer.  Contextualizes the question
        using conversation history so the downstream ``supportai`` node can
        perform a meaningful KG search.  The original question and a
        ``history_mode`` flag are stashed in state for
        ``merge_history_context`` to use later.
        """
        self.emit_progress("Looking up the conversation history")
        state["history_mode"] = True
        state["original_question"] = state["question"]

        if self.supportai_enabled:
            state["question"] = self.contextualize_question(
                state["question"], state["conversation"]
            )
        else:
            state["lookup_source"] = "history"
            state["context"] = {
                "result": state["conversation"],
                "reasoning": (
                    "The conversation history was used to answer the question."
                ),
            }
        return state

    def merge_history_context(self, state):
        """
        Merge the KG search results produced by the ``supportai`` node with
        the original conversation history, then restore the original question
        for answer generation.
        """
        kg_result = {}
        if state.get("context") and state["context"].get("result"):
            kg_result = state["context"]["result"].get("final_retrieval", {})

        combined = {
            "conversation_history": state["conversation"],
            "knowledge_graph": kg_result,
        }

        state["question"] = state.pop("original_question", state["question"])
        state.pop("history_mode", None)
        state["lookup_source"] = "history"
        state["context"] = {
            "result": combined,
            "reasoning": (
                "The conversation history and knowledge graph search results "
                "were combined to answer the question."
            ),
        }
        return state

    def map_question_to_schema(self, state):
        """
        Run the agent schema mapping.
        """
        self.emit_progress("Mapping your question to the graph's schema")
        try:
            step = self.mq2s._run(state["question"], state["conversation"])
            logger.info(f"schema_mapping: {step}")
            state["schema_mapping"] = step
            return state
        except MapQuestionToSchemaException as e:
            state["context"] = {"error": True}
            if "error_history" not in state or state["error_history"] is None:
                state["error_history"] = []
            state["error_history"].append({"error_message": str(e), "error_step": "generate_function"})

    def generate_function(self, state):
        """
        Run the agent function generator.
        """
        self.emit_progress("Generating the code to answer your question")
        try:
            step = self.gen_func._run(
                state["question"],
                state["schema_mapping"].target_vertex_types,
                state["schema_mapping"].target_vertex_attributes,
                state["schema_mapping"].target_vertex_ids,
                state["schema_mapping"].target_edge_types,
                state["schema_mapping"].target_edge_attributes,
            )
            logger.info(f"generate_function: {step}")
            state["context"] = step
        except Exception as e:
            state["context"] = {"error": True}
            if "error_history" not in state or state["error_history"] is None:
                state["error_history"] = []
            state["error_history"].append({"error_message": str(e), "error_step": "generate_function"})
        state["lookup_source"] = "inquiryai"
        return state

    def generate_cypher(self, state):
        """
        Run the agent cypher generator.
        """
        self.emit_progress("Generating the query to answer your question")
        gen_history = []
        response_json = None
        cypher = None
        json_str = None
        response = None

        for i in range(3):
            try:
                cypher = self.cypher_gen._run(state["question"], gen_history)
            except ValueError as e:
                logger.warning(f"Cypher generation failed: {e}")
                gen_history.append(f"{i}: Error: {e}\n")
                continue
            response = self.db_connection.gsql(cypher)
            response_lines = response.split("\n")
            json_str = "\n".join(response_lines[1:])
            try:
                response_json = json.loads(json_str)
                break
            except Exception as e:
                gen_history.append(f"{i}: {cypher}\n\tError: {json_str}\n")
        if response_json and not self.is_query_result_empty(response_json["results"][0]):
            state["context"] = {
                "result": response_json["results"][0],
                "cypher": cypher,
                "reasoning": "The following OpenCypher query was executed to answer the question. {}".format(
                    cypher
                ),
            }
        else:
            state["context"] = {
                "error": True,
                "cypher": cypher,
                "result": json_str
            }
            if "error_history" not in state or state["error_history"] is None:
                state["error_history"] = []
            
            error_msg = response if response else "LLM failed to produce a valid Cypher query after 3 attempts"
            state["error_history"].append({"error_message": error_msg, "error_step": "generate_cypher"})

        state["lookup_source"] = "cypher"
        return state

    def hybrid_search(self, state):
        """
        Run the agent overlap search.
        """
        self.emit_progress("Searching the knowledge graph")
        retriever = HybridRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider,
            self.db_connection,
        )
        chunk_only=self._graphrag_cfg.get("chunk_only", True)
        step = retriever.search(
            state["question"],
            indices=["DocumentChunk"],
            top_k=self._graphrag_cfg.get("top_k", 5),
            num_seen_min=self._graphrag_cfg.get("num_seen_min", 2),
            num_hops=self._graphrag_cfg.get("num_hops", 2),
            chunk_only=chunk_only,
            doc_only=self._graphrag_cfg.get("doc_only", False),
        )

        query_name = "GraphRAG_Hybrid_Vector_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def similarity_search(self, state):
        """
        Run the agent vector search.
        """
        self.emit_progress("Searching the vector store")
        retriever = SimilarityRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider,
            self.db_connection
        )

        step = retriever.search(
            state["question"],
            index="DocumentChunk",
            top_k=self._graphrag_cfg.get("top_k", 5)
        )

        query_name = "Content_Similarity_Vector_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def sibling_search(self, state):
        """
        Run the agent sibling contextual search.
        """
        self.emit_progress("Searching the knowledge graph")
        retriever = SiblingRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider,
            self.db_connection,
        )
        step = retriever.search(
            state["question"],
            index="DocumentChunk",
            top_k=self._graphrag_cfg.get("top_k", 5)
        )

        query_name = "Chunk_Sibling_Vector_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def community_search(self, state):
        """
        Run the agent graphrag community search.
        """
        self.emit_progress("Searching the knowledge graph")
        retriever = CommunityRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider,
            self.db_connection,
        )
        step = retriever.search(
            state["question"],
            community_level=self._graphrag_cfg.get("community_level", 2),
            top_k=self._graphrag_cfg.get("top_k", 5),
            with_chunk=self._graphrag_cfg.get("with_chunk", True),
        )

        query_name = "GraphRAG_Community_Vector_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
        }
        state["lookup_source"] = "supportai"
        return state
    
    # User-friendly labels for the four retrieval methods. Used in progress
    # events and UI badges; keep in sync with method_selector.METHOD_* constants.
    _METHOD_DISPLAY_NAMES = {
        "similaritysearch": "Similarity",
        "contextualsearch": "Contextual",
        "hybridsearch": "Hybrid",
        "communitysearch": "Community",
    }

    def _dispatch_retriever(self, method, state):
        """Run the retriever named by `method` and return the updated state.

        Centralises the if/elif chain so it can be called twice (for in-lane
        fallback) without duplication.
        """
        if method == "hybridsearch":
            return self.hybrid_search(state)
        elif method == "similaritysearch":
            return self.similarity_search(state)
        elif method == "contextualsearch":
            return self.sibling_search(state)
        elif method == "communitysearch":
            return self.community_search(state)
        raise ValueError(f"Invalid supportai retriever: {method}")

    def _record_selection_metric(self, method, source):
        """Increment the selection counter; never lets a metric error break the request."""
        try:
            pmetrics.llm_method_selection_total.labels(
                selected_method=method, selection_source=source
            ).inc()
        except Exception:  # noqa: BLE001
            pass

    def supportai_search(self, state):
        """
        Run the agent supportai search.

        Three layers of behavior:

        1. **Method selection.** When `self.supportai_retriever == "auto"` (the
           default), picks a method via `RetrieverSelector`. When configured to
           a specific method, uses that. **Exception:** when reached via
           cross-lane fallback (`state["router_fallback_attempted"]`), forces
           auto-selection regardless of configuration — manual users still get
           the best vector method when the structured-data path has exhausted
           its retries.
        2. **In-lane fallback.** After the first chunk-based retriever runs, if
           it returned fewer than `top_k` chunks (signal: insufficient context),
           runs a second method per `INLANE_FALLBACK_TABLE` and uses its context
           for downstream generation. Single retry only; skipped for manual
           mode and community search.
        3. **Out-of-corpus short-circuit.** If after all retrieval attempts the
           result is still empty, marks the context so `generate_answer`
           returns an honest "couldn't find" message instead of letting the
           LLM hallucinate from empty context.

        State written: `chosen_retriever{,_reason,_source}` populated for the
        UI/telemetry; mirrored into `state["context"]` so it lands on
        `GraphRAGResponse.query_sources` without further plumbing.
        """
        is_router_fallback = bool(state.get("router_fallback_attempted"))

        method = self.supportai_retriever
        chosen_reason = "user-selected"
        chosen_source = "manual"

        # In router_fallback mode we always auto-select, even for manual users —
        # they need the best vector method now that structured-data is dead.
        if is_router_fallback or method == "auto":
            selector = RetrieverSelector(self.llm_provider, self.db_connection)
            choice = selector.choose(state["question"], state.get("conversation"))
            method = choice.method
            chosen_reason = choice.reason
            chosen_source = choice.source

        if is_router_fallback:
            chosen_source = "router_fallback"
            chosen_reason = f"{chosen_reason} (after structured-data retries exhausted)"
            label = self._METHOD_DISPLAY_NAMES.get(method, method)
            self.emit_progress(f"Trying a different approach: {label} search")
        elif chosen_source != "manual":
            label = self._METHOD_DISPLAY_NAMES.get(method, method)
            self.emit_progress(f"Auto-selected {label} search")

        state["chosen_retriever"] = method
        state["chosen_retriever_reason"] = chosen_reason
        state["chosen_retriever_source"] = chosen_source
        self._record_selection_metric(method, chosen_source)

        # First retrieval attempt
        result_state = self._dispatch_retriever(method, state)

        # In-lane fallback (Feature 2) — chunk-based methods only, single retry,
        # skipped for manual users so we don't second-guess their pick.
        ctx = result_state.get("context") if isinstance(result_state.get("context"), dict) else {}
        result = ctx.get("result") if isinstance(ctx.get("result"), dict) else {}
        final_retrieval = result.get("final_retrieval") if isinstance(result, dict) else None
        top_k = self._graphrag_cfg.get("top_k", 5)
        can_inlane_fallback = (
            chosen_source != "manual"
            and method in CHUNK_BASED_METHODS
            and not result_state.get("inlane_fallback_attempted")
            and has_insufficient_context(final_retrieval, method, top_k)
        )
        if can_inlane_fallback:
            fallback_method = INLANE_FALLBACK_TABLE.get(method)
            if fallback_method:
                label_old = self._METHOD_DISPLAY_NAMES.get(method, method)
                label_new = self._METHOD_DISPLAY_NAMES.get(fallback_method, fallback_method)
                self.emit_progress(
                    f"Insufficient context from {label_old} search, trying {label_new} search"
                )
                result_state["inlane_fallback_attempted"] = True
                result_state["inlane_fallback_from"] = method
                # Update the active method/source for the second pass.
                method = fallback_method
                chosen_source = "inlane_fallback"
                chosen_reason = f"fallback from {label_old} (returned fewer than top_k chunks)"
                self._record_selection_metric(method, chosen_source)
                result_state = self._dispatch_retriever(method, result_state)

        # Mirror the (final) choice onto the context dict so it lands on
        # GraphRAGResponse.query_sources without further plumbing.
        ctx = result_state.get("context") or {}
        if isinstance(ctx, dict):
            ctx["chosen_retriever"] = method
            ctx["chosen_retriever_reason"] = chosen_reason
            ctx["chosen_retriever_source"] = chosen_source
            if result_state.get("inlane_fallback_attempted"):
                ctx["inlane_fallback_from"] = result_state.get("inlane_fallback_from")
            if is_router_fallback:
                ctx["router_fallback"] = True

            # Out-of-corpus short-circuit — applies after all retrieval attempts.
            # If the chosen retriever (or its fallback) returned nothing, mark
            # the context so generate_answer returns an honest "couldn't find"
            # message instead of hallucinating from empty context.
            result = ctx.get("result") if isinstance(ctx.get("result"), dict) else {}
            final_retrieval = result.get("final_retrieval") if isinstance(result, dict) else None
            if not final_retrieval:
                ctx["out_of_corpus"] = True
                self.emit_progress(
                    f"No relevant information found in the knowledge graph "
                    f"for {method} search"
                )

            result_state["context"] = ctx

        return result_state
    
    def generate_answer(self, state):
        """
        Run the agent generator.
        """
        self.emit_progress("Connecting the pieces")
        step = TigerGraphAgentGenerator(self.llm_provider)
        logger.debug_pii(
            f"request_id={req_id_cv.get()} Generating answer for question: {state['question']}"
        )

        if state["lookup_source"] == "supportai":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["result"]}"""
            )

            # Phase 1.5 — out-of-corpus short-circuit. supportai_search flagged
            # the context as having no usable retrieval results; produce an
            # honest "couldn't find" answer instead of letting the LLM
            # hallucinate from empty context.
            if isinstance(state.get("context"), dict) and state["context"].get("out_of_corpus"):
                method = state.get("chosen_retriever") or self.supportai_retriever
                label = self._METHOD_DISPLAY_NAMES.get(method, method)
                ooc_msg = (
                    "I couldn't find relevant information about this topic in "
                    "the knowledge graph (using "
                    f"{label} search). The corpus may not cover this question — "
                    "try rephrasing or asking about a topic the documents discuss."
                )
                resp = GraphRAGResponse(
                    natural_language_response=ooc_msg,
                    answered_question=False,
                    response_type="supportai",
                    query_sources=state["context"],
                )
                state["answer"] = resp
                logger.info(
                    f"request_id={req_id_cv.get()} out-of-corpus short-circuit "
                    f"(method={method})"
                )
                return state

            context = state["context"]["result"]["final_retrieval"]
            citations = sorted(list(context.keys()))
            answer = step.generate_answer(
                state["question"], context
            )

            if answer.citation:
                for citation in answer.citation:
                    if citation in citations:
                        citations[citations.index(citation)] = f"* {citation}"
                    else:
                        logger.info(f"Answer citation {citation} not found in the context")
            state["context"]["reasoning"] = citations

        elif state["lookup_source"] == "inquiryai":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["result"]}"""
            )
            try:
                context_data_str = json.dumps(state["context"]["result"])
            except (TypeError, ValueError) as e:
                logger.error(f"Failed to serialize context to JSON: {e}")
                raise ValueError("Invalid context data format. Unable to convert to JSON.")

            answer = step.generate_answer(state["question"], state["context"]["result"])

        elif state["lookup_source"] == "cypher":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["result"]}"""
            )
            answer = step.generate_answer(state["question"], state["context"]["result"], state["context"]["cypher"])

        elif state["lookup_source"] == "history":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["result"]}"""
            )
            answer = step.generate_answer(state["question"], state["context"]["result"])

        logger.debug_pii(
            f"request_id={req_id_cv.get()} Generated answer: {answer.generated_answer}"
        )

        try:
            # Replace S3 URLs with presigned URLs (for AWS Bedrock BDA processing)
            if isinstance(self.llm_provider, AWSBedrock):
                answer.generated_answer = self.replace_s3_urls_with_presigned(answer.generated_answer)
            
            # Convert [IMAGE_REF:image_id] to markdown images for React UI
            # This converts internal image references to URLs that the UI can display
            answer.generated_answer = self.convert_image_refs_to_markdown(answer.generated_answer)
            
            resp = GraphRAGResponse(
                natural_language_response=answer.generated_answer,
                answered_question=True,
                response_type=state["lookup_source"],
                query_sources=state["context"],
            )
        except Exception as e:
            resp = GraphRAGResponse(
                natural_language_response="I'm sorry, I don't know the answer to that question.",
                answered_question=False,
                response_type=state["lookup_source"],
                query_sources={"error": True, "error_history": state["error_history"]},
            )
        state["answer"] = resp

        return state

    def replace_s3_urls_with_presigned(self, content, expires_in=3600):
        """
        Recursively detects S3 URLs in content (string, list, or dict) 
        and replaces them with presigned URLs.

        Args:
            content (Any): String, dict, or list containing potential S3 URLs.
            expires_in (int): Expiration time for the presigned URL in seconds.

        Returns:
            Any: Content with S3 URLs replaced by presigned URLs (same type as input).
        """

        s3_url_pattern = r'\(s3://([^/]+)/([^\)]+)\)'
        s3 = boto3.client('s3')

        def presign(match):
            bucket, key = match.group(1), match.group(2)
            try:
                url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': key},
                    ExpiresIn=expires_in
                )
                return f"({url})"
            except Exception as e:
                logger.error(f"Failed to presign S3 url for s3://{bucket}/{key}: {e}")
                return f"({match.group(0)})"

        def process(value):
            if isinstance(value, str):
                return re.sub(s3_url_pattern, presign, value)
            elif isinstance(value, list):
                return [process(v) for v in value]
            elif isinstance(value, dict):
                return {k: process(v) for k, v in value.items()}
            else:
                return value

        return process(content)

    def convert_image_refs_to_markdown(self, text):
        """
        Convert tg:// protocol URLs to actual API endpoint URLs for images stored in TigerGraph.
        
        Creates relative URLs pointing to the /ui/image_vertex/ endpoint which serves images 
        from TigerGraph. The endpoint uses standard HTTP Basic Authentication (same pattern as 
        other endpoints), so credentials are handled via HTTP headers, not URL parameters.
        
        PATH_PREFIX is automatically handled by FastAPI router configuration.
        
        Format: ![description](tg://image_id) → ![description](/ui/image_vertex/{graphname}/{image_id})
        
        Args:
            text (str): The text containing markdown images with tg:// protocol.
            
        Returns:
            str: The text with tg:// URLs converted to endpoint URLs.
        """
        if not isinstance(text, str):
            return text
            
        if "(tg://" not in text:
            return text
        
        # Get graphname from connection
        graphname = self.db_connection.graphname
        
        # Replace tg://image_id with actual endpoint URL and count
        # Preserves the image description from markdown
        # Note: Authentication is handled via HTTP Basic Auth headers (standard FastAPI pattern)
        # PATH_PREFIX is already applied at router level in main.py, so use relative URL
        converted, count = re.subn(
            r'!\[([^\]]*)\]\(tg://([^\)]+)\)',
            rf'![\1](/ui/image_vertex/{graphname}/\2)',
            text
        )
        
        if count > 0:
            logger.info(f"Converted {count} tg:// image reference(s) to endpoint URLs")
            return converted
        else:
            return text

    def rewrite_question(self, state):
        """
        Run the agent question rewriter.
        """
        self.emit_progress("Rephrasing the question")
        step = TigerGraphAgentRewriter(self.llm_provider)
        question_str = state["question"]
        state["question"] = step.rewrite_question(question_str)
        return state

    def is_query_result_empty(self, query_result) -> bool:
        """
        Check if the query result is empty or contains empty values.
        """
        if query_result in ("", [], {}, (), set(), range(0), None):
            return True

        if isinstance(query_result, (list, set)):
            return all(self.is_query_result_empty(item) for item in query_result)

        if isinstance(query_result, dict):
            return all(self.is_query_result_empty(v) for v in query_result.values())

        return False

    # remove halucinaton check, always return grounded
    def check_answer_for_hallucinations(self, state):
        """
        Run the agent hallucination check.
        """
        # self.emit_progress("Checking the response is relevant")
        # step = TigerGraphAgentHallucinationCheck(self.llm_provider)

        # try:
        #     context_data_str = json.dumps(state["context"]["result"])
        #     # logger.info(f"context_data_str: {context_data_str}")
        # except (TypeError, ValueError) as e:
        #     logger.error(f"Failed to serialize context to JSON: {e}")
        #     raise ValueError("Invalid context data format. Unable to convert to JSON.")
        # hallucinations = step.check_hallucination(
        #     state["answer"].natural_language_response, context_data_str
        # )
        # logger.info(f"hallucination checker")
        # logger.info(f"answer: {state['answer'].natural_language_response}")
        # logger.info(f"context: {context_data_str}")
        # logger.info(f"if grounded: {hallucinations}")
        # if hallucinations.score == "yes":
        #     self.emit_progress(DONE)
        #     return "grounded"
        # else:
        #     return "hallucination"
        return "grounded"

    # remove usefulness check, always return useful
    def check_answer_for_usefulness(self, state):
        """
        Run the agent usefulness check.
        """
        # step = TigerGraphAgentUsefulnessCheck(self.llm_provider)

        # usefulness = step.check_usefulness(
        #     state["question"], state["answer"].natural_language_response
        # )
        # logger.info(f"usefulness checker")
        # logger.info(f"question: {state['question']}")
        # logger.info(f"answer: {state['answer'].natural_language_response}")
        # logger.info(f"if useful: {usefulness}")
        # if usefulness.score == "yes":
        #     return "useful"
        # else:
        #     return "not_useful"
        return "useful"

    def check_answer_for_usefulness_and_hallucinations(self, state):
        """
        Run the agent usefulness and hallucination check.
        """
        hallucinated = self.check_answer_for_hallucinations(state)
        if hallucinated == "hallucination":
            return "hallucination"
        else:
            useful = self.check_answer_for_usefulness(state)
            if useful == "useful":
                self.emit_progress(DONE)
                return "grounded"
            else:
                if state["lookup_source"] == "supportai":
                    return "supportai_not_useful"
                elif state["lookup_source"] == "inquiryai":
                    return "inquiryai_not_useful"
                elif state["lookup_source"] == "cypher":
                    return "cypher_not_useful"

    def check_state_for_generation_error(self, state):
        """
        Check if the state has an error.
        """
        if (
            state.get("context") is not None and
            (
                isinstance(state.get("context"), Exception) or
                state["context"].get("error") is not None
            )
        ):
            return "error"
        else:
            return "success"

    def route_after_supportai(self, state):
        """Route after supportai: if we came from history lookup, merge
        the KG results with history; otherwise proceed to answer generation."""
        if state.get("history_mode"):
            return "merge_history"
        return "generate"

    def create_graph(self):
        """
        Create a graph of the agent.
        """
        self.workflow.set_entry_point("entry")
        self.workflow.add_node("entry", self.entry)
        self.workflow.add_node("generate_answer", self.generate_answer)
        self.workflow.add_node("lookup_history", self.lookup_history)
        self.workflow.add_node("map_question_to_schema", self.map_question_to_schema)
        self.workflow.add_node("generate_function", self.generate_function)
        if self.supportai_enabled:
            self.workflow.add_node("supportai", self.supportai_search)
            self.workflow.add_node("merge_history_context", self.merge_history_context)
        self.workflow.add_node("rewrite_question", self.rewrite_question)
        self.workflow.add_node("apologize", self.apologize)
        self.workflow.add_node("greet", self.greet)

        if self.cypher_gen:
            self.workflow.add_node("generate_cypher", self.generate_cypher)
            self.workflow.add_conditional_edges(
                "generate_function",
                self.check_state_for_generation_error,
                {"error": "generate_cypher", "success": "generate_answer"},
            )

            if self.supportai_enabled:
                self.workflow.add_conditional_edges(
                    "generate_cypher",
                    self.check_state_for_generation_error,
                    {"error": "supportai", "success": "generate_answer"},
                )
            else:
                self.workflow.add_conditional_edges(
                    "generate_cypher",
                    self.check_state_for_generation_error,
                    {"error": "apologize", "success": "generate_answer"},
                )

            # remove hallucination and usefulness check
            if self.supportai_enabled:
                self.workflow.add_conditional_edges(
                    "generate_answer",
                    self.check_answer_for_usefulness_and_hallucinations,
                    {
                        "hallucination": "rewrite_question",
                        "grounded": END,
                        "inquiryai_not_useful": "generate_cypher",
                        "cypher_not_useful": "supportai",
                        "supportai_not_useful": "map_question_to_schema",
                    },
                )
            else:
                self.workflow.add_conditional_edges(
                    "generate_answer",
                    self.check_answer_for_usefulness_and_hallucinations,
                    {
                        "hallucination": "rewrite_question",
                        "grounded": END,
                        "inquiryai_not_useful": "generate_cypher",
                        "cypher_not_useful": "apologize",
                    },
                )
        else:
            self.workflow.add_conditional_edges(
                "generate_function",
                self.check_state_for_generation_error,
                {"error": "rewrite_question", "success": "generate_answer"},
            )

            if self.supportai_enabled:
                self.workflow.add_conditional_edges(
                    "generate_answer", 
                    # alwasy return grounded
                    self.check_answer_for_usefulness_and_hallucinations,
                    {
                        "hallucination": "rewrite_question",
                        "grounded": END,
                        "not_useful": "rewrite_question",
                        "inquiryai_not_useful": "supportai",
                        "supportai_not_useful": "map_question_to_schema",
                    },
                )
            else:
                self.workflow.add_conditional_edges(
                    "generate_answer", 
                    # always return grounded
                    self.check_answer_for_usefulness_and_hallucinations,
                    {
                        "hallucination": "rewrite_question",
                        "grounded": END,
                        "not_useful": "rewrite_question",
                        "inquiryai_not_useful": "apologize",
                        "supportai_not_useful": "map_question_to_schema",
                    },
                )

        if self.supportai_enabled:
            self.workflow.add_conditional_edges(
                "entry",
                self.route_question,
                {
                    "supportai_lookup": "supportai",
                    "inquiryai_lookup": "map_question_to_schema",
                    "history_lookup": "lookup_history",
                    "greeting": "greet",
                    "apologize": "apologize",
                },
            )
        else:
            self.workflow.add_conditional_edges(
                "entry",
                self.route_question,
                {
                    "inquiryai_lookup": "map_question_to_schema",
                    "history_lookup": "lookup_history",
                    "greeting": "greet",
                    "apologize": "apologize",
                },
            )

        if self.supportai_enabled:
            self.workflow.add_edge("lookup_history", "supportai")
            self.workflow.add_conditional_edges(
                "supportai",
                self.route_after_supportai,
                {
                    "merge_history": "merge_history_context",
                    "generate": "generate_answer",
                },
            )
            self.workflow.add_edge("merge_history_context", "generate_answer")
        else:
            self.workflow.add_edge("lookup_history", "generate_answer")
        self.workflow.add_edge("map_question_to_schema", "generate_function")
        self.workflow.add_edge("rewrite_question", "entry")
        self.workflow.add_edge("apologize", END)
        self.workflow.add_edge("greet", END)

        app = self.workflow.compile()
        return app
