# Copyright (c) 2025 TigerGraph, Inc.
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

import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from fastapi.security.http import HTTPBase
from supportai import supportai
from supportai.concept_management.create_concepts import (
    CommunityConceptCreator,
    EntityConceptCreator,
    HigherLevelConceptCreator,
    RelationshipConceptCreator,
)
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
    embedding_service,
    embedding_store,
    get_llm_service,
    llm_config,
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
    if service_status["embedding_store"]["error"]:
        return HTTPException(
            status_code=503, detail=service_status["embedding_store"]["error"]
        )


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

    return supportai.create_ingest(graphname, cfg, conn)


@router.post("/{graphname}/graphrag/ingest")
@router.post("/{graphname}/supportai/ingest")
def ingest(
    graphname,
    loader_info: LoadingInfo,
    conn: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    conn = conn.state.conn
    if loader_info.file_path is None:
        raise Exception("File path not provided")
    if loader_info.load_job_id is None:
        raise Exception("Load job id not provided")
    if loader_info.data_source_id is None:
        raise Exception("Data source id not provided")

    try:
        res = conn.gsql(
            'USE GRAPH {}\nRUN LOADING JOB -noprint {} USING {}="{}"'.format(
                graphname,
                loader_info.load_job_id,
                "DocumentContent",
                "$" + loader_info.data_source_id + ":" + loader_info.file_path,
            )
        )
    except Exception as e:
        if (
            "Running the following loading job in background with '-noprint' option:"
            in str(e)
        ):
            res = str(e)
        else:
            raise e
    return {
        "job_name": loader_info.load_job_id,
        "job_id": res.split(
            "Running the following loading job in background with '-noprint' option:"
        )[1]
        .split("Jobid: ")[1]
        .split("\n")[0],
        "log_location": res.split(
            "Running the following loading job in background with '-noprint' option:"
        )[1]
        .split("Log directory: ")[1]
        .split("\n")[0],
    }


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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
        )
        res = retriever.search(query.question, query.method_params["top_k"])
    elif query.method.lower() == "community":
        retriever = CommunityRetriever(
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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
            embedding_service, embedding_store, get_llm_service(llm_config), conn
        )
        res = retriever.retrieve_answer(query.question, query.method_params["top_k"])

    elif query.method.lower() == "community":
        retriever = CommunityRetriever(
            embedding_service, embedding_store, get_llm_service(llm_config), conn
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


@router.get("/{graphname}/supportai/buildconcepts")
def build_concepts(
    graphname, conn: Request, credentials: Annotated[HTTPBase, Depends(security)]
):
    conn = conn.state.conn
    rels_concepts = RelationshipConceptCreator(conn, llm_config, embedding_service)
    rels_concepts.create_concepts()
    ents_concepts = EntityConceptCreator(conn, llm_config, embedding_service)
    ents_concepts.create_concepts()
    comm_concepts = CommunityConceptCreator(conn, llm_config, embedding_service)
    comm_concepts.create_concepts()
    high_level_concepts = HigherLevelConceptCreator(conn, llm_config, embedding_service)
    high_level_concepts.create_concepts()

    return {"status": "success"}


@router.get("/{graphname}/{method}/forceupdate")
def supportai_update(
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
        graphrag_config.get("ecc", "http://localhost:8001")
        + f"/{graphname}/{method}/consistency_status"
    )
    LogWriter.info(f"Sending ECC request to: {ecc}")
    bg_tasks.add_task(
        http_get, ecc, headers={"Authorization": conn.headers["authorization"]}
    )
    return {"status": "submitted"}
