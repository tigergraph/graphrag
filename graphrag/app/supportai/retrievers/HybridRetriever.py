import json
from supportai.retrievers import BaseRetriever
from common.metrics.tg_proxy import TigerGraphConnectionProxy

def rerank_enabled(cfg) -> bool:
    """The ``rerank`` setting; when unset, it follows ``keyword_search``."""
    value = cfg.get("rerank")
    if value is None:
        value = cfg.get("keyword_search", False)
    return bool(value)


def rerank_on(retriever, doc_only) -> bool:
    """Reranking is on for the graph and applies to this search."""
    from common.config import get_graphrag_config
    if doc_only:
        return False
    cfg = get_graphrag_config(retriever.conn.graphname if retriever.conn else None)
    return rerank_enabled(cfg)


class HybridRetriever(BaseRetriever):
    def __init__(
        self,
        embedding_service,
        embedding_store,
        llm_service,
        connection: TigerGraphConnectionProxy,
    ):
        super().__init__(embedding_service, embedding_store, llm_service, connection)

    def search(self, question, indices, top_k=1, similarity_threshold=0.90, num_hops=2, num_seen_min=1, expand = False, method = "similarity", chunk_only=False, doc_only=False, verbose=False, max_results: int = 0, keyword_matches: bool = True):
        if expand:
            questions = self._expand_question(question, top_k, verbose)
            verbose and self.logger.info(f"Expanded questions to use: {questions}")

            method = method.lower()
            if method == "keywords" or method == "both" or method == "all":
                keywords = self._question_to_keywords(questions, top_k, verbose)
                verbose and self.logger.info(f"Searching with keywords: {keywords}")

                self._check_query_install("Keyword_Search")
                res = self.conn.runInstalledQuery(
                    "Keyword_Search",
                    params = {
                        "keywords": keywords,
                        "mode": "Any",
                        "top_k": top_k,
                        "doc_only": doc_only,
                        "verbose": verbose,
                    },
                    usePost=True
                )            
                start_set = []
                if len(res) > 1 and "selected_set" in res[1]:
                    if len(res[1]["selected_set"]) > 0:
                        start_set += res[1]["selected_set"]
                self.logger.info(f"Got start_set from keywords {keywords}: {str(start_set)}")
                if not method == "keywords":
                    start_set += self._generate_start_set(questions, indices, top_k, similarity_threshold, verbose=verbose)
            else:
                start_set = self._generate_start_set(questions, indices, top_k, similarity_threshold, verbose=verbose)

            verbose and self.logger.info(f"Searching with start_set: {str(start_set)}")

            self._check_query_install("GraphRAG_Hybrid_Search")
            res = self.conn.runInstalledQuery(
                "GraphRAG_Hybrid_Search",
                params = {
                    "json_list_vts": str(start_set),
                    "num_hops": num_hops,
                    "num_seen_min": num_seen_min,
                    "chunk_only": chunk_only,
                    "doc_only": doc_only,
                    "verbose": verbose,
                },
                usePost=True
            )  
        else:
            # Resolve the result cap with a top_k*2 floor: an explicit override
            # or graphrag_config value is honored, but never return fewer than
            # top_k*2 chunks (all seeds plus a comparable amount of the most
            # query-relevant expansion). Unset -> top_k*2.
            from common.config import get_graphrag_config
            _floor = top_k * 2
            cfg = get_graphrag_config(self.conn.graphname if self.conn else None)
            max_results = max(max_results or cfg.get("max_results", 0), _floor)
            # With reranking, search a larger pool and keep the best
            # max_results after the ranking call.
            rerank = rerank_enabled(cfg) and not doc_only
            keep = max_results
            if rerank:
                top_k = min(top_k * self._RERANK_POOL, self._RERANK_MAX_SEEDS)
                max_results = min(max_results * self._RERANK_POOL, self._RERANK_MAX_CANDIDATES)
            query_vector = self._generate_embedding(question)
            self._check_query_install("GraphRAG_Hybrid_Vector_Search")
            res = self.conn.runInstalledQuery(
                "GraphRAG_Hybrid_Vector_Search",
                params = {
                    "v_types": indices,
                    "query_vector": query_vector,
                    "top_k": top_k,
                    #"similarity_threshold": similarity_threshold,
                    "num_hops": num_hops,
                    "num_seen_min": num_seen_min,
                    "max_results": max_results,
                    "chunk_only": chunk_only,
                    "doc_only": doc_only,
                    "verbose": verbose,
                },
                sizeLimit=1000000000,
                usePost=True
            )  
        if not expand and rerank_on(self, doc_only):
            # The ranking call judges keyword and vector candidates together,
            # so keyword matches join without the strength gate.
            if keyword_matches:
                self._add_keyword_matches(question, res, top_k, strong_only=False)
            self._apply_rerank(question, res, keep)
        elif keyword_matches and not expand and not doc_only:
            self._add_keyword_matches(question, res, top_k)
        if len(res) > 1 and "verbose" in res[1]:
            verbose_info = json.dumps(res[1]['verbose'])
            self.logger.info(f"Retrived HybridSearch query verbose info: {verbose_info}")
            if expand:
                res[1]["verbose"]["expanded_questions"] = questions
        self._log_retrieval("GraphRAG_Hybrid_Vector_Search", res)
        return res

    # Reranking pool: this many times the usual seeds and results, capped.
    _RERANK_POOL = 3
    _RERANK_MAX_SEEDS = 30
    _RERANK_MAX_CANDIDATES = 60

    def _apply_rerank(self, question, res, keep):
        if not res or not isinstance(res[0], dict):
            return
        retrieved = res[0].get("final_retrieval") or {}
        texts = {cid: "\n".join(v) if isinstance(v, list) else str(v) for cid, v in retrieved.items()}
        order = self._rerank(question, texts, keep)
        res[0]["final_retrieval"] = {cid: retrieved[cid] for cid in order}
        res[0]["reranked"] = True

    def _add_keyword_matches(self, question, res, top_k, strong_only=True):
        """Add the chunks that best match the question's words (BM25) and are
        not already in the result, when the match is strong. Their text goes
        into ``final_retrieval`` and their ids into ``keyword_matches``.

        Only a strong match is added: weaker matches share only common words
        with the question and push relevant passages out of the answer."""
        from common.config import get_graphrag_config
        from supportai.keyword_index import chunk_texts, keyword_match

        cfg = get_graphrag_config(self.conn.graphname if self.conn else None)
        if not cfg.get("keyword_search", False) or not res or not isinstance(res[0], dict):
            return
        retrieved = res[0].setdefault("final_retrieval", {})
        try:
            match = keyword_match(self.conn, question, cfg.get("keyword_top_k", top_k))
            if match is None or (strong_only and match.strength != "strong"):
                return
            matches = match.chunks
            new_ids = [cid for cid, _ in matches if cid not in retrieved]
            texts = chunk_texts(self.conn, new_ids)
        except Exception as e:
            self.logger.warning(f"Keyword search failed: {e}")
            return
        for cid in new_ids:
            if cid in texts:
                retrieved[cid] = [texts[cid]]
        res[0]["keyword_matches"] = [cid for cid, _ in matches]

    def retrieve_answer(self, question, index, top_k=1, similarity_threshold=0.90, num_hops=2, num_seen_min=1, expand: bool = False, method: str = "similarity", chunk_only: bool = False, doc_only: bool = False, combine: bool = False, verbose: bool = False, max_results: int = 0):
        retrieved = self.search(question, index, top_k, similarity_threshold, num_hops, num_seen_min, expand, method, chunk_only, doc_only, verbose, max_results)

        if combine:
            context = []
            for x in retrieved[0]["final_retrieval"]:
                context += retrieved[0]["final_retrieval"][x]
            context = ["\n".join(set(context))]
            resp = self._generate_response(question, context, verbose=verbose)
        else:
            context = ["\n".join(retrieved[0]["final_retrieval"][x]) for x in retrieved[0]["final_retrieval"]]
            scored = self._score_candidates(question, context, top_k=top_k)
            resp = self._generate_response(question, scored, verbose=verbose)
        
        if verbose and len(retrieved) > 1 and "verbose" in retrieved[1]:
            resp["verbose"] = retrieved[1]["verbose"]
            resp["verbose"]["final_retrieval"] = retrieved[0]["final_retrieval"]
      
        return resp
