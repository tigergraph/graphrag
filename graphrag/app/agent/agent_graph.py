import json
import logging
from typing import Dict, List, Optional

from agent.agent_generation import TigerGraphAgentGenerator
from agent.agent_hallucination_check import TigerGraphAgentHallucinationCheck
from agent.agent_rewrite import TigerGraphAgentRewriter
from agent.agent_router import TigerGraphAgentRouter
from agent.agent_usefulness_check import TigerGraphAgentUsefulnessCheck
from agent.Q import DONE, Q
from langgraph.graph import END, StateGraph
from pyTigerGraph.common.exception import TigerGraphException
from supportai.retrievers import (HybridRetriever, SimilarityRetriever,
                                  SiblingRetriever, GraphRAGRetriever)
from tools import MapQuestionToSchemaException
from typing_extensions import TypedDict

from common.logs.log import req_id_cv
from common.py_schemas import GraphRAGResponse, MapQuestionToSchemaResponse

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
        supportai_retriever="hybridsearch",
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

        self.supportai_enabled = True
        self.supportai_retriever = supportai_retriever.lower()
        try:
            self.db_connection.getQueryMetadata("GraphRAG_Hybrid_Search")
        except TigerGraphException as e:
            logger.info(f"GraphRAG_Hybrid_Search not found in the graph {self.db_connection.graphname}. Disabling supportai.")
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

    def route_question(self, state):
        """
        Run the agent router.
        """
        if state["question_retry_count"] > 2:
            return "apologize"
        self.emit_progress("Thinking")
        step = TigerGraphAgentRouter(self.llm_provider, self.db_connection)
        logger.debug_pii(
            f"request_id={req_id_cv.get()} Routing question: {state['question']}"
        )
        if self.supportai_enabled:
            source = step.route_question(state["question"])
            logger.debug_pii(
                f"request_id={req_id_cv.get()} Routing question to: {source}"
            )
            if source.datasource == "vectorstore":
                return "supportai_lookup"
            elif source.datasource == "functions":
                return "inquiryai_lookup"
        else:
            return "inquiryai_lookup"

    def apologize(self, state):
        """
        Apologize for not being able to answer the question.
        """
        self.emit_progress(DONE)
        state["answer"] = GraphRAGResponse(
            natural_language_response="I'm sorry, I don't know the answer to that question. Please try rephrasing your question.",
            answered_question=False,
            response_type="error",
            query_sources={"error": True, "error_history": state["error_history"]},
        )
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
            if "error_history" not in state:
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
            if "error_history" not in state:
                state["error_history"] = []
            state["error_history"].append({"error_message": str(e), "error_step": "generate_function"})
        state["lookup_source"] = "inquiryai"
        return state

    def generate_cypher(self, state):
        """
        Run the agent cypher generator.
        """
        self.emit_progress("Generating the Cypher to answer your question")
        gen_history = []
        response_json = None

        for i in range(3):
            cypher = self.cypher_gen._run(state["question"], gen_history)
            logger.info(f"cypher: {cypher}")

            response = self.db_connection.gsql(cypher)
            response_lines = response.split("\n")
            json_str = "\n".join(response_lines[1:])
            try:
                response_json = json.loads(json_str)
                break
            except Exception as e:
                gen_history.append(f"{i}: {cypher}\n\tError: {json_str}\n")
        if response_json:
            state["context"] = {
                "answer": response_json["results"][0],
                "cypher": cypher,
                "reasoning": "The following OpenCypher query was executed to answer the question. {}".format(
                    cypher
                ),
            }
        else:
            state["context"] = {
                "error": True,
                "cypher": cypher,
                "answer": json_str
            }
            if state["error_history"] is None:
                state["error_history"] = []
            
            state["error_history"].append({"error_message": response, "error_step": "generate_cypher"})

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
            self.llm_provider.model,
            self.db_connection,
        )
        step = retriever.search(
            state["question"],
            indices=["Document", "DocumentChunk", "Entity", "Relationship"],
            top_k=5,
            num_seen_min=2,
            num_hops=3,
        )

        query_name = "GraphRAG_Hybrid_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
            "query_output_format": self.db_connection.getQueryMetadata(
                query_name
            )["output"],
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
            top_k=5
        )

        query_name = "Content_Similarity_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
            "query_output_format": self.db_connection.getQueryMetadata(
                query_name
            )["output"],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def sibling_search(self, state):
        """
        Run the agent sibling search.
        """
        self.emit_progress("Searching the knowledge graph")
        retriever = SiblingRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider.model,
            self.db_connection,
        )
        step = retriever.search(
            state["question"],
            index="DocumentChunk",
            top_k=3
        )

        query_name = "Chunk_Sibling_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
            "query_output_format": self.db_connection.getQueryMetadata(
                query_name
            )["output"],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def graphrag_search(self, state):
        """
        Run the agent graphrag search.
        """
        self.emit_progress("Searching the knowledge graph")
        retriever = GraphRAGRetriever(
            self.embedding_model,
            self.embedding_store,
            self.llm_provider.model,
            self.db_connection,
        )
        step = retriever.search(
            state["question"],
            community_level=2,
            top_k=5,
            with_chunk=True,
        )

        query_name = "GraphRAG_Community_Search"
        state["context"] = {
            "function_call": query_name,
            "result": step[0],
            "query_output_format": self.db_connection.getQueryMetadata(
                query_name
            )["output"],
        }
        state["lookup_source"] = "supportai"
        return state
    
    def supportai_search(self, state):
        """
        Run the agent supportai search.
        """
        if self.supportai_retriever == "hybridsearch":
            return self.hybrid_search(state)
        elif self.supportai_retriever == "similaritysearch":
            return self.similarity_search(state)
        elif self.supportai_retriever == "siblingsearch":
            return self.sibling_search(state)
        elif self.supportai_retriever == "graphrag":
            return self.graphrag_search(state)
        else:
            raise ValueError(f"Invalid supportai retriever: {self.supportai_retriever}")
    
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
            answer = step.generate_answer(
                state["question"], state["context"]["result"]
            )
        elif state["lookup_source"] == "inquiryai":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["result"]}"""
            )
            try:
                context_data_str = json.dumps(state["context"]["result"])
            except (TypeError, ValueError) as e:
                logger.error(f"Failed to serialize context to JSON: {e}")
                raise ValueError("Invalid context data format. Unable to convert to JSON.")

            answer = step.generate_answer(state["question"], context_data_str)

        elif state["lookup_source"] == "cypher":
            logger.debug_pii(
                f"""request_id={req_id_cv.get()} Got result: {state["context"]["answer"]}"""
            )
            answer = step.generate_answer(state["question"], state["context"]["answer"], state["context"]["cypher"])
        logger.debug_pii(
            f"request_id={req_id_cv.get()} Generated answer: {answer.generated_answer}"
        )

        if state["lookup_source"] == "supportai":
            import re

            citations = [re.sub(r"_chunk_\d+", "", x) for x in answer.citation]
            state["context"]["reasoning"] = list(set(citations))

        try:
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

    def rewrite_question(self, state):
        """
        Run the agent question rewriter.
        """
        self.emit_progress("Rephrasing the question")
        step = TigerGraphAgentRewriter(self.llm_provider)
        question_str = state["question"]
        state["question"] = step.rewrite_question(question_str)
        return state

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

    def create_graph(self):
        """
        Create a graph of the agent.
        """
        self.workflow.set_entry_point("entry")
        self.workflow.add_node("entry", self.entry)
        self.workflow.add_node("generate_answer", self.generate_answer)
        self.workflow.add_node("map_question_to_schema", self.map_question_to_schema)
        self.workflow.add_node("generate_function", self.generate_function)
        if self.supportai_enabled:
            self.workflow.add_node("supportai", self.supportai_search)
        self.workflow.add_node("rewrite_question", self.rewrite_question)
        self.workflow.add_node("apologize", self.apologize)

        if self.cypher_gen:
            self.workflow.add_node("generate_cypher", self.generate_cypher)
            self.workflow.add_conditional_edges(
                "generate_function",
                self.check_state_for_generation_error,
                {"error": "generate_cypher", "success": "generate_answer"},
            )
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
                    "apologize": "apologize",
                },
            )
        else:
            self.workflow.add_conditional_edges(
                "entry",
                self.route_question,
                {
                    "inquiryai_lookup": "map_question_to_schema",
                    "apologize": "apologize",
                },
            )

        self.workflow.add_edge("map_question_to_schema", "generate_function")
        if self.supportai_enabled:
            self.workflow.add_edge("supportai", "generate_answer")
        self.workflow.add_edge("rewrite_question", "entry")
        self.workflow.add_edge("apologize", END)

        app = self.workflow.compile()
        return app
