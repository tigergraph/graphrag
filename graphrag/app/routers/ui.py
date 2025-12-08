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

import asyncio
import base64
import json
import logging
import os
import re
import time
import traceback
import uuid
from typing import Annotated

import asyncer
import httpx
import requests
from agent.agent import TigerGraphAgent, make_agent
from agent.Q import DONE
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.http import HTTPBase
from pyTigerGraph import TigerGraphConnection
from tools.validation_utils import MapQuestionToSchemaException

from common.config import db_config, graphrag_config, embedding_service, llm_config, service_status
from common.db.connections import get_db_connection_pwd_manual
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics as pmetrics
from supportai import supportai
from common.py_schemas.schemas import (
    AgentProgess,
    CreateIngestConfig,
    GraphRAGResponse,
    LoadingInfo,
    Message,
    ResponseType,
    Role,
)
from common.utils.text_extractors import TextExtractor
logger = logging.getLogger(__name__)

use_cypher = os.getenv("USE_CYPHER", "false").lower() == "true"
route_prefix = "/ui"  # APIRouter's prefix doesn't work with the websocket, so it has to be done here
router = APIRouter(tags=["UI"])
security = HTTPBasic()
GRAPH_NAME_RE = re.compile(r"- Graph (.*)\(")


def auth(usr: str, password: str, conn=None) -> tuple[list[str], TigerGraphConnection]:
    if conn is None:
        conn = TigerGraphConnection(
            host=db_config["hostname"], graphname="", username=usr, password=password
        )

    try:
        # parse user info
        info = conn.gsql("LS USER")
        graphs = []
        for m in GRAPH_NAME_RE.finditer(info):
            groups = m.groups()
            graphs.extend(groups)

    except requests.exceptions.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    except Exception as e:
        raise e
    return graphs, conn


def ws_basic_auth(auth_info: str, graphname=None):
    auth_info = base64.b64decode(auth_info.encode()).decode()
    auth_info = auth_info.split(":")
    username = auth_info[0]
    password = auth_info[1]
    conn = get_db_connection_pwd_manual(graphname, username, password)
    return auth(username, password, conn)


def ui_basic_auth(
    creds: Annotated[HTTPBasicCredentials, Depends(security)],
) -> list[str]:
    """
    1) Try authenticating with DB.
    2) Get list of graphs user has access to
    """
    graphs = auth(creds.username, creds.password)[0]
    return graphs, creds


@router.post(f"{route_prefix}/ui-login")
def login(auth: Annotated[list[str], Depends(ui_basic_auth)]):
    graphs = auth[0]
    return {"graphs": graphs}


@router.post(f"{route_prefix}/feedback")
def add_feedback(
    message: Message,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    try:
        res = httpx.post(
            f"{graphrag_config['chat_history_api']}/conversation",
            json=message.model_dump(),
            headers={"Authorization": f"Basic {auth}"},
        )
        res.raise_for_status()
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/ui/feedback request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise e

    return {"message": "feedback saved", "message_id": message.message_id}


@router.post(route_prefix + "/{graphname}/create_graph")
def create_graph(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Create a new TigerGraph knowledge graph.
    This creates an empty graph with the specified name.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Create the graph using GSQL
        LogWriter.info(f"Creating graph: {graphname}")
        create_query = f"CREATE GRAPH {graphname}()"
        result = conn.gsql(create_query)

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


@router.post(route_prefix + "/{graphname}/initialize_graph")
def init_graph(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Initialize a TigerGraph knowledge graph with GraphRAG schema.
    This initializes the graph with SupportAI/GraphRAG schema, indexes, and queries.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Initialize the graph with GraphRAG schema
        LogWriter.info(f"Initializing graph: {graphname}")
        resp = supportai.init_supportai(conn, graphname)
        schema_res, index_res, query_res = resp[0], resp[1], resp[2]

        LogWriter.info(f"Graph initialization completed for: {graphname}")

        return {
            "status": "success",
            "message": f"Graph '{graphname}' initialized successfully",
            "graphname": graphname,
            "host_name": conn._tg_connection.host,
            "schema_creation_status": json.dumps(schema_res),
            "index_creation_status": json.dumps(index_res),
            "query_creation_status": json.dumps(query_res),
        }

    except Exception as e:
        LogWriter.error(f"Error initializing graph {graphname}: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to initialize graph '{graphname}': {str(e)}",
            "details": str(e)
        }


@router.post(route_prefix + "/{graphname}/rebuild_graph")
def forceupdate(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    bg_tasks: BackgroundTasks,
):
    """
    Force update/refresh of a GraphRAG knowledge graph.
    This triggers the ECC (Eventual Consistency Checker) service to rebuild the graph.
    Uses HTTP Basic Authentication to get credentials.
    """
    # Extract credentials from the dependency
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()

    from httpx import get as http_get

    ecc = (
        graphrag_config.get("ecc", "http://localhost:8001")
        + f"/{graphname}/graphrag/consistency_update"
    )
    LogWriter.info(f"Sending ECC request to: {ecc}")
    bg_tasks.add_task(
        http_get, ecc, headers={"Authorization": f"Basic {auth}"}
    )
    return {"status": "submitted"}


@router.get(route_prefix + "/{graphname}/rebuild_status")
def get_rebuild_status(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Check if a GraphRAG rebuild is currently in progress for the specified graph.
    Returns the current status without triggering a new rebuild.
    Uses HTTP Basic Authentication to get credentials.
    """
    # Extract credentials from the dependency
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()

    try:
        ecc_status_url = (
            graphrag_config.get("ecc", "http://localhost:8001")
            + f"/{graphname}/graphrag/rebuild_status"
        )
        LogWriter.info(f"Checking ECC status at: {ecc_status_url}")
        
        response = httpx.get(
            ecc_status_url,
            headers={"Authorization": f"Basic {auth}"},
            timeout=10.0
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            LogWriter.warning(f"ECC status check returned {response.status_code}")
            return {
                "graphname": graphname,
                "is_running": False,
                "status": "unknown",
                "error": f"ECC service returned status {response.status_code}"
            }
    except Exception as e:
        LogWriter.error(f"Failed to check ECC status: {str(e)}")
        return {
            "graphname": graphname,
            "is_running": False,
            "status": "error",
            "error": str(e)
        }


@router.post(route_prefix + "/{graphname}/create_ingest")
def create_ingest(
    graphname: str,
    cfg: CreateIngestConfig,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Create an ingest configuration for a GraphRAG knowledge graph.
    This sets up the data source and load job configuration for document ingestion.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Create the ingest configuration
        LogWriter.info(f"Creating ingest configuration for graph: {graphname}")
        result = supportai.create_ingest(graphname, cfg, conn)

        return result

    except Exception as e:
        LogWriter.error(f"Error creating ingest configuration for graph {graphname}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create ingest configuration: {str(e)}"
        )


@router.post(route_prefix + "/{graphname}/ingest")
def ingest(
    graphname: str,
    loader_info: LoadingInfo,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Run document ingestion for a GraphRAG knowledge graph.
    This processes documents from the configured data source and loads them into the graph.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Run the ingestion
        LogWriter.info(f"Running ingestion for graph: {graphname}")
        result = supportai.ingest(graphname, loader_info, conn)

        return result

    except Exception as e:
        LogWriter.error(f"Error running ingestion for graph {graphname}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run ingestion: {str(e)}"
        )


@router.get(route_prefix + "/image_vertex/{graphname}/{image_id}")
async def serve_image_from_vertex(
    graphname: str,
    image_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Serve an image directly from the TigerGraph Image vertex.
    
    This endpoint uses standard HTTP Basic Authentication (same pattern as other endpoints).
    The endpoint fetches the base64 encoded image data from the Image vertex
    and returns it as an image response with the appropriate content type.
    
    Example URL: /ui/image_vertex/{graphname}/{image_id}
    """
    from fastapi.responses import Response
    
    try:
        # Extract credentials from the dependency (same pattern as graph_query and other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)
        
        LogWriter.info(f"Serving image {image_id} from graph {graphname}")

        # Fetch the Image vertex by ID
        image_vertices = conn.getVerticesById('Image', [image_id.lower()])
        
        if not image_vertices:
            raise HTTPException(status_code=404, detail=f"Image not found: {image_id}")
        
        image_vertex = image_vertices[0]
        image_data_b64 = image_vertex['attributes'].get('image_data', '')
        image_format = image_vertex['attributes'].get('image_format', 'jpg')
        
        if not image_data_b64:
            raise HTTPException(status_code=404, detail=f"No image data for: {image_id}")
        
        # Decode base64 to bytes
        image_bytes = base64.b64decode(image_data_b64)
        
        # Determine content type
        content_type_map = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp'
        }
        content_type = content_type_map.get(image_format.lower(), 'image/jpeg')
        
        # Return image as Response
        return Response(content=image_bytes, media_type=content_type)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving image {image_id} from graph {graphname}: {e}")
        raise HTTPException(status_code=500, detail=f"Error serving image: {str(e)}")


@router.get(route_prefix + "/user/{user_id}")
async def get_user_conversations(
    user_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/user/{user_id}",
                headers={"Authorization": f"Basic {auth}"},
            )
            res.raise_for_status()
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/ui/user/{user_id} request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise e

    return res.json()


@router.get(route_prefix + "/conversation/{conversation_id}")
async def get_conversation_contents(
    conversation_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/conversation/{conversation_id}",
                headers={"Authorization": f"Basic {auth}"},
            )
            res.raise_for_status()
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/conversation/{conversation_id} request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise e

    return res.json()

@router.get(route_prefix + "/get_feedback")
async def get_conversation_feedback(
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/get_feedback",
                headers={"Authorization": f"Basic {auth}"},
            )
            res.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error occurred: {e}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to fetch feedback")
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/get_feedback request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise HTTPException(status_code=500, detail="Internal server error")

    return res.json()


@router.delete(route_prefix + "/conversation/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Delete a conversation and all its messages."""
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            res = await client.delete(
                f"{graphrag_config['chat_history_api']}/conversation/{conversation_id}",
                headers={"Authorization": f"Basic {auth}"},
            )
            res.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error occurred: {e}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to delete conversation")
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/conversation/{conversation_id} DELETE request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"message": "Conversation deleted successfully"}


async def emit_progress(agent: TigerGraphAgent, ws: WebSocket):
    # loop on q until done token emit events through ws
    msg = None
    pop = asyncer.asyncify(agent.q.pop)

    while msg != DONE:
        msg = await pop()
        if msg is not None and msg != DONE:
            message = AgentProgess(
                content=msg,
                response_type=ResponseType.PROGRESS,
            )
            if ws:
                await ws.send_text(message.model_dump_json())
            else:
                return message.model_dump_json()


async def run_agent(
    agent: TigerGraphAgent,
    data: str,
    conversation_history: list[dict[str, str]],
    graphname,
    ws: WebSocket,
) -> GraphRAGResponse:
    resp = GraphRAGResponse(
        natural_language_response="", answered_question=False, response_type="inquiryai"
    )
    a_question_for_agent = asyncer.asyncify(agent.question_for_agent)
    try:
        # start agent and sample from Q to emit progress

        async with asyncio.TaskGroup() as tg:
            # run agent
            a_resp = tg.create_task(
                # TODO: make num mesages in history configureable
                a_question_for_agent(data, conversation_history[-4:])
            )
            # sample Q and emit events
            if ws:
                tg.create_task(emit_progress(agent, ws))
            else:
                emit_progress(agent, ws)
        pmetrics.llm_success_response_total.labels(embedding_service.model_name).inc()
        resp = a_resp.result()
        if ws:
            agent.q.clear()

    except MapQuestionToSchemaException:
        resp.natural_language_response = (
            "A schema mapping error occurred. Please try rephrasing your question."
        )
        resp.query_sources = {}
        resp.answered_question = False
        LogWriter.warning(
            f"/{graphname}/ui/chat request_id={req_id_cv.get()} agent execution failed due to MapQuestionToSchemaException"
        )
        pmetrics.llm_query_error_total.labels(embedding_service.model_name).inc()
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/{graphname}/ui/chat request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
    except Exception as e:
        resp.natural_language_response = "GraphRAG had an issue answering your question. Please try again, or rephrase your prompt."

        resp.query_sources = {}
        resp.answered_question = False
        LogWriter.warning(
            f"/{graphname}/ui/chat request_id={req_id_cv.get()} agent execution failed due to unknown exception {e}"
        )
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/{graphname}/ui/chat request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        pmetrics.llm_query_error_total.labels(embedding_service.model_name).inc()

    return resp


async def load_conversation_history(conversation_id: str, usr_auth: str) -> list[dict[str, str]]:
    """
    Load conversation history from the chat history service.
    Returns a list of dicts with 'query', 'response', 'create_ts', and 'update_ts' keys.
    """
    if not conversation_id or conversation_id == "new":
        return []
    
    ch = graphrag_config.get("chat_history_api")
    if ch is None:
        LogWriter.info("chat-history not enabled, returning empty history")
        return []
    
    headers = {"Authorization": f"Basic {usr_auth}"}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{ch}/conversation/{conversation_id}",
                headers=headers,
            )
            res.raise_for_status()
            conversation_data = res.json()
            # Convert conversation messages to the format expected by the agent
            history = []
            for msg in conversation_data:
                if msg.get("role") == "user":
                    # Find the corresponding system response
                    for response_msg in conversation_data:
                        if (response_msg.get("role") == "system" and 
                            response_msg.get("parent_id") == msg.get("message_id")):
                            history.append({
                                "query": msg.get("content", ""),
                                "response": response_msg.get("content", ""),
                                "create_ts": response_msg.get("create_ts"),
                                "update_ts": response_msg.get("update_ts"),
                            })
                            break
            
            LogWriter.info(f"Loaded {len(history)} conversation history entries for conversation {conversation_id}")
            return history
            
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(f"Error loading conversation history for {conversation_id}\nException Trace:\n{exc}")
        LogWriter.warning(f"Failed to load conversation history for {conversation_id}: {e}")
        return []


async def write_message_to_history(message: Message, usr_auth: str):
    ch = graphrag_config.get("chat_history_api")
    if ch is not None:
        headers = {"Authorization": f"Basic {usr_auth}"}
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    f"{ch}/conversation", headers=headers, json=message.model_dump()
                )
                res.raise_for_status()
        except Exception:  # catch all exceptions to log them, but don't raise
            exc = traceback.format_exc()
            logger.debug_pii(f"Error writing chat history\nException Trace:\n{exc}")

    else:
        LogWriter.info(f"chat-history not enabled. chat-history url: {ch}")

@router.get(route_prefix + "/{graphname}/query")
async def graph_query(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    q: str | None = None,
    rag_pattern: str | None = None,
    conversation_id: str | None = None,
):
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
    _, conn = ws_basic_auth(auth, graphname)
    try:
        # Load conversation history if conversation_id is provided
        conversation_history = await load_conversation_history(conversation_id, auth) if conversation_id else []

        # Use provided conversation ID or generate new one
        if not conversation_id or conversation_id == "new":
            convo_id = str(uuid.uuid4())
            LogWriter.info(f"Starting new conversation with ID: {convo_id}")
        else:
            convo_id = conversation_id
            LogWriter.info(f"Continuing conversation with ID: {convo_id}")

        # create agent
        # get retrieval pattern to use
        rag_pattern = rag_pattern or "hybridsearch"
        agent = make_agent(graphname, conn, use_cypher, supportai_retriever=rag_pattern)

        prev_id = None
        data = q

        # make message from data
        message = Message(
            conversation_id=convo_id,
            message_id=str(uuid.uuid4()),
            parent_id=prev_id,
            model=llm_config["model_name"],
            content=data,
            role=Role.USER,
        )
        # save message
        await write_message_to_history(message, auth)
        prev_id = message.message_id

        # generate response and keep track of response time
        start = time.monotonic()
        resp = await run_agent(
            agent, data, conversation_history, graphname, None
        )
        elapsed = time.monotonic() - start

        # save message
        message = Message(
            conversation_id=convo_id,
            message_id=str(uuid.uuid4()),
            parent_id=prev_id,
            model=llm_config["model_name"],
            content=resp.natural_language_response,
            role=Role.SYSTEM,
            response_time=elapsed,
            answered_question=resp.answered_question,
            response_type=resp.response_type,
            query_sources=resp.query_sources,
        )
        await write_message_to_history(message, auth)
        prev_id = message.message_id

        # reply
        return message.model_dump_json()
    except Exception as e:
        exc = traceback.format_exc()
        logger.debug_pii(
            f"/ui/{graphname}/query request_id={req_id_cv.get()} Exception Trace:\n{exc}"
        )
        raise e

@router.websocket(route_prefix + "/{graphname}/chat")
async def chat(
    graphname: str,
    websocket: WebSocket,
    rag_pattern: str | None = None,
):
    """
    WebSocket endpoint for chat functionality with conversation history support.
    
    Expected message flow:
    1. Authentication (base64 encoded username:password)
    2. RAG pattern (e.g., "hybridsearch", "similaritysearch", etc.)
    3. Conversation ID (or "new" for new conversation)
    4. User messages
    """
    if service_status["embedding_store"]["error"]:
        return HTTPException(
            status_code=503,
            detail=service_status["embedding_store"]["error"]
        )
    
    await websocket.accept()

    # AUTH with proper error handling and timeout
    try:
        logger.info(f"WebSocket connected, waiting for authentication for graph: {graphname}")
        usr_auth = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        logger.info(f"Received authentication data, length: {len(usr_auth)}")
        _, conn = ws_basic_auth(usr_auth, graphname)
        logger.info("Authentication successful")
    except asyncio.TimeoutError:
        logger.error("WebSocket authentication timeout - no credentials received")
        await websocket.close(code=1008, reason="Authentication timeout")
        return
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        await websocket.close(code=1008, reason=f"Authentication failed")
        return

    # Get RAG pattern
    rag_pattern = rag_pattern or "hybridsearch"
    
    # Get conversation ID
    conversation_id = await websocket.receive_text()
    
    # Load conversation history if not a new conversation
    conversation_history = await load_conversation_history(conversation_id, usr_auth)
    
    # Use provided conversation ID or generate new one
    if conversation_id == "new" or not conversation_id:
        convo_id = str(uuid.uuid4())
        LogWriter.info(f"Starting new conversation with ID: {convo_id}")
    else:
        convo_id = conversation_id
        LogWriter.info(f"Continuing conversation with ID: {convo_id}")

    # Send conversation ID to frontend
    await websocket.send_text(json.dumps({"conversation_id": convo_id}))

    # create agent
    agent = make_agent(graphname, conn, use_cypher, ws=websocket, supportai_retriever=rag_pattern)

    prev_id = None
    try:
        while True:
            data = await websocket.receive_text()

            # make message from data
            message = Message(
                conversation_id=convo_id,
                message_id=str(uuid.uuid4()),
                parent_id=prev_id,
                model=llm_config["model_name"],
                content=data,
                role=Role.USER,
            )
            # save message
            await write_message_to_history(message, usr_auth)
            prev_id = message.message_id

            # generate response and keep track of response time
            start = time.monotonic()
            resp = await run_agent(
                agent, data, conversation_history, graphname, websocket
            )
            elapsed = time.monotonic() - start

            # save message
            message = Message(
                conversation_id=convo_id,
                message_id=str(uuid.uuid4()),
                parent_id=prev_id,
                model=llm_config["model_name"],
                content=resp.natural_language_response,
                role=Role.SYSTEM,
                response_time=elapsed,
                answered_question=resp.answered_question,
                response_type=resp.response_type,
                query_sources=resp.query_sources,
            )
            await write_message_to_history(message, usr_auth)
            prev_id = message.message_id

            # reply
            await websocket.send_text(message.model_dump_json())

            # append message to history
            conversation_history.append(
                {"query": data, "response": resp.natural_language_response}
            )
    except WebSocketDisconnect as e:
        logger.info(f"Websocket disconnected: {str(e)}")
    except:
        await websocket.close()


# =====================================================
# File Upload Functionality for Server +Multi
# =====================================================

@router.get(route_prefix + "/{graphname}/uploads/list")
async def list_uploaded_files(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    List all files currently uploaded for a specific graphname.
    Returns file names, sizes, and upload dates.
    """
    try:
        upload_dir = os.path.join("uploads", graphname)
        
        if not os.path.exists(upload_dir):
            return {"graphname": graphname, "files": [], "total_files": 0, "total_size": 0}
        
        files_info = []
        total_size = 0
        
        for filename in os.listdir(upload_dir):
            file_path = os.path.join(upload_dir, filename)
            if os.path.isfile(file_path):
                file_stat = os.stat(file_path)
                files_info.append({
                    "filename": filename,
                    "size": file_stat.st_size,
                    "modified": file_stat.st_mtime,
                })
                total_size += file_stat.st_size
        
        return {
            "graphname": graphname,
            "files": files_info,
            "total_files": len(files_info),
            "total_size": total_size,
        }
    
    except Exception as e:
        logger.error(f"Error listing files for graph {graphname}: {e}")
        raise HTTPException(status_code=500, detail=f"Error listing files: {str(e)}")


@router.post(route_prefix + "/{graphname}/uploads")
async def upload_files(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    files: list[UploadFile] = File(...),
    overwrite: bool = False,
):
    """
    Upload one or multiple files for a specific graphname.
    Files are stored in uploads/{graphname}/ directory.
    
    Parameters:
    - graphname: The graph name to associate files with
    - files: List of files to upload
    - overwrite: If False (default), will reject if files already exist
    """
    try:
        upload_dir = os.path.join("uploads", graphname)
        os.makedirs(upload_dir, exist_ok=True)
        
        # Check for existing files if overwrite is False
        if not overwrite:
            existing_files = []
            for file in files:
                file_path = os.path.join(upload_dir, file.filename)
                if os.path.exists(file_path):
                    existing_files.append(file.filename)
            
            if existing_files:
                return {
                    "status": "conflict",
                    "message": "Some files already exist. Set overwrite=true to replace them.",
                    "existing_files": existing_files,
                }
        
        # Save uploaded files
        uploaded_files = []
        total_size = 0
        
        for file in files:
            file_path = os.path.join(upload_dir, file.filename)
            
            # Write file to disk
            with open(file_path, "wb") as f:
                content = await file.read()
                f.write(content)
                file_size = len(content)
                total_size += file_size
            
            uploaded_files.append({
                "filename": file.filename,
                "size": file_size,
                "path": file_path,
            })
            
            logger.info(f"Uploaded file {file.filename} ({file_size} bytes) for graph {graphname}")
        
        return {
            "status": "success",
            "message": f"Successfully uploaded {len(uploaded_files)} file(s)",
            "graphname": graphname,
            "uploaded_files": uploaded_files,
            "total_size": total_size,
        }
    
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error uploading files for graph {graphname}: {e}")
        logger.debug_pii(f"Upload error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error uploading files: {str(e)}")


@router.delete(route_prefix + "/{graphname}/uploads")
async def clear_uploaded_files(
    graphname: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    filename: str | None = None,
    session_id: str | None = None,
):
    """
    Clear uploaded files for a specific graphname.
    
    Parameters:
    - graphname: The graph name whose files to clear
    - filename: If provided, only delete this specific file. Otherwise, delete all files.
    - session_id: Optional session ID to delete processed content from temp folder
    """
    try:
        upload_dir = os.path.join("uploads", graphname)
        
        if not os.path.exists(upload_dir):
            return {
                "status": "success",
                "message": f"No files found for graph {graphname}",
                "deleted_files": [],
            }
        
        deleted_files = []
        text_extractor = TextExtractor()
        
        if filename:
            # Delete processed content from JSONL FIRST if session_id provided
            if session_id:
                temp_folder = os.path.join("uploads", "ingestion_temp", graphname, session_id)
                if os.path.exists(temp_folder):
                    logger.info(f"Deleting processed content for {filename} from temp folder")
                    result = text_extractor.delete_file_from_jsonl(temp_folder, filename)
                    if result.get('success'):
                        logger.info(f"Removed {result.get('removed_count', 0)} processed documents for {filename}")
                    else:
                        logger.warning(f"Failed to remove processed content: {result.get('error', 'Unknown error')}")
            
            # Then delete the original file
            file_path = os.path.join(upload_dir, filename)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                os.remove(file_path)
                deleted_files.append(filename)
                logger.info(f"Deleted file {filename} for graph {graphname}")
            else:
                raise HTTPException(status_code=404, detail=f"File {filename} not found")
        else:
            # Delete all files in the directory
            for filename in os.listdir(upload_dir):
                file_path = os.path.join(upload_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    deleted_files.append(filename)
            
            # Remove the directory if it's empty
            if not os.listdir(upload_dir):
                os.rmdir(upload_dir)
            
            logger.info(f"Deleted {len(deleted_files)} file(s) for graph {graphname}")
        
        return {
            "status": "success",
            "message": f"Successfully deleted {len(deleted_files)} file(s)",
            "graphname": graphname,
            "deleted_files": deleted_files,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error deleting files for graph {graphname}: {e}")
        logger.debug_pii(f"Delete error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error deleting files: {str(e)}")


# Cloud Storage Download Endpoints

@router.post(route_prefix + "/{graphname}/cloud/download")
async def download_from_cloud(
    graphname: str,
    credentials: Annotated[HTTPBase, Depends(security)],
    request_body: dict = Body(...),
):
    """
    Download files from cloud storage (S3, GCS, or Azure) to local directory.
    
    Parameters:
    - graphname: The graph name to associate downloaded files with
    - request_body: JSON body containing:
      - provider: Cloud provider (s3, gcs, azure)
      - For S3: access_key, secret_key, bucket, region, prefix
      - For GCS: project_id, gcs_credentials_json, bucket, prefix
      - For Azure: account_name, account_key, container, prefix
    """
    try:
        # Extract parameters from request body
        provider = request_body.get("provider")
        access_key = request_body.get("access_key")
        secret_key = request_body.get("secret_key")
        bucket = request_body.get("bucket")
        region = request_body.get("region")
        prefix = request_body.get("prefix", "")
        project_id = request_body.get("project_id")
        gcs_credentials_json = request_body.get("gcs_credentials_json")
        account_name = request_body.get("account_name")
        account_key = request_body.get("account_key")
        container = request_body.get("container")
        
        download_dir = os.path.join("downloaded_files_cloud", graphname)
        os.makedirs(download_dir, exist_ok=True)
        
        downloaded_files = []
        
        if provider == "s3":
            # Import boto3 for S3
            try:
                import boto3
                from botocore.exceptions import ClientError
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="boto3 is not installed. Please install it to use S3 downloads."
                )
            
            if not all([access_key, secret_key, bucket, region]):
                raise HTTPException(
                    status_code=400,
                    detail="Missing S3 credentials: access_key, secret_key, bucket, and region are required"
                )
            
            # Create S3 client
            s3_client = boto3.client(
                's3',
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )
            
            # List and download objects
            try:
                paginator = s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=bucket, Prefix=prefix or "")
                
                for page in pages:
                    if 'Contents' not in page:
                        continue
                    
                    for obj in page['Contents']:
                        key = obj['Key']
                        # Skip directories
                        if key.endswith('/'):
                            continue
                        
                        # Get filename
                        filename = os.path.basename(key)
                        local_path = os.path.join(download_dir, filename)
                        
                        # Download file
                        s3_client.download_file(bucket, key, local_path)
                        downloaded_files.append(filename)
                        logger.info(f"Downloaded {key} to {local_path}")
                
            except ClientError as e:
                logger.error(f"S3 download error: {e}")
                raise HTTPException(status_code=500, detail=f"S3 error: {str(e)}")
        
        elif provider == "gcs":
            # Import GCS client
            try:
                from google.cloud import storage
                from google.oauth2 import service_account
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="google-cloud-storage is not installed. Please install it to use GCS downloads."
                )
            
            if not all([project_id, gcs_credentials_json, bucket]):
                raise HTTPException(
                    status_code=400,
                    detail="Missing GCS credentials: project_id, gcs_credentials_json, and bucket are required"
                )
            
            try:
                # Parse credentials JSON
                creds_dict = json.loads(gcs_credentials_json)
                credentials = service_account.Credentials.from_service_account_info(creds_dict)
                
                # Create GCS client
                gcs_client = storage.Client(project=project_id, credentials=credentials)
                bucket_obj = gcs_client.bucket(bucket)
                
                # List and download blobs
                blobs = bucket_obj.list_blobs(prefix=prefix or "")
                
                for blob in blobs:
                    # Skip directories
                    if blob.name.endswith('/'):
                        continue
                    
                    # Get filename
                    filename = os.path.basename(blob.name)
                    local_path = os.path.join(download_dir, filename)
                    
                    # Download blob
                    blob.download_to_filename(local_path)
                    downloaded_files.append(filename)
                    logger.info(f"Downloaded {blob.name} to {local_path}")
                    
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid GCS credentials JSON")
            except Exception as e:
                logger.error(f"GCS download error: {e}")
                raise HTTPException(status_code=500, detail=f"GCS error: {str(e)}")
        
        elif provider == "azure":
            # Import Azure Blob Storage client
            try:
                from azure.storage.blob import BlobServiceClient
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="azure-storage-blob is not installed. Please install it to use Azure downloads."
                )
            
            if not all([account_name, account_key, container]):
                raise HTTPException(
                    status_code=400,
                    detail="Missing Azure credentials: account_name, account_key, and container are required"
                )
            
            try:
                # Create Azure Blob Service client
                connection_string = f"DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={account_key};EndpointSuffix=core.windows.net"
                blob_service_client = BlobServiceClient.from_connection_string(connection_string)
                container_client = blob_service_client.get_container_client(container)
                
                # List and download blobs
                blobs = container_client.list_blobs(name_starts_with=prefix or "")
                
                for blob in blobs:
                    # Skip directories
                    if blob.name.endswith('/'):
                        continue
                    
                    # Get filename
                    filename = os.path.basename(blob.name)
                    local_path = os.path.join(download_dir, filename)
                    
                    # Download blob
                    blob_client = container_client.get_blob_client(blob.name)
                    with open(local_path, "wb") as download_file:
                        download_file.write(blob_client.download_blob().readall())
                    
                    downloaded_files.append(filename)
                    logger.info(f"Downloaded {blob.name} to {local_path}")
                    
            except Exception as e:
                logger.error(f"Azure download error: {e}")
                raise HTTPException(status_code=500, detail=f"Azure error: {str(e)}")
        
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported cloud provider: {provider}. Supported: s3, gcs, azure"
            )
        
        if not downloaded_files:
            return {
                "status": "warning",
                "message": "No files found in the specified cloud storage location",
                "graphname": graphname,
                "provider": provider,
                "downloaded_files": [],
            }
        
        logger.info(f"Downloaded {len(downloaded_files)} file(s) from {provider} for graph {graphname}")
        
        return {
            "status": "success",
            "message": f"Successfully downloaded {len(downloaded_files)} file(s) from {provider}",
            "graphname": graphname,
            "provider": provider,
            "downloaded_files": downloaded_files,
            "local_path": download_dir,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error downloading from cloud for graph {graphname}: {e}")
        logger.debug_pii(f"Cloud download error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error downloading from cloud: {str(e)}")


@router.get(route_prefix + "/{graphname}/cloud/list")
async def list_cloud_downloads(
    graphname: str,
    credentials: Annotated[HTTPBase, Depends(security)],
):
    """
    List downloaded files from cloud storage for a specific graph.
    
    Parameters:
    - graphname: The graph name to list downloaded files for
    """
    try:
        download_dir = os.path.join("downloaded_files_cloud", graphname)
        
        if not os.path.exists(download_dir):
            return {
                "status": "success",
                "graphname": graphname,
                "files": [],
                "count": 0,
            }
        
        files = []
        for filename in os.listdir(download_dir):
            file_path = os.path.join(download_dir, filename)
            if os.path.isfile(file_path):
                file_stat = os.stat(file_path)
                files.append({
                    "name": filename,
                    "size": file_stat.st_size,
                    "modified": file_stat.st_mtime,
                })
        
        return {
            "status": "success",
            "graphname": graphname,
            "files": files,
            "count": len(files),
        }
    
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error listing cloud downloads for graph {graphname}: {e}")
        logger.debug_pii(f"List error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error listing files: {str(e)}")


@router.delete(route_prefix + "/{graphname}/cloud/delete")
async def delete_cloud_downloads(
    graphname: str,
    credentials: Annotated[HTTPBase, Depends(security)],
    filename: str = None,
    session_id: str = None,
):
    """
    Delete downloaded cloud files for a specific graph.
    
    Parameters:
    - graphname: The graph name whose downloaded files to clear
    - filename: If provided, only delete this specific file. Otherwise, delete all files.
    - session_id: Optional session ID to delete processed content from temp folder
    """
    try:
        download_dir = os.path.join("downloaded_files_cloud", graphname)
        
        if not os.path.exists(download_dir):
            return {
                "status": "success",
                "message": f"No downloaded files found for graph {graphname}",
                "deleted_files": [],
            }
        
        deleted_files = []
        text_extractor = TextExtractor()
        
        if filename:
            # Delete processed content from JSONL FIRST if session_id provided
            if session_id:
                temp_folder = os.path.join("uploads", "ingestion_temp", graphname, session_id)
                if os.path.exists(temp_folder):
                    logger.info(f"Deleting processed content for {filename} from temp folder")
                    result = text_extractor.delete_file_from_jsonl(temp_folder, filename)
                    if result.get('success'):
                        logger.info(f"Removed {result.get('removed_count', 0)} processed documents for {filename}")
                    else:
                        logger.warning(f"Failed to remove processed content: {result.get('error', 'Unknown error')}")
            
            # Then delete the original file
            file_path = os.path.join(download_dir, filename)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                os.remove(file_path)
                deleted_files.append(filename)
                logger.info(f"Deleted cloud download {filename} for graph {graphname}")
            else:
                raise HTTPException(status_code=404, detail=f"File {filename} not found")
        else:
            # Delete all files in the directory
            for filename in os.listdir(download_dir):
                file_path = os.path.join(download_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    deleted_files.append(filename)
            
            # Remove the directory if it's empty
            if not os.listdir(download_dir):
                os.rmdir(download_dir)
            
            logger.info(f"Deleted {len(deleted_files)} cloud download(s) for graph {graphname}")
        
        return {
            "status": "success",
            "message": f"Successfully deleted {len(deleted_files)} file(s)",
            "graphname": graphname,
            "deleted_files": deleted_files,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error deleting cloud downloads for graph {graphname}: {e}")
        logger.debug_pii(f"Delete error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error deleting files: {str(e)}")


@router.delete(route_prefix + "/{graphname}/ingestion_temp/delete")
async def delete_ingestion_temp_files(
    graphname: str,
    credentials: Annotated[HTTPBase, Depends(security)],
    session_id: str = None,
    filename: str = None,
):
    """
    Delete files from ingestion temp folder.
    """
    try:
        base_temp_dir = os.path.join("uploads", "ingestion_temp", graphname)
        
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        
        session_dir = os.path.join(base_temp_dir, session_id)
        
        if not os.path.exists(session_dir):
            return {
                "status": "success",
                "message": f"No temp files found for session {session_id}",
                "deleted_files": [],
            }
        
        deleted_files = []
        
        if filename:
            # Delete specific file
            file_path = os.path.join(session_dir, filename)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                os.remove(file_path)
                deleted_files.append(filename)
                logger.info(f"Deleted temp file {filename} from session {session_id}")
                
                # If session folder is now empty, remove it
                if not os.listdir(session_dir):
                    os.rmdir(session_dir)
                    logger.info(f"Removed empty session folder {session_id}")
            else:
                raise HTTPException(status_code=404, detail=f"File {filename} not found")
        else:
            # Delete entire session folder
            import shutil
            for filename in os.listdir(session_dir):
                if os.path.isfile(os.path.join(session_dir, filename)):
                    deleted_files.append(filename)
            
            shutil.rmtree(session_dir)
            logger.info(f"Deleted session folder {session_id} for graph {graphname}")
        
        return {
            "status": "success",
            "message": f"Successfully deleted {len(deleted_files)} file(s)",
            "deleted_files": deleted_files,
            "session_id": session_id,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error deleting ingestion temp files for graph {graphname}: {e}")
        logger.debug_pii(f"Delete error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error deleting temp files: {str(e)}")