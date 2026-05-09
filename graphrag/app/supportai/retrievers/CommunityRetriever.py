import json

from supportai.retrievers import BaseRetriever
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.llm_services import LLM_Model


class CommunityRetriever(BaseRetriever):
    def __init__(
        self,
        embedding_service,
        embedding_store,
        llm_service: LLM_Model,
        connection: TigerGraphConnectionProxy,
    ):
        super().__init__(embedding_service, embedding_store, llm_service, connection)

    def search(self, question, community_level: int, top_k: int = 5, similarity_threshold = 0.90, expand: bool = False, with_chunk: bool = True, with_doc: bool = False, verbose: bool = False):
        if expand:
            questions = self._expand_question(question, top_k, verbose=verbose)
            verbose and self.logger.info(f"Expanded questions to use: {questions}")

            filter_expr = f"vertex_id like \"%"
            for i in range(1, community_level+1):
                filter_expr += f"_{i}"
            filter_expr += "\""  
            start_set = self._generate_start_set(questions, ["Community"], top_k, similarity_threshold, filter_expr=filter_expr, verbose=verbose)
            verbose and self.logger.info(f"Searching with start_set: {str(start_set)}")

            self._check_query_install("GraphRAG_Community_Search")
            res = self.conn.runInstalledQuery(
                "GraphRAG_Community_Search",
                params = {
                    "json_list_vts": str(start_set),
                    "community_level": community_level,
                    "with_chunk": with_chunk,
                    "with_doc": with_doc,
                    "verbose": verbose,
                },
                sizeLimit=1000000000,
                usePost=True
            )

            # Include similarity search results
            if with_chunk or with_doc:
                start_set = self._generate_start_set(questions, ["DocumentChunk"], top_k, similarity_threshold)

                self._check_query_install("Content_Similarity_Search")
                resp = self.conn.runInstalledQuery(
                    "Content_Similarity_Search",
                    params = {
                        "json_list_vts": str(start_set),
                        "v_type": "DocumentChunk",
                        "verbose": verbose,
                    },
                    usePost=True
                )
                res[0]["final_retrieval"]["Similarity_Context"] = [resp[0]["final_retrieval"][x] for x in resp[0]["final_retrieval"]]
        else:
            query_vector = self._generate_embedding(question)

            self._check_query_install("GraphRAG_Community_Vector_Search")
            res = self.conn.runInstalledQuery(
                "GraphRAG_Community_Vector_Search",
                params = {
                    "query_vector": query_vector,
                    "community_level": community_level,
                    "top_k": top_k,
                    #"similarity_threshold": similarity_threshold,
                    "with_chunk": with_chunk,
                    "with_doc": with_doc,
                    "verbose": verbose,
                },
                usePost=True
            )
        if len(res) > 1 and "verbose" in res[1]:
            verbose_info = json.dumps(res[1]['verbose'])
            self.logger.info(f"Retrived GraphRAG query verbose info: {verbose_info}")
            if expand:
                res[1]["verbose"]["expanded_questions"] = questions
        return res
    
    def retrieve_answer(self,
                        question: str,
                        community_level: int,
                        top_k: int = 1,
                        similarity_threshold: float = 0.90,
                        expand: bool = False,
                        with_chunk: bool = False,
                        with_doc: bool = False,
                        combine: bool = False,
                        verbose: bool = False):
        retrieved = self.search(question, community_level, top_k, similarity_threshold, expand, with_chunk, with_doc, verbose)

        if combine:
            context = []
            for x in retrieved[0]["final_retrieval"]:
                context += retrieved[0]["final_retrieval"][x]
            context = ["\n".join(set(context))]
            resp = self._generate_response(question, context, verbose=verbose)
        else:
            context = ["\n".join(retrieved[0]["final_retrieval"][x]) for x in retrieved[0]["final_retrieval"]]
            new_context = self._score_candidates(question, context, top_k=top_k)
            resp = self._generate_response(question, new_context, verbose=verbose)

        if verbose and len(retrieved) > 1 and "verbose" in retrieved[1]:
            resp["verbose"] = retrieved[1]["verbose"]
            resp["verbose"]["final_retrieval"] = retrieved[0]["final_retrieval"]

        return resp
