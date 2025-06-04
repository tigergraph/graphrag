import logging
import traceback
import json
from time import sleep, time
from collections import defaultdict
from typing import Iterable, List, Optional, Tuple

import Levenshtein as lev
from asyncer import asyncify
from langchain_core.documents.base import Document

from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.embedding_services import EmbeddingModel
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics

from pyTigerGraph import TigerGraphConnection

logger = logging.getLogger(__name__)


class TigerGraphEmbeddingStore(EmbeddingStore):
    def __init__(
        self,
        conn: TigerGraphConnection,
        embedding_service: EmbeddingModel,
        support_ai_instance: bool = False
    ):
        self.embedding_service = embedding_service
        self.support_ai_instance = support_ai_instance
        self.conn = TigerGraphConnection(
                host=conn.host,
                username=conn.username,
                password=conn.password,
                graphname=conn.graphname,
                restppPort=conn.restppPort,
                gsPort=conn.gsPort,
                tgCloud=conn.tgCloud,
                useCert=conn.useCert,
                certPath=conn.certPath,
                sslPort = conn.sslPort,
                jwtToken = conn.jwtToken,
             )
        if conn.apiToken:
            self.conn.getToken()

        tg_version = self.conn.getVer()
        ver = tg_version.split(".")
        if int(ver[0]) >= 4 and int(ver[1]) >= 2:
            logger.info(f"Installing GDS library")
            q_res = self.conn.gsql(
                """USE GLOBAL\nimport package gds\ninstall function gds.**"""
            )
            logger.info(f"Done installing GDS library with status {q_res}")
            if not conn.graphname == "MyGraph":
                self.install_vector_queries()
            logger.info(f"TigerGraph embedding store is initialized with graph {self.conn.graphname}")
        else:
            raise Exception(f"Current TigerGraph version {ver} does not support vector feature!")

    def install_vector_queries(self):
        logger.info(f"Installing vector queries")
        vector_queries = [
            "vertices_have_embedding",
            "check_embedding_exists",
            "get_vertices_with_vector",
            "get_topk_similar",
            "get_topk_closest",
        ]

        installed_queries = [q.split("/")[-1] for q in self.conn.getEndpoints(dynamic=True) if f"/{self.conn.graphname}/" in q]
        need_install = False
        for q_name in vector_queries:
            if q_name not in installed_queries:
                with open(f"common/gsql/vector/{q_name}.gsql", "r") as f:
                    q_body = f.read()
                q_res = self.conn.gsql(
                    """USE GRAPH {}\nBEGIN\n{}\nEND\ninstall query {}\n""".format(
                        self.conn.graphname, q_body, q_name
                    )
                )
                need_install = True
                logger.info(f"Done creating vector query {q_name} with status {q_res}")
        #TBD
        if need_install and False:
            logger.info(f"Installing supportai queries all together")
            query_res = conn.gsql(
                """USE GRAPH {}\nINSTALL QUERY ALL\n""".format(
                    conn.graphname
                )
            )
            logger.info(f"Done installing supportai query all with status {query_res}")
        else:
            logger.info(f"Query installation is not needed for supportai")

    def set_graphname(self, graphname):
        self.conn.graphname = graphname
        self.install_vector_queries()

    def set_connection(self, conn):
        self.conn = TigerGraphConnection(
                host=conn.host,
                username=conn.username,
                password=conn.password,
                graphname=conn.graphname,
                restppPort=conn.restppPort,
                gsPort=conn.gsPort,
                tgCloud=conn.tgCloud,
                useCert=conn.useCert,
                certPath=conn.certPath,
                sslPort = conn.sslPort,
                jwtToken = conn.jwtToken,
             )
        if conn.apiToken:
            self.conn.getToken()

        self.install_vector_queries()

    def map_attrs(self, attributes: Iterable[Tuple[str, List[float]]]):
        # map attrs
        attrs = {}
        for (k, v) in attributes:
            attrs[k] = {"value": v}
        return attrs

    def add_embeddings(
        self,
        embeddings: Iterable[Tuple[Tuple[str, str], List[float]]],
        metadatas: List[dict] = None,
    ):
        """Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
        """
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} TigerGraph ENTRY add_embeddings()"
            )

            start_time = time()
            batch = {
                "vertices": defaultdict(dict[str, any]),
            }

            for i, (text, _) in enumerate(embeddings):
                (v_id, v_type) = metadatas[i].get("vertex_id")
                try:
                    embedding = self.embedding_service.embed_query(text)
                except Exception as e:
                    LogWriter.error(f"Failed to embed {v_id}: {e}")
                    return
                attr = self.map_attrs([("embedding", embedding)])
                batch["vertices"][v_type][v_id] = attr
            data = json.dumps(batch)
            added = self.conn.upsertData(data)

            duration = time() - start_time

            LogWriter.info(f"request_id={req_id_cv.get()} TigerGraph EXIT add_embeddings()")

            # Check if registration was successful
            if added:
                success_message = f"Document registered with id: {added[0]}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while registering document: {str(e)}"
            LogWriter.error(error_message)

    async def aadd_embeddings(
        self,
        embeddings: Iterable[Tuple[str, List[float]]],
        metadatas: List[dict] = None,
    ):
        """Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
        """
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} TigerGraph ENTRY aadd_embeddings()"
            )

            start_time = time()
            batch = {
                "vertices": defaultdict(dict[str, any]),
            }

            for i, (text, _) in enumerate(embeddings):
                (v_id, v_type) = metadatas[i].get("vertex_id")
                try:
                    embedding = await self.embedding_service.aembed_query(text)
                except Exception as e:
                    LogWriter.error(f"Failed to embed {v_id}: {e}")
                    return
                attr = self.map_attrs([("embedding", embedding)])
                batch["vertices"][v_type][v_id] = attr
            data = json.dumps(batch)
            added = self.conn.upsertData(data)

            duration = time() - start_time

            LogWriter.info(f"request_id={req_id_cv.get()} TigerGraph EXIT aadd_embeddings()")

            # Check if registration was successful
            if added:
                success_message = f"Document {metadatas} registered with status: {added}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {metadatas} with status {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            error_message = f"An error occurred while registering document: {str(e)}"
            LogWriter.error(error_message)

    def has_embeddings(
        self,
        v_ids: Iterable[Tuple[str, str]]
    ):
        ret = True
        try:
            for (v_id, v_type) in v_ids:
                res = self.conn.runInstalledQuery(
                    "check_embedding_exists",
                    params={
                        "vertex_type": v_type,
                        "vertex_id": v_id,
                    }
                )
                logger.info(f"Return result {res} for has_embeddings({v_ids})")
                found = False
                if "results" in res[0]:
                    for v in res[0]["results"]:
                        if v["v_id"] == v_id:
                            found = True
                            break
                ret = ret and found 
        except Exception as e:
            logger.info(f"Exception {str(e)} when running has_embeddings({v_type}, {v_id}), return False")
            ret = False
        return ret

    def check_embedding_rebuilt(
        self,
        v_type: str
    ):
        try:
            res = self.conn.runInstalledQuery(
                "vertices_have_embedding",
                params={
                    "vertex_type": v_type,
                }
            )
            logger.info(f"Return result {res} for all_has_embeddings({v_type})")
            return res[0]["all_have_embedding"]
        except Exception as e:
            logger.info(f"Exception {str(e)} when running has_embeddings({v_type}, {v_id}), return False")
        return False

    def remove_embeddings(
        self, ids: Optional[List[str]] = None, expr: Optional[str] = None
    ):
        #TBD
        return

    def retrieve_similar(self, query_embedding, top_k=10, filter_expr: str = None, vertex_types: List[str] = ["DocumentChunk"]):
        res = retrieve_similar_with_score(query_embedding, top_k=top_k, filter_expr=filter_expr, vertex_types=vertex_types)
        similar = [x[0] for x in res]
        return similar

    def retrieve_similar_with_score(self, query_embedding, top_k=10, similarity_threshold=0.90, filter_expr: str = None, vertex_types: List[str] = ["DocumentChunk"]):
        """Retireve Similar.
        Retrieve similar embeddings from the vector store given a query embedding.
        Args:
            query_embedding (List[float]):
                The embedding to search with.
            top_k (int, optional):
                The number of documents to return. Defaults to 10.
        Returns:
            https://api.python.langchain.com/en/latest/documents/langchain_core.documents.base.Document.html#langchain_core.documents.base.Document
            Document results for search.
        """
        try:
            logger.info(f"Fetch {top_k} similar entries from {vertex_types} with filter {filter_expr}")

            start_time = time()
            verts = self.conn.runInstalledQuery(
                "get_topk_similar",
                params={
                    "vertex_types": vertex_types,
                    "query_vector": query_embedding,
                    "top_k": top_k*2,
                    "expr": filter_expr,
                }
            )
            end_time = time()
            logger.info(f"Got {top_k} similar entries: {verts}")
            similar = []
            for r in verts:
                if "results" in r:
                    for v in r["results"]:
                        document = Document(
                            page_content="",
                            metadata={"vertex_id": v["v_id"], "vertex_type": v["v_type"]}
                        )
                        similar.append((document, v["score"]))

            similar.sort(key=lambda x: x[1], reverse=True)
            i = 0
            for i in range(len(similar)):
                if similar[i][1] < similarity_threshold:
                    break
            if i <= top_k:
                return similar[:top_k]
            else:
                return similar[:i]
        except Exception as e:
            error_message = f"An error occurred while retrieving docuements: {str(e)}"
            LogWriter.error(error_message)
            raise e

    def add_connection_parameters(self, query_params: dict) -> dict:
        """Add Connection Parameters.
        Add connection parameters to the query parameters.
        Args:
            query_params (dict):
                Dictionary containing the parameters for the GSQL query.
        Returns:
            A dictionary containing the connection parameters.
        """
        # Nothing needed for TG
        return query_params

    async def aget_k_closest(
        self, vertex: Tuple[str, str], k=10, threshold_similarity=0.90, edit_dist_threshold_pct=0.75
    ) -> list[Document]:
        logger.info(f"Fetch {k} closest entries for {vertex} with threshold {threshold_similarity}")
        # Get all vectors with this ID
        (v_id, v_type) = vertex
        verts = self.conn.runInstalledQuery(
            "get_topk_closest",
            params={
                "vertex_type": v_type,
                "vertex_id": v_id,
                "k": k,
                "threshold": threshold_similarity,
            }
        )
        result = []
        for r in verts:
            if "results" in r:
                for v in r["results"]:
                    result.append(v["v_id"])
        logger.info(f"Returning {result}")
        return set(result)


    def query(self, expr: str, output_fields: List[str]):
        """Get output fields with expression

        Args:
            expr: Expression - E.g: "pk > 0"

        Returns:
            List of output fields' contents
        """

        return []

    def __del__(self):
        logger.info("TigerGraphEmbeddingStore destructed.")
