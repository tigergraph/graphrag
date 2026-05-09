from common.embeddings.embedding_services import EmbeddingModel
from common.embeddings.base_embedding_store import EmbeddingStore
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.llm_services.base_llm import LLM_Model
from common.py_schemas import CandidateScore, CandidateGenerator, GraphRAGAnswerOutput, CommunityAnswer
from common.utils.token_calculator import get_token_calculator
from common.config import get_chat_config, get_embedding_store, get_graphrag_config

from langchain_core.output_parsers import StrOutputParser, PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate

import re
import logging
from concurrent.futures import ThreadPoolExecutor

class BaseRetriever:
    def __init__(
        self,
        embedding_service: EmbeddingModel,
        embedding_store: EmbeddingStore,
        llm_service: LLM_Model,
        connection: TigerGraphConnectionProxy = None,
    ):
        self.emb_service = embedding_service
        self.llm_service = llm_service
        self.conn = connection
        # Resolve the per-graph store; the ``embedding_store`` arg is
        # accepted for back-compat but ignored when a graphname is
        # available so concurrent chat across different graphs can't
        # race over a shared connection.
        if connection and getattr(connection, "graphname", None):
            self.embedding_store = get_embedding_store(connection.graphname)
        else:
            self.embedding_store = embedding_store
        self.logger = logging.getLogger(__name__)
        # Use llm_service's own config when available (chatbot path);
        # fall back to get_chat_config() (direct supportai API path).
        llm_cfg = getattr(llm_service, "config", None) or get_chat_config()
        self.token_calculator = get_token_calculator(token_limit=llm_cfg.get("token_limit"), model_name=llm_cfg.get("llm_model"))

    def _install_query(self, query_name):
        self.logger.info(f"Installing query {query_name}")
        with open(f"common/gsql/supportai/retrievers/{query_name}.gsql", "r") as f:
            query = f.read()
        res = self.conn.gsql(
            "USE GRAPH "
            + self.conn.graphname
            + "\n"
            + query
            + "\n INSTALL QUERY "
            + query_name
        )
        return res

    def _check_query_install(self, query_name):
        endpoints = self.conn.getEndpoints(
            dynamic=True
        )  # installed queries in database
        installed_queries = [q.split("/")[-1] for q in endpoints if f"/{self.conn.graphname}/" in q]

        if query_name not in installed_queries:
            return self._install_query(query_name)
        else:
            return True

    def _question_to_keywords(self, question, top_k, verbose):
        keyword_parser = PydanticOutputParser(pydantic_object=CandidateGenerator)
        keyword_prompt = PromptTemplate(
            template=self.llm_service.keyword_extraction_prompt,
            input_variables=["question"],
            partial_variables={"format_instructions": keyword_parser.get_format_instructions()}
        )

        if verbose:
            self.logger.info("Prompt to LLM:\n" + keyword_prompt.invoke({"question": question}).to_string())

        answer = self.llm_service.invoke_with_parser(
            keyword_prompt, keyword_parser,
            {"question": question}, caller_name="question_to_keywords",
        )

        if verbose:
            self.logger.info(f"Extracted keywords \"{answer}\" from question \"{question}\" by LLM")

        res = answer.candidates
        res.sort(key=lambda x: x.quality_score, reverse=True)
        return [x.candidate for x in res[:top_k]]

    def _expand_question(self, question, top_k, verbose):
        expansion_parser = PydanticOutputParser(pydantic_object=CandidateGenerator)
        prompt = PromptTemplate(
            template=self.llm_service.question_expansion_prompt,
            input_variables=["question"],
            partial_variables={"format_instructions": expansion_parser.get_format_instructions()}
        )

        answer = self.llm_service.invoke_with_parser(
            prompt, expansion_parser,
            {"question": question}, caller_name="expand_question",
        )

        if verbose:
            self.logger.info(f"Expanded question \"{question}\" from LLM: {answer}")

        res = answer.candidates
        res.sort(key=lambda x: x.quality_score, reverse=True)
        return [x.candidate for x in res[:top_k]]

    def _score_candidate(self, question, context):
        """Score a single context chunk against the question using the LLM."""
        scoring_parser = PydanticOutputParser(pydantic_object=CommunityAnswer)
        prompt = PromptTemplate(
            template=self.llm_service.graphrag_scoring_prompt,
            input_variables=["question", "context"],
            partial_variables={"format_instructions": scoring_parser.get_format_instructions()}
        )

        try:
            return self.llm_service.invoke_with_parser(
                prompt, scoring_parser,
                {"question": question, "context": context},
                caller_name="score_candidate",
            )
        except Exception:
            self.logger.warning("score_candidate: all parsing failed, returning score 0")
            return CommunityAnswer(answer=str(context).strip(), quality_score=0)

    def _score_candidates(self, question, contexts, top_k=None):
        """Score multiple context chunks in parallel and return top-k ranked candidates."""
        if not contexts:
            return []

        graphrag_cfg = get_graphrag_config(self.conn.graphname if self.conn else None)
        max_workers = graphrag_cfg.get("default_concurrency", 10)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._score_candidate, question, c) for c in contexts]
            results = [f.result() for f in futures]

        results.sort(key=lambda x: x.quality_score, reverse=True)
        if top_k is not None:
            results = results[:top_k]

        return [{"candidate_answer": x.answer, "score": x.quality_score} for x in results]

    def _generate_response(self, question, retrieved, query = "", verbose = False):
        # Truncate retrieved sources to fit within token limit
        if not self.token_calculator.is_unlimited_tokens():
            # Reserve tokens for question, query, and format instructions (approximately 1000 tokens)
            max_context_tokens = self.token_calculator.get_max_context_tokens() - 1000

            if len(retrieved) > max_context_tokens:
                retrieved_tokens = self.token_calculator.count_tokens(retrieved)
                if retrieved_tokens > max_context_tokens:
                    retrieved = self.token_calculator.truncate_to_token_limit(retrieved, max_context_tokens)
                    self.logger.info(f"Truncated retrieved text from {retrieved_tokens} to {max_context_tokens} tokens")

        response_parser = PydanticOutputParser(pydantic_object=GraphRAGAnswerOutput)
        prompt = ChatPromptTemplate.from_template(self.llm_service.chatbot_response_prompt)
        input_vars = {
            "question": question, "context": retrieved, "query": query,
            "format_instructions": response_parser.get_format_instructions(),
        }

        if verbose:
            self.logger.info("Prompt to LLM:\n" + prompt.invoke(input_vars).to_string())

        generated = self.llm_service.invoke_with_parser(
            prompt, response_parser,
            input_vars, caller_name="generate_response",
        )

        return {"response": generated.generated_answer, "retrieved": retrieved}

    def _generate_embedding(self, text, str_mode: bool = False) -> str:
        embedding = self.emb_service.embed_query(text)
        if str_mode:
            return (
                str(embedding)
                .strip("[")
                .strip("]")
                .replace(" ", "")
            )
        else:
            return embedding

    def _hyde_embedding(self, text, str_mode: bool = False) -> str:
        prompt = ChatPromptTemplate.from_template(self.llm_service.hyde_prompt)

        generated = self.llm_service.invoke_with_parser(
            prompt, StrOutputParser(),
            {"question": text}, caller_name="hyde_embedding",
        )

        return self._generate_embedding(generated, str_mode)

    """    
    def _get_entities_relationships(self, text: str, extractor: BaseExtractor):
        return extractor.extract(text)
    """

    def _generate_start_set(self, questions, indices, top_k, similarity_threshold: float = 0.90, filter_expr: str = None, withHyDE: bool = False, verbose: bool = False):
        if not isinstance(questions, list):
            questions = [questions]

        candidate_set = []
        for question in questions:
            if withHyDE:
                query_embedding = self._hyde_embedding(question)
            else:
                query_embedding = self._generate_embedding(question)
            if filter_expr and "\"%" in filter_expr:
                filter_expr = re.findall(r'"(%[^"]*)"', filter_expr)[0]
            res = self.embedding_store.retrieve_similar_with_score(
                query_embedding=query_embedding,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                vertex_types=indices,
                filter_expr=filter_expr,
            )
            verbose and self.logger.info(f"Retrived topk similar for query \"{question}\": {res}")
            candidate_set += res
        candidate_set.sort(key=lambda x: x[1], reverse=True)
        start_set = []
        for document, _ in candidate_set:
            start_set.append({"v": document.metadata["vertex_id"], "t": document.metadata["vertex_type"]})
        start_set = [dict(d) for d in {tuple(vt.items()) for vt in start_set}][:top_k]
        verbose and self.logger.info(f"Returning start_set: {str(start_set)}")
        return start_set

    def search(self, question):
        pass

    def retrieve_answer(self, question):
        pass
