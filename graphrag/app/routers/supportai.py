# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.security.http import HTTPBase
from supportai import supportai
from supportai.retrievers import (
    EntityRelationshipRetriever,
    HybridRetriever,
    SimilarityRetriever,
    SiblingRetriever,
    CommunityRetriever
)

from common.config import (
    db_config,
    graphrag_config,
    get_embedding_service,
    get_embedding_store,
    get_chat_config,
    get_llm_service,
    service_status,
)
from common.logs.logwriter import LogWriter
from common.py_schemas.schemas import (  # SupportAIInitConfig,; SupportAIMethod,
    GraphRAGResponse,
    CreateIngestConfig,
    LoadingInfo,
    SupportAIMethod,
    SupportAIQuestion,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["SupportAI"])

security = HTTPBase(scheme="basic", auto_error=False)


def check_embedding_store_status():
    """Return the embedding store if ready, else raise 503.

    Replaces the old behavior that returned (rather than raised) an
    HTTPException, leaving callers thinking the check succeeded.
    """
    try:
        return get_embedding_store(timeout=0)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{graphname}/graphrag/initialize")
@router.post("/{graphname}/supportai/initialize")
def initialize(
    graphname,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    conn = conn.state.conn

    resp = supportai.init_supportai(conn, graphname)
    schema_res, index_res, query_res = resp[0], resp[1], resp[2]
    return {
        "host_name": conn._tg_connection.host,  # include host_name for debugging from client. Their pyTG conn might not have the same host as what's configured in graphrag
        "schema_creation_status": json.dumps(schema_res),
        "index_creation_status": json.dumps(index_res),
        "query_creation_status": json.dumps(query_res),
    }


@router.post("/{graphname}/graphrag/create_ingest")
@router.post("/{graphname}/supportai/create_ingest")
def create_ingest(
    graphname,
    cfg: CreateIngestConfig,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    conn = conn.state.conn
    try:
        return supportai.create_ingest(graphname, cfg, conn)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_ingest failed for graph '{graphname}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingest preparation failed: {str(e)}")


@router.post("/{graphname}/graphrag/ingest")
@router.post("/{graphname}/supportai/ingest")
def ingest(
    graphname,
    loader_info: LoadingInfo,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    conn = conn.state.conn
    try:
        return supportai.ingest(graphname, loader_info, conn)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ingest failed for graph '{graphname}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


@router.post("/{graphname}/graphrag/search")
@router.post("/{graphname}/supportai/search")
def search(
    graphname,
    query: SupportAIQuestion,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    check_embedding_store_status()
    conn = conn.state.conn
    if "expand" not in query.method_params:
        query.method_params["expand"] = False
    if "verbose" not in query.method_params:
        query.method_params["verbose"] = False
    if query.method.lower() == "hybrid":
        retriever = HybridRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        if "method" not in query.method_params:
            query.method_params["method"] = "similarity"
        if "chunk_only" not in query.method_params:
            query.method_params["chunk_only"] = False
        if "doc_only" not in query.method_params:
            query.method_params["doc_only"] = False
        if "similarity_threshold" not in query.method_params:
            query.method_params["similarity_threshold"] = 0.90
        res = retriever.search(
            query.question,
            query.method_params["indices"],
            query.method_params["top_k"],
            query.method_params["similarity_threshold"],
            query.method_params["num_hops"],
            query.method_params["num_seen_min"],
            query.method_params["expand"],
            query.method_params["method"],
            query.method_params["chunk_only"],
            query.method_params["doc_only"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "similarity":
        if "index" not in query.method_params:
            raise Exception("Index name not provided")
        retriever = SimilarityRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.search(
            query.question,
            query.method_params["index"],
            query.method_params["top_k"],
            query.method_params["withHyDE"],
            query.method_params["expand"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "contextual":
        if "index" not in query.method_params:
            raise Exception("Index name not provided")
        retriever = SiblingRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.search(
            query.question,
            query.method_params["index"],
            query.method_params["top_k"],
            query.method_params["lookback"],
            query.method_params["lookahead"],
            query.method_params["withHyDE"],
            query.method_params["expand"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "entityrelationship":
        retriever = EntityRelationshipRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.search(query.question, query.method_params["top_k"])
    elif query.method.lower() == "community":
        retriever = CommunityRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        if "with_chunk" not in query.method_params:
            query.method_params["with_chunk"] = True
        if "with_doc" not in query.method_params:
            query.method_params["with_doc"] = False
        if "similarity_threshold" not in query.method_params:
            query.method_params["similarity_threshold"] = 0.90
        res = retriever.search(
            query.question,
            query.method_params["community_level"],
            query.method_params["top_k"],
            query.method_params["similarity_threshold"],
            query.method_params["expand"],
            query.method_params["with_chunk"],
            query.method_params["with_doc"],
            query.method_params["verbose"],
        )
    else:
        raise Exception(f"Method {query.method} not implemented")
    return res


@router.post("/{graphname}/graphrag/answerquestion")
@router.post("/{graphname}/supportai/answerquestion")
def answer_question(
    graphname,
    query: SupportAIQuestion,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    check_embedding_store_status()
    conn = conn.state.conn
    resp = GraphRAGResponse
    resp.response_type = "supportai"
    if "combine" not in query.method_params:
        query.method_params["combine"] = False
    if "expand" not in query.method_params:
        query.method_params["expand"] = False
    if "verbose" not in query.method_params:
        query.method_params["verbose"] = False
    if query.method.lower() == "hybrid":
        retriever = HybridRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        if "method" not in query.method_params:
            query.method_params["method"] = "Similarity"
        if "chunk_only" not in query.method_params:
            query.method_params["chunk_only"] = False
        if "doc_only" not in query.method_params:
            query.method_params["doc_only"] = False
        if "similarity_threshold" not in query.method_params:
            query.method_params["similarity_threshold"] = 0.90
        res = retriever.retrieve_answer(
            query.question,
            query.method_params["indices"],
            query.method_params["top_k"],
            query.method_params["similarity_threshold"],
            query.method_params["num_hops"],
            query.method_params["num_seen_min"],
            query.method_params["expand"],
            query.method_params["method"],
            query.method_params["chunk_only"],
            query.method_params["doc_only"],
            query.method_params["combine"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "similarity":
        if "index" not in query.method_params:
            raise Exception("Index name not provided")
        retriever = SimilarityRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.retrieve_answer(
            query.question,
            query.method_params["index"],
            query.method_params["top_k"],
            query.method_params["withHyDE"],
            query.method_params["expand"],
            query.method_params["combine"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "contextual":
        if "index" not in query.method_params:
            raise Exception("Index name not provided")
        retriever = SiblingRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.retrieve_answer(
            query.question,
            query.method_params["index"],
            query.method_params["top_k"],
            query.method_params["lookback"],
            query.method_params["lookahead"],
            query.method_params["withHyDE"],
            query.method_params["expand"],
            query.method_params["combine"],
            query.method_params["verbose"],
        )
    elif query.method.lower() == "entityrelationship":
        retriever = EntityRelationshipRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        res = retriever.retrieve_answer(query.question, query.method_params["top_k"])

    elif query.method.lower() == "community":
        retriever = CommunityRetriever(
            get_embedding_service(), get_embedding_store(), get_llm_service(get_chat_config(graphname)), conn
        )
        if "with_chunk" not in query.method_params:
            query.method_params["with_chunk"] = True
        if "with_doc" not in query.method_params:
            query.method_params["with_doc"] = False
        if "similarity_threshold" not in query.method_params:
            query.method_params["similarity_threshold"] = 0.90
        res = retriever.retrieve_answer(
            query.question,
            query.method_params["community_level"],
            query.method_params["top_k"],
            query.method_params["similarity_threshold"],
            query.method_params["expand"],
            query.method_params["with_chunk"],
            query.method_params["with_doc"],
            query.method_params["combine"],
            query.method_params["verbose"],
        )
    else:
        raise Exception("Method not implemented")

    resp.natural_language_response = res["response"]
    resp.query_sources = res["retrieved"]

    return res


@router.get("/{graphname}/{method}/forceupdate")
def graphrag_update(
    graphname: str,
    method: str,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
    bg_tasks: BackgroundTasks,
    response: Response,
):
    if method != SupportAIMethod.SUPPORTAI and method != SupportAIMethod.GRAPHRAG:
        response.status_code = status.HTTP_404_NOT_FOUND
        return f"{method} is not a valid method. {SupportAIMethod.SUPPORTAI} or {SupportAIMethod.GRAPHRAG}"

    from httpx import get as http_get

    ecc = (
        graphrag_config.get("ecc", "http://graphrag-ecc:8001")
        + f"/{graphname}/{method}/consistency_update"
    )
    LogWriter.info(f"Sending ECC request to: {ecc}")
    bg_tasks.add_task(
        http_get, ecc, headers={"Authorization": conn.headers["authorization"]}
    )
    return {"status": "submitted"}


@router.post("/{graphname}/graphrag/create_graph")
def create_graph(
    graphname: str,
    conn: Request,
):
    """
    Create a new TigerGraph knowledge graph.
    This creates an empty graph with the specified name.
    The middleware creates the TigerGraph connection and stores it in request.state.conn
    """
    try:
        # Get the connection from request state (created by auth_middleware in main.py)
        tg_conn = conn.state.conn

        # Create the graph using GSQL
        LogWriter.info(f"Creating graph: {graphname}")
        create_query = f"CREATE GRAPH {graphname}()"
        result = tg_conn.gsql(create_query)

        LogWriter.info(f"Graph creation result: {result}")
        return {
            "status": "success",
            "message": f"Graph '{graphname}' created successfully",
            "graphname": graphname,
            "details": result
        }

    except Exception as e:
        LogWriter.error(f"Error creating graph {graphname}: {str(e)}")
        if "conflicts" in str(e).lower() or "existing graph" in str(e).lower():
            return {
                "status": "error",
                "message": f"Graph '{graphname}' already exists",
                "details": str(e)
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to create graph '{graphname}': {str(e)}",
                "details": str(e)
            }
