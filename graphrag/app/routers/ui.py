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

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import threading
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
    Path,
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

from common.config import db_config, graphrag_config, embedding_service, llm_config, service_status, get_chat_config, get_completion_config, get_embedding_config, get_multimodal_config, validate_graphname, get_llm_service, resolve_llm_services
from common.db.connections import get_db_connection_pwd_manual
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics as pmetrics
from common.utils.graph_locks import acquire_graph_lock, release_graph_lock, acquire_rebuild_lock, release_rebuild_lock, get_rebuilding_graph
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

logger = logging.getLogger(__name__)

TRACE_LOGS_DIR = os.environ.get("TRACE_LOGS_DIR", "/code/trace_logs")

def _save_trace_log(message_id: str, conversation_id: str, user_query: str, resp: GraphRAGResponse, elapsed: float):
    try:
        os.makedirs(TRACE_LOGS_DIR, exist_ok=True)
        trace_data = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "user_query": user_query,
            "response_time": elapsed,
            "response_type": resp.response_type,
            "answered_question": resp.answered_question,
            "query_sources": resp.query_sources,
            "natural_language_response": resp.natural_language_response,
            "timestamp": time.time(),
        }
        filepath = os.path.join(TRACE_LOGS_DIR, f"{message_id}.json")
        with open(filepath, "w") as f:
            json.dump(trace_data, f, default=str)
    except Exception:
        logger.warning(f"Failed to save trace log for message {message_id}", exc_info=True)

# Validated graph name path parameter — rejects path traversal characters
ValidGraphName = Annotated[str, Path(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

use_cypher = os.getenv("USE_CYPHER", "false").lower() == "true"
route_prefix = "/ui"  # APIRouter's prefix doesn't work with the websocket, so it has to be done here
router = APIRouter(tags=["UI"])
security = HTTPBasic()
llm_config_lock = asyncio.Lock()

# Cache for user role lookups (avoids repeated GSQL calls)
# Key: (username, password_hash) -> (timestamp, (global_roles, graph_roles))
_role_cache: dict[tuple[str, str], tuple[float, tuple[list[str], dict[str, list[str]]]]] = {}
_role_cache_lock = threading.Lock()
_ROLE_CACHE_TTL = 60  # seconds

def _normalize_roles(raw_roles: str) -> list[str]:
    cleaned = re.sub(r"[\[\]]", "", raw_roles).strip()
    if not cleaned or cleaned.lower() == "none":
        return []
    return [r.strip().lower() for r in re.split(r"[,\s]+", cleaned) if r.strip()]


def _parse_user_roles_detail(user_info: str, username: str) -> tuple[list[str], dict[str, list[str]]]:
    global_roles: list[str] = []
    graph_roles: dict[str, list[str]] = {}
    is_user_section = False
    for line in user_info.splitlines():
        line_stripped = line.strip()
        match = re.match(
            r"^[\*\-]?\s*\-?\s*(Name|User Name|User)\s*:\s*(.+)$",
            line_stripped,
            re.IGNORECASE,
        )
        if match:
            current_name = match.group(2).strip()
            is_user_section = current_name == username
            continue
        if not is_user_section:
            continue

        roles_match = re.match(
            r"^[\*\-]?\s*\-?\s*(Global Roles|Roles)\s*:\s*(.+)$",
            line_stripped,
            re.IGNORECASE,
        )
        if roles_match:
            global_roles.extend(_normalize_roles(roles_match.group(2)))
            continue

        graph_roles_match = re.match(
            r"^[\*\-]?\s*\-?\s*Graph\s+'([^']+)'\s+Roles\s*:\s*(.+)$",
            line_stripped,
            re.IGNORECASE,
        )
        if graph_roles_match:
            graph_name = graph_roles_match.group(1).strip()
            roles = _normalize_roles(graph_roles_match.group(2))
            if roles:
                graph_roles[graph_name] = roles

    return global_roles, graph_roles


def _parse_user_roles(user_info: str, username: str) -> list[str]:
    global_roles, _ = _parse_user_roles_detail(user_info, username)
    return global_roles

def _get_user_role_details(username: str, password: str) -> tuple[list[str], dict[str, list[str]]]:
    """Get user roles with short TTL cache to avoid repeated GSQL calls."""
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()[:16]
    cache_key = (username, pwd_hash)
    now = time.time()

    with _role_cache_lock:
        cached = _role_cache.get(cache_key)
        if cached and (now - cached[0]) < _ROLE_CACHE_TTL:
            return cached[1]

    conn = TigerGraphConnection(
        host=db_config.get("hostname"),
        username=username,
        password=password,
        gsPort=db_config.get("gsPort"),
        restppPort=db_config.get("restppPort"),
        graphname="",
    )
    user_info = conn.gsql("SHOW USER")
    result = _parse_user_roles_detail(user_info, username)

    with _role_cache_lock:
        _role_cache[cache_key] = (now, result)

    return result


def _get_user_roles(username: str, password: str) -> list[str]:
    global_roles, _ = _get_user_role_details(username, password)
    return global_roles

def _require_roles(credentials: HTTPBasicCredentials, allowed_roles: set[str]) -> list[str]:
    try:
        roles = _get_user_roles(credentials.username, credentials.password)
    except Exception as e:
        logger.error(f"Failed to resolve user roles: {e}")
        raise HTTPException(status_code=403, detail="Unable to verify user roles.")
    if not any(role in allowed_roles for role in roles):
        raise HTTPException(status_code=403, detail="Insufficient permissions.")
    return roles


def _create_embedding_service(provider: str, config: dict):
    from common.embeddings.embedding_services import (
        OpenAI_Embedding, AzureOpenAI_Ada002, GenAI_Embedding,
        VertexAI_PaLM_Embedding, AWS_Bedrock_Embedding, Ollama_Embedding
    )
    providers = {
        "openai": OpenAI_Embedding,
        "azure": AzureOpenAI_Ada002,
        "genai": GenAI_Embedding,
        "vertexai": VertexAI_PaLM_Embedding,
        "bedrock": AWS_Bedrock_Embedding,
        "ollama": Ollama_Embedding,
    }
    cls = providers.get(provider.lower())
    return cls(config) if cls else None


def _require_prompt_access(credentials: HTTPBasicCredentials, graphname: str | None) -> str:
    """
    Check if user can access prompts. Returns access level: 'full' or 'chatbot_only'.
    Raises 403 for globalobserver or any user without sufficient access.
    - superuser / globaldesigner  → 'full'   (can edit all prompts)
    - graph admin on graphname    → 'chatbot_only'  (can only edit chatbot_response)
    """
    if graphname:
        validate_graphname(graphname)
    try:
        global_roles, graph_roles = _get_user_role_details(credentials.username, credentials.password)
    except Exception as e:
        logger.error(f"Failed to resolve user roles: {e}")
        raise HTTPException(status_code=403, detail="Unable to verify user roles.")
    if any(role in {"superuser", "globaldesigner"} for role in global_roles):
        return "full"
    if graphname and any(role in {"admin"} for role in graph_roles.get(graphname, [])):
        return "chatbot_only"
    raise HTTPException(status_code=403, detail="Insufficient permissions.")


def _resolve_llm_config_access(
    credentials: HTTPBasicCredentials, graphname: str | None
) -> str:
    if graphname:
        validate_graphname(graphname)
    try:
        global_roles, graph_roles = _get_user_role_details(
            credentials.username, credentials.password
        )
    except Exception as e:
        logger.error(f"Failed to resolve user roles: {e}")
        raise HTTPException(status_code=403, detail="Unable to verify user roles.")

    if any(role in {"superuser", "globaldesigner"} for role in global_roles):
        return "full"
    if graphname:
        roles_for_graph = graph_roles.get(graphname, [])
        if any(role in {"admin"} for role in roles_for_graph):
            return "chatbot_only"
    raise HTTPException(status_code=403, detail="Insufficient permissions.")

def _ecc_jobs_running(graphs: list[str], auth_header: str) -> bool:
    if not graphs:
        return False
    ecc_base = graphrag_config.get("ecc", "http://graphrag-ecc:8001")
    for graphname in graphs:
        try:
            status_url = f"{ecc_base}/{graphname}/graphrag/rebuild_status"
            response = httpx.get(
                status_url,
                headers={"Authorization": auth_header},
                timeout=5.0,
            )
            if response.status_code == 200:
                payload = response.json()
                if payload.get("is_running"):
                    return True
        except Exception as e:
            logger.warning(f"ECC status check failed for {graphname}: {e}")
            continue
    return False


def auth(usr: str, password: str, conn=None) -> tuple[list[str], TigerGraphConnection]:
    if conn is None:
        conn = TigerGraphConnection(
            host=db_config["hostname"], graphname="", username=usr, password=password
        )

    try:
        graph_list = conn.listGraphs()
        graphs = [g["graphName"] for g in graph_list if "graphName" in g]

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
    creds = auth[1]
    # Fetch roles at login so frontend doesn't need separate /roles calls
    try:
        global_roles, graph_roles = _get_user_role_details(creds.username, creds.password)
    except Exception as e:
        logger.warning(f"Failed to fetch roles at login: {e}")
        global_roles, graph_roles = [], {}
    return {"graphs": graphs, "roles": global_roles, "graph_roles": graph_roles}


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


@router.get(route_prefix + "/trace/{message_id}")
def get_trace_log(
    message_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    filepath = os.path.join(TRACE_LOGS_DIR, f"{message_id}.json")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Trace log not found")
    with open(filepath, "r") as f:
        return json.load(f)



@router.post(route_prefix + "/{graphname}/create_graph")
def create_graph(
    graphname: ValidGraphName,
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
    graphname: ValidGraphName,
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
async def forceupdate(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    bg_tasks: BackgroundTasks,
):
    """
    Force update/refresh of a GraphRAG knowledge graph.
    This triggers the ECC (Eventual Consistency Checker) service to rebuild the graph.
    Only ONE rebuild can run at a time across all graphs (resource-intensive operation).
    Uses HTTP Basic Authentication to get credentials.
    
    The lock is held until ALL 4 stages complete:
    1. Doc Processing (chunk, embed, extract)
    2. Type Processing
    3. Entity Processing (resolution)
    4. Community Processing (detection & summarization)
    """
    # Check if another graph is already rebuilding
    currently_rebuilding = get_rebuilding_graph()
    if currently_rebuilding and currently_rebuilding != graphname:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{currently_rebuilding}' is currently being rebuilt. Only one rebuild allowed at a time."
        )
    
    # Try to acquire global rebuild lock (async, non-blocking)
    if not await acquire_rebuild_lock(graphname):
        currently_rebuilding = get_rebuilding_graph()
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{currently_rebuilding}' is currently being rebuilt. Only one rebuild allowed at a time."
        )
    
    # Extract credentials from the dependency
    creds = creds[1]
    auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()

    ecc_base = graphrag_config.get("ecc", "http://graphrag-ecc:8001")
    ecc_update_url = f"{ecc_base}/{graphname}/graphrag/consistency_update"
    ecc_status_url = f"{ecc_base}/{graphname}/graphrag/rebuild_status"
    
    LogWriter.info(f"Sending ECC rebuild request to: {ecc_update_url}")
    
    # Background task to trigger rebuild, monitor completion, and release lock
    async def rebuild_and_monitor():
        try:
            # Step 1: Trigger the ECC rebuild (non-blocking)
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(ecc_update_url, headers={"Authorization": f"Basic {auth}"})
                if response.status_code not in [200, 202]:
                    LogWriter.error(f"ECC rebuild trigger failed for {graphname}: {response.status_code} - {response.text}")
                    return
            
            LogWriter.info(f"ECC rebuild triggered for {graphname}, now monitoring status...")
            
            # Step 2: Poll ECC status until all 4 stages complete (non-blocking)
            max_wait_time = 7200  # 2 hours max
            poll_interval = 5  # Check every 5 seconds
            elapsed = 0
            
            while elapsed < max_wait_time:
                await asyncio.sleep(poll_interval)  # Non-blocking sleep
                elapsed += poll_interval
                
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        status_response = await client.get(
                            ecc_status_url, 
                            headers={"Authorization": f"Basic {auth}"}
                        )
                    
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        is_running = status_data.get("is_running", False)
                        status = status_data.get("status", "unknown")
                        
                        # Log every minute to avoid spam
                        if elapsed % 60 == 0:
                            LogWriter.info(f"ECC status for {graphname}: {status} (running={is_running}) - elapsed {elapsed}s")
                        
                        # Check if ALL stages are complete
                        if not is_running and status in ["completed", "failed", "idle"]:
                            LogWriter.info(f"ECC rebuild finished for {graphname} with status: {status} after {elapsed}s")
                            break
                    else:
                        LogWriter.warning(f"ECC status check returned {status_response.status_code} for {graphname}")
                        
                except Exception as e:
                    LogWriter.warning(f"Failed to check ECC status for {graphname}: {e}")
                    # Continue polling - ECC might still be working
            
            if elapsed >= max_wait_time:
                LogWriter.error(f"ECC rebuild monitoring timed out for {graphname} after {max_wait_time}s")
                
        except Exception as e:
            LogWriter.error(f"Error during ECC rebuild monitoring for {graphname}: {e}")
            import traceback
            LogWriter.error(traceback.format_exc())
        finally:
            # Release lock only after ALL stages complete (or timeout/error)
            release_rebuild_lock(graphname)
            LogWriter.info(f"Released global rebuild lock for {graphname}")
    
    bg_tasks.add_task(rebuild_and_monitor)
    return {"status": "submitted"}


@router.get(route_prefix + "/{graphname}/rebuild_status")
def get_rebuild_status(
    graphname: ValidGraphName,
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
            graphrag_config.get("ecc", "http://graphrag-ecc:8001")
            + f"/{graphname}/graphrag/rebuild_status"
        )
        LogWriter.info(f"Checking ECC status at: {ecc_status_url}")
        
        response = httpx.get(
            ecc_status_url,
            headers={"Authorization": f"Basic {auth}"},
            timeout=30.0
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
    except httpx.TimeoutException as e:
        # ECC is busy (heavy processing) - assume rebuild is still running
        LogWriter.warning(f"ECC status check timed out (ECC may be busy): {str(e)}")
        return {
            "graphname": graphname,
            "is_running": True,
            "status": "unknown",
            "error": "ECC is busy processing, status check timed out. Rebuild likely still in progress."
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
    graphname: ValidGraphName,
    cfg: CreateIngestConfig,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Create an ingest configuration for a GraphRAG knowledge graph.
    This sets up the data source and load job configuration for document ingestion.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    # Check if this graph is currently being rebuilt
    currently_rebuilding = get_rebuilding_graph()
    if currently_rebuilding == graphname:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being rebuilt. Please wait for the rebuild to complete before ingesting documents."
        )
    
    # Acquire graph lock
    if not acquire_graph_lock(graphname, "create_ingest"):
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being processed by another operation. Please wait and try again."
        )
    
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Create the ingest configuration
        LogWriter.info(f"Creating ingest configuration for graph: {graphname}")
        result = supportai.create_ingest(graphname, cfg, conn)

        return result

    except HTTPException:
        raise
    except Exception as e:
        LogWriter.error(f"Error creating ingest configuration for graph {graphname}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create ingest configuration: {str(e)}"
        )
    finally:
        release_graph_lock(graphname, "create_ingest")


@router.post(route_prefix + "/{graphname}/ingest")
def ingest(
    graphname: ValidGraphName,
    loader_info: LoadingInfo,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """
    Run document ingestion for a GraphRAG knowledge graph.
    This processes documents from the configured data source and loads them into the graph.
    Uses HTTP Basic Authentication to get credentials and create a connection.
    """
    # Check if this graph is currently being rebuilt
    currently_rebuilding = get_rebuilding_graph()
    if currently_rebuilding == graphname:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being rebuilt. Please wait for the rebuild to complete before ingesting documents."
        )
    
    # Acquire graph lock
    if not acquire_graph_lock(graphname, "ingest"):
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being processed by another operation. Please wait and try again."
        )
    
    try:
        # Extract credentials from the dependency (same pattern as other endpoints)
        creds = creds[1]
        auth = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        _, conn = ws_basic_auth(auth, graphname)

        # Run the ingestion
        LogWriter.info(f"Running ingestion for graph: {graphname}")
        result = supportai.ingest(graphname, loader_info, conn)

        return result

    except HTTPException:
        raise
    except Exception as e:
        LogWriter.error(f"Error running ingestion for graph {graphname}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run ingestion: {str(e)}"
        )
    finally:
        release_graph_lock(graphname, "ingest")


@router.get(route_prefix + "/image_vertex/{graphname}/{image_id}")
async def serve_image_from_vertex(
    graphname: ValidGraphName,
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


@router.get(route_prefix + "/roles")
async def get_user_roles(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)]
):
    roles, graph_roles = _get_user_role_details(
        credentials.username, credentials.password
    )
    return {"roles": roles, "graph_roles": graph_roles}


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
        error_msg = str(e)
        if "does not exist" in error_msg or "not found" in error_msg.lower():
            resp.natural_language_response = f"Error: {error_msg}. Please check the knowledge graph name and try again."
        else:
            resp.natural_language_response = "GraphRAG had an issue answering your question. Please try again, or rephrase your prompt."

        resp.query_sources = {}
        resp.answered_question = False
        LogWriter.warning(
            f"/{graphname}/ui/chat request_id={req_id_cv.get()} agent execution failed due to exception: {e}"
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
    graphname: ValidGraphName,
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
            model=get_chat_config(graphname).get("llm_model", "unknown"),
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
            model=get_chat_config(graphname).get("llm_model", "unknown"),
            content=resp.natural_language_response,
            role=Role.SYSTEM,
            response_time=elapsed,
            answered_question=resp.answered_question,
            response_type=resp.response_type,
            query_sources=resp.query_sources,
        )
        await write_message_to_history(message, auth)
        await asyncio.to_thread(_save_trace_log, message.message_id, convo_id, data, resp, elapsed)
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
    graphname: ValidGraphName,
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
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected during authentication")
        return
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        try:
            await websocket.close(code=1008, reason="Authentication failed")
        except Exception:
            pass
        return

    # Get RAG pattern
    rag_pattern = rag_pattern or "hybridsearch"

    # Get conversation ID
    try:
        conversation_id = await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected before conversation ID received")
        return
    logger.info(
        f"WebSocket conversation_id received: {conversation_id or 'empty'} "
        f"(graph={graphname}, rag_pattern={rag_pattern})"
    )
    
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
                model=get_chat_config(graphname).get("llm_model", "unknown"),
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
                model=get_chat_config(graphname).get("llm_model", "unknown"),
                content=resp.natural_language_response,
                role=Role.SYSTEM,
                response_time=elapsed,
                answered_question=resp.answered_question,
                response_type=resp.response_type,
                query_sources=resp.query_sources,
            )
            await write_message_to_history(message, usr_auth)
            await asyncio.to_thread(_save_trace_log, message.message_id, convo_id, data, resp, elapsed)
            prev_id = message.message_id

            # reply
            await websocket.send_text(message.model_dump_json())

            # append message to history
            conversation_history.append(
                {"query": data, "response": resp.natural_language_response}
            )
    except WebSocketDisconnect as e:
        close_code = getattr(e, "code", None)
        close_reason = getattr(e, "reason", None)
        logger.info(
            f"Websocket disconnected (code={close_code}, reason={close_reason})"
        )
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(
            f"Websocket error (graph={graphname}, conversation_id={convo_id}): {e}\n{exc}"
        )
        await websocket.close()


# =====================================================
# File Upload Functionality for Server +Multi
# =====================================================

@router.get(route_prefix + "/{graphname}/uploads/list")
async def list_uploaded_files(
    graphname: ValidGraphName,
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
    graphname: ValidGraphName,
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
    # Acquire graph lock
    acquired = await asyncio.to_thread(acquire_graph_lock, graphname, "upload_files")
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being processed by another operation. Please wait and try again."
        )
    
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
    
    except HTTPException:
        raise
    except Exception as e:
        exc = traceback.format_exc()
        logger.error(f"Error uploading files for graph {graphname}: {e}")
        logger.debug_pii(f"Upload error trace:\n{exc}")
        raise HTTPException(status_code=500, detail=f"Error uploading files: {str(e)}")
    finally:
        await asyncio.to_thread(release_graph_lock, graphname, "upload_files")


@router.delete(route_prefix + "/{graphname}/uploads")
async def clear_uploaded_files(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    filename: str | None = None,
):
    """
    Clear uploaded files for a specific graphname.
    
    Parameters:
    - graphname: The graph name whose files to clear
    - filename: If provided, only delete this specific file. Otherwise, delete all files.
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
        
        if filename:
            # Delete corresponding JSONL file from temp folder FIRST
            temp_folder = os.path.join("uploads", "ingestion_temp", graphname)
            if os.path.exists(temp_folder):
                from pathlib import Path
                file_stem = Path(filename).stem
                jsonl_file = os.path.join(temp_folder, f"{file_stem}.jsonl")
                
                if os.path.exists(jsonl_file):
                    os.remove(jsonl_file)
                    logger.info(f"Deleted corresponding JSONL file: {file_stem}.jsonl")
                    
                    # If temp folder is now empty, remove it
                    if not os.listdir(temp_folder):
                        os.rmdir(temp_folder)
                        logger.info(f"Removed empty temp folder for graph {graphname}")
            
            # Then delete the raw file
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
            
            # Also delete the entire temp folder for this graph
            temp_folder = os.path.join("uploads", "ingestion_temp", graphname)
            if os.path.exists(temp_folder):
                import shutil
                shutil.rmtree(temp_folder)
                logger.info(f"Deleted temp folder for graph {graphname}")
            
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
    graphname: ValidGraphName,
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
    # Acquire graph lock
    acquired = await asyncio.to_thread(acquire_graph_lock, graphname, "download_from_cloud")
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently being processed by another operation. Please wait and try again."
        )
    
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
    finally:
        await asyncio.to_thread(release_graph_lock, graphname, "download_from_cloud")


@router.get(route_prefix + "/{graphname}/cloud/list")
async def list_cloud_downloads(
    graphname: ValidGraphName,
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
    graphname: ValidGraphName,
    credentials: Annotated[HTTPBase, Depends(security)],
    filename: str = None,
):
    """
    Delete downloaded cloud files for a specific graph.
    
    Parameters:
    - graphname: The graph name whose downloaded files to clear
    - filename: If provided, only delete this specific file. Otherwise, delete all files.
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
        
        if filename:
            # Delete corresponding JSONL file from temp folder FIRST
            temp_folder = os.path.join("downloaded_files_cloud", "ingestion_temp", graphname)
            if os.path.exists(temp_folder):
                from pathlib import Path
                file_stem = Path(filename).stem
                jsonl_file = os.path.join(temp_folder, f"{file_stem}.jsonl")
                
                if os.path.exists(jsonl_file):
                    os.remove(jsonl_file)
                    logger.info(f"Deleted corresponding JSONL file: {file_stem}.jsonl")
                    
                    # If temp folder is now empty, remove it
                    if not os.listdir(temp_folder):
                        os.rmdir(temp_folder)
                        logger.info(f"Removed empty temp folder for graph {graphname}")
            
            # Then delete the raw file
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
            
            # Also delete the entire temp folder for this graph
            temp_folder = os.path.join("downloaded_files_cloud", "ingestion_temp", graphname)
            if os.path.exists(temp_folder):
                import shutil
                shutil.rmtree(temp_folder)
                logger.info(f"Deleted temp folder for graph {graphname}")
            
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


@router.post(f"{route_prefix}/config/llm")
async def save_llm_config(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    llm_config_data: dict = Body(...)
):
    """
    Save LLM configuration and reload services.
    """
    try:
        graphname = llm_config_data.get("graphname")
        llm_access_mode = _resolve_llm_config_access(credentials, graphname)
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = "Basic " + base64.b64encode(
            f"{credentials.username}:{credentials.password}".encode()
        ).decode()
        if _ecc_jobs_running(graphs, auth_header):
            raise HTTPException(
                status_code=409,
                detail="ECC rebuild in progress. Please wait for it to complete before updating config."
            )
        if llm_config_lock.locked():
            raise HTTPException(
                status_code=409,
                detail="LLM config update already in progress. Please try again shortly."
            )
        async with llm_config_lock:
            # Save and reload in graphrag service
            from common.config import reload_llm_config

            candidate, graphname, scope = _prepare_llm_config(llm_config_data)

            if llm_access_mode == "chatbot_only" or (llm_access_mode == "full" and scope == "graph"):
                # Per-graph save: write only overrides to graph config file.
                # chatbot_only: can only set chat_service
                # full + scope=graph: can set completion_service, chat_service, multimodal_service
                from common.config import _config_file_lock

                if not graphname:
                    raise HTTPException(status_code=400, detail="graphname is required for per-graph config")

                graph_config_dir = f"configs/graph_configs/{graphname}"
                os.makedirs(graph_config_dir, exist_ok=True)
                graph_config_path = os.path.join(graph_config_dir, "server_config.json")

                with _config_file_lock:
                    if os.path.exists(graph_config_path):
                        with open(graph_config_path, "r") as f:
                            graph_server_config = json.load(f)
                    else:
                        graph_server_config = {}

                    graph_llm = graph_server_config.setdefault("llm_config", {})

                    if llm_access_mode == "chatbot_only":
                        # Graph admin: only chat_service
                        svc_keys = ["chat_service"]
                    else:
                        # Superadmin per-graph: all services
                        svc_keys = ["completion_service", "embedding_service", "chat_service", "multimodal_service"]

                    # Resolve both candidate and global to get fully expanded configs,
                    # then store only the delta as the graph override.
                    resolved_candidate = resolve_llm_services(candidate)
                    resolved_global = resolve_llm_services(llm_config)

                    for svc_key in svc_keys:
                        incoming = candidate.get(svc_key)
                        if incoming:
                            rc = resolved_candidate.get(svc_key, {})
                            rg = resolved_global.get(svc_key, {})
                            # Compute delta: keys whose resolved values differ
                            delta = {}
                            for k, v in rc.items():
                                if k == "authentication_configuration":
                                    continue
                                if rg.get(k) != v:
                                    delta[k] = v
                            if delta:
                                graph_llm[svc_key] = delta
                            else:
                                graph_llm.pop(svc_key, None)
                        else:
                            # Revert to inherit: remove override
                            graph_llm.pop(svc_key, None)

                    temp_file = f"{graph_config_path}.tmp"
                    with open(temp_file, "w") as f:
                        json.dump(graph_server_config, f, indent=2)
                    os.replace(temp_file, graph_config_path)

                result = {"status": "success"}
            else:
                # Superadmin global save
                result = reload_llm_config(candidate)

            if result["status"] != "success":
                raise HTTPException(status_code=500, detail=result["message"])
        
            return {
                "status": "success",
                "message": "Configuration saved successfully"
            }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving config: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(f"{route_prefix}/config/llm/test")
async def test_llm_config(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    llm_test_config: dict = Body(...)
):
    """
    Test LLM configuration by making actual API calls to the provider.
    Tests completion, embedding, and multimodal services.
    """
    test_results = {
        "completion": {"status": "not_tested", "message": ""},
        "chatbot": {"status": "not_tested", "message": ""},
        "embedding": {"status": "not_tested", "message": ""},
        "multimodal": {"status": "not_tested", "message": ""}
    }
    try:
        graphname = llm_test_config.get("graphname")
        llm_access_mode = _resolve_llm_config_access(credentials, graphname)

        # Build candidate config — same preparation as save
        candidate, graphname, scope = _prepare_llm_config(llm_test_config)
        # Resolve partial service configs into full configs for testing
        # (same resolution logic used when parsing config from disk)
        test_configs = resolve_llm_services(candidate)

        # Graph admins (chatbot_only) can only test chat_service
        if llm_access_mode == "chatbot_only":
            if "chat_service" in candidate:
                try:
                    test_config = test_configs["chat_service"]
                    model = test_config.get("llm_model", "")
                    llm_service = get_llm_service(test_config)
                    response = llm_service.llm.invoke("Say 'Connection successful' in 2 words")
                    if not response or not str(response).strip():
                        raise ValueError("LLM returned an empty response")
                    test_results["chatbot"]["status"] = "success"
                    test_results["chatbot"]["message"] = f"Chatbot LLM ({model}) connected successfully"
                except Exception as e:
                    test_results["chatbot"]["status"] = "error"
                    test_results["chatbot"]["message"] = f"Chatbot test failed: {str(e)}"
                    logger.error(f"Chatbot test failed for graph {graphname}: {str(e)}")

            chatbot_status = test_results["chatbot"]["status"]
            overall_status = "success" if chatbot_status == "success" else ("error" if chatbot_status == "error" else "not_tested")
            return {
                "status": overall_status,
                "message": "Connection test completed",
                "results": {"chatbot": test_results["chatbot"]}
            }

        # Full access: test all services from the resolved test configs

        # Test Completion Service
        if "completion_service" in test_configs:
            try:
                test_config = test_configs["completion_service"]
                model = test_config.get("llm_model", "")
                llm_service = get_llm_service(test_config)
                response = llm_service.llm.invoke("Say 'Connection successful' in 2 words")
                if not response or not str(response).strip():
                    raise ValueError("LLM returned an empty response")
                test_results["completion"]["status"] = "success"
                test_results["completion"]["message"] = f"Completion model ({model}) connected successfully"
            except Exception as e:
                test_results["completion"]["status"] = "error"
                test_results["completion"]["message"] = f"Completion test failed: {str(e)}"
                logger.error(f"Completion test failed: {str(e)}")

        # Test Chatbot Service (only if custom config provided in candidate;
        # when inheriting from completion, the completion test already covers it)
        if "chat_service" in candidate:
            try:
                test_config = test_configs["chat_service"]
                model = test_config.get("llm_model", "")
                llm_service = get_llm_service(test_config)
                response = llm_service.llm.invoke("Say 'Connection successful' in 2 words")
                if not response or not str(response).strip():
                    raise ValueError("LLM returned an empty response")
                test_results["chatbot"]["status"] = "success"
                test_results["chatbot"]["message"] = f"Chatbot LLM model ({model}) connected successfully"
            except Exception as e:
                test_results["chatbot"]["status"] = "error"
                test_results["chatbot"]["message"] = f"Chatbot test failed: {str(e)}"
                logger.error(f"Chatbot test failed: {str(e)}")

        # Test Embedding Service
        if "embedding_service" in test_configs:
            try:
                test_config = test_configs["embedding_service"]
                provider = test_config.get("embedding_model_service", "openai").lower()
                model = test_config.get("model_name", "")
                embedding_service_test = _create_embedding_service(provider, test_config)
                if not embedding_service_test:
                    raise ValueError(f"Provider '{provider}' not supported for embeddings")
                embeddings = embedding_service_test.embed_query("test connection")
                if not embeddings or len(embeddings) == 0:
                    raise ValueError("Embedding returned empty result")
                test_results["embedding"]["status"] = "success"
                test_results["embedding"]["message"] = f"Embedding model ({model}) connected successfully"
            except Exception as e:
                test_results["embedding"]["status"] = "error"
                test_results["embedding"]["message"] = f"Embedding test failed: {str(e)}"
                logger.error(f"Embedding test failed: {str(e)}")

        # Test Multimodal Service — verifies the model supports vision
        # When multimodal_service is absent (inheriting), use completion_service
        # config — that's what will be used at runtime after save.
        multimodal_config = test_configs.get("multimodal_service") or test_configs.get("completion_service")
        if multimodal_config:
            model = ""
            try:
                from langchain_core.messages import HumanMessage
                test_config = multimodal_config
                model = test_config.get("llm_model", "")
                llm_service = get_llm_service(test_config)
                # Send a small 20x20 red PNG to verify the model accepts image input.
                # Some providers (e.g. Bedrock) reject 1x1 images.
                TEST_IMAGE_B64 = (
                    "iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAIAAAAC64paAAAAKUlEQVR4"
                    "nGP8z0A+YKJAL8OoZhIBE6kakMGoZhIBE6kakMGoZhIBRZoBIpwBJy3"
                    "phGMAAAAASUVORK5CYII="
                )
                provider = test_config.get("llm_service", "").lower()
                # Google GenAI/VertexAI only accept image_url format;
                # Bedrock/Anthropic-native providers prefer type:"image" with source.
                if provider in ("genai", "vertexai"):
                    image_block = {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{TEST_IMAGE_B64}"},
                    }
                else:
                    image_block = {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": TEST_IMAGE_B64,
                        },
                    }
                vision_message = HumanMessage(
                    content=[
                        {"type": "text", "text": "Describe this image in one word."},
                        image_block,
                    ]
                )
                response = llm_service.llm.invoke([vision_message])
                if not response or not str(response).strip():
                    raise ValueError("Multimodal LLM returned an empty response")
                test_results["multimodal"]["status"] = "success"
                test_results["multimodal"]["message"] = f"Multimodal model ({model}) connected and supports vision"
            except Exception as e:
                test_results["multimodal"]["status"] = "error"
                test_results["multimodal"]["message"] = (
                    f"Multimodal test failed for model ({model}): {str(e)}. "
                    f"Please ensure the model supports vision input (e.g., GPT-4o, Claude 3.5+, Gemini)."
                )
                logger.error(f"Multimodal test failed: {str(e)}")

        # Determine overall status
        all_success = all(result["status"] == "success" for result in test_results.values() if result["status"] != "not_tested")
        any_error = any(result["status"] == "error" for result in test_results.values())

        overall_status = "success" if all_success and not any_error else "error" if any_error else "partial"

        return {
            "status": overall_status,
            "message": "Connection test completed",
            "results": test_results
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LLM connection test failed: {str(e)}")
        return {
            "status": "error",
            "message": f"Test failed: {str(e)}",
            "results": test_results
        }


MASKED_SECRET = "********"


def _prepare_llm_config(llm_config_data: dict):
    """
    Shared preparation for both save and test endpoints.

    1. Pop metadata keys (graphname, scope)
    2. Unmask MASKED_SECRET values using current config from disk
    3. Strip null service values (null = inherit, key should be absent)

    Returns (candidate_config, graphname, scope).
    The candidate_config is save-ready. Top-level parameters (authentication_configuration,
    region_name) are promoted from completion_service if missing and redundant per-service
    copies are stripped. reload_llm_config() and resolve_llm_services() handle injecting
    them back into service configs at runtime.
    """
    graphname = llm_config_data.pop("graphname", None)
    scope = llm_config_data.pop("scope", None)

    # Resolve masked secrets from disk before modifying the payload
    _unmask_auth(llm_config_data, graphname)

    # Strip null values — null means "inherit from base", key should be absent
    for key in list(llm_config_data.keys()):
        if llm_config_data[key] is None:
            del llm_config_data[key]

    # Normalize auth: ensure top-level authentication_configuration exists.
    # If missing, promote from completion_service so future config files
    # always have auth at the top level.
    if "authentication_configuration" not in llm_config_data:
        completion_svc = llm_config_data.get("completion_service")
        if isinstance(completion_svc, dict) and "authentication_configuration" in completion_svc:
            llm_config_data["authentication_configuration"] = completion_svc["authentication_configuration"]

    # Strip per-service auth if identical to top-level (redundant on disk;
    # reload_llm_config injects top-level auth into services on load)
    top_auth = llm_config_data.get("authentication_configuration")
    if top_auth:
        for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
            svc = llm_config_data.get(svc_key)
            if isinstance(svc, dict) and svc.get("authentication_configuration") == top_auth:
                del svc["authentication_configuration"]

    # Normalize region_name: promote from completion_service to top level,
    # strip per-service copies if identical (same pattern as auth).
    if "region_name" not in llm_config_data:
        completion_svc = llm_config_data.get("completion_service")
        if isinstance(completion_svc, dict) and "region_name" in completion_svc:
            llm_config_data["region_name"] = completion_svc["region_name"]

    top_region = llm_config_data.get("region_name")
    if top_region:
        for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
            svc = llm_config_data.get(svc_key)
            if isinstance(svc, dict) and svc.get("region_name") == top_region:
                del svc["region_name"]

    return llm_config_data, graphname, scope



def _mask_secret_values(auth_config: dict) -> dict:
    """Replace all values in an authentication_configuration dict with the masked sentinel."""
    return {k: MASKED_SECRET for k in auth_config}


def _unmask_auth(incoming: dict, graphname: str = None):
    """
    In-place: replace MASKED_SECRET values in incoming authentication_configuration
    with real values resolved through the full config chain via getters.

    Uses get_xxx_config(graphname) which resolves:
      Layer 1 (base) → Layer 2 (global service) → Layer 3 (graph base) → Layer 4 (graph service)
    """
    # Use completion_service as the primary source for top-level auth resolution
    # (backward compat: base bootstraps from completion_service)
    resolved_completion = get_completion_config(graphname)

    # Resolved configs for each service (lazy — only built if needed)
    _resolved_cache = {}
    def _get_resolved(svc_key):
        if svc_key not in _resolved_cache:
            getter = {
                "completion_service": get_completion_config,
                "embedding_service": get_embedding_config,
                "chat_service": get_chat_config,
                "multimodal_service": get_multimodal_config,
            }.get(svc_key)
            if getter:
                result = getter(graphname)
                _resolved_cache[svc_key] = result if result else {}
            else:
                _resolved_cache[svc_key] = {}
        return _resolved_cache[svc_key]

    def _resolve_real_value(key, svc_key=None):
        """Find real value for an auth key using the resolved config chain."""
        # Check the specific service first
        if svc_key:
            resolved = _get_resolved(svc_key)
            val = resolved.get("authentication_configuration", {}).get(key, "")
            if val and val != MASKED_SECRET:
                return val
        # Fallback to completion (which has full base resolution)
        val = resolved_completion.get("authentication_configuration", {}).get(key, "")
        if val and val != MASKED_SECRET:
            return val
        return ""

    # Top-level authentication_configuration
    if "authentication_configuration" in incoming:
        auth = incoming["authentication_configuration"]
        if isinstance(auth, dict):
            for k, v in auth.items():
                if v == MASKED_SECRET:
                    auth[k] = _resolve_real_value(k)

    # Per-service authentication_configuration
    for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
        svc = incoming.get(svc_key)
        if isinstance(svc, dict) and "authentication_configuration" in svc:
            auth = svc["authentication_configuration"]
            if isinstance(auth, dict):
                for k, v in auth.items():
                    if v == MASKED_SECRET:
                        auth[k] = _resolve_real_value(k, svc_key)


def _strip_auth(config: dict) -> dict:
    """Deep copy a config dict and mask all secret values in authentication_configuration sections."""
    result = copy.deepcopy(config)
    if "authentication_configuration" in result and isinstance(result["authentication_configuration"], dict):
        result["authentication_configuration"] = _mask_secret_values(result["authentication_configuration"])
    for service_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
        svc = result.get(service_key)
        if svc and "authentication_configuration" in svc and isinstance(svc["authentication_configuration"], dict):
            svc["authentication_configuration"] = _mask_secret_values(svc["authentication_configuration"])
    return result


@router.get(f"{route_prefix}/config")
async def get_config(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    graphname: str | None = None,
    scope: str | None = None,
):
    """
    Get current server configuration to display in UI.
    Returns config WITHOUT any API keys or secrets.

    Query params:
        scope: "graph" to get per-graph overrides (superadmin only).
               Default (None or "global") returns global config.
    """
    try:
        llm_access_mode = _resolve_llm_config_access(credentials, graphname)
        safe_llm_config = _strip_auth(llm_config)

        if llm_access_mode == "chatbot_only":
            # Load graph-specific chat_service if it exists
            graph_chat_service = None
            if graphname:
                from common.config import _load_graph_llm_config
                graph_llm = _load_graph_llm_config(graphname)
                graph_chat_service = graph_llm.get("chat_service")
                if graph_chat_service:
                    graph_chat_service = copy.deepcopy(graph_chat_service)
                    if "authentication_configuration" in graph_chat_service and isinstance(graph_chat_service["authentication_configuration"], dict):
                        graph_chat_service["authentication_configuration"] = _mask_secret_values(graph_chat_service["authentication_configuration"])

            # Global chat info for "Inherited from" display
            global_chat = get_chat_config()
            global_chat_info = {
                "llm_service": global_chat.get("llm_service", ""),
                "llm_model": global_chat.get("llm_model", ""),
            }

            return {
                "llm_config": safe_llm_config,
                "llm_config_access": "chatbot_only",
                "chatbot_config": graph_chat_service,
                "global_chat_info": global_chat_info,
            }

        # Full access (superadmin/globaldesigner)
        if scope == "graph" and graphname:
            # Return per-graph overrides + global config for reference
            from common.config import _load_graph_config
            graph_cfg = _load_graph_config(graphname)
            graph_llm = graph_cfg.get("llm_config", {})
            # Mask auth in graph overrides
            safe_graph_overrides = {}
            for svc_key in ["completion_service", "chat_service", "embedding_service", "multimodal_service"]:
                svc_override = graph_llm.get(svc_key)
                if svc_override:
                    svc_copy = copy.deepcopy(svc_override)
                    if "authentication_configuration" in svc_copy and isinstance(svc_copy["authentication_configuration"], dict):
                        svc_copy["authentication_configuration"] = _mask_secret_values(svc_copy["authentication_configuration"])
                    safe_graph_overrides[svc_key] = svc_copy

            return {
                "llm_config": safe_llm_config,
                "graph_overrides": safe_graph_overrides,
                "graphrag_config": graphrag_config,
                "graphrag_overrides": graph_cfg.get("graphrag_config", {}),
                "llm_config_access": "full",
                "scope": "graph",
            }

        safe_db_config = copy.deepcopy(db_config)
        if safe_db_config.get("password"):
            safe_db_config["password"] = MASKED_SECRET
        if safe_db_config.get("apiToken"):
            safe_db_config["apiToken"] = MASKED_SECRET

        return {
            "llm_config": safe_llm_config,
            "db_config": safe_db_config,
            "graphrag_config": graphrag_config,
            "llm_config_access": "full",
            "scope": "global",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error returning config: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to return config: {str(e)}")


@router.post(f"{route_prefix}/config/db/test")
async def test_db_connection(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    db_test_config: dict = Body(...)
):
    """
    Test database connection with provided credentials from UI.
    """
    try:
        _require_roles(credentials, {"superuser"})
        # Substitute masked sentinel with stored values
        if db_test_config.get("password") == MASKED_SECRET:
            db_test_config["password"] = db_config.get("password", "")
        if db_test_config.get("apiToken") == MASKED_SECRET:
            db_test_config["apiToken"] = db_config.get("apiToken", "")
        test_conn = TigerGraphConnection(
            host=db_test_config["hostname"],
            username=db_test_config["username"],
            password=db_test_config["password"],
            gsPort=db_test_config["gsPort"],
            restppPort=db_test_config["restppPort"],
            graphname="",
        )
        
        if db_test_config.get("getToken", False):
            test_conn.getToken()

        test_conn.listGraphs()
        
        return {
            "status": "success",
            "message": "Connection successful"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB connection test failed: {str(e)}")
        return {
            "status": "error",
            "message": f"Connection failed: {str(e)}"
        }


@router.post(f"{route_prefix}/config/db")
async def save_db_config(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    db_config_data: dict = Body(...)
):
    """
    Save GraphDB configuration to server_config.json.
    """
    try:
        _require_roles(credentials, {"superuser"})
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = "Basic " + base64.b64encode(
            f"{credentials.username}:{credentials.password}".encode()
        ).decode()
        if _ecc_jobs_running(graphs, auth_header):
            raise HTTPException(
                status_code=409,
                detail="ECC rebuild in progress. Please wait for it to complete before updating config."
            )
        from common.config import reload_db_config
        # Substitute masked sentinel with stored values
        if db_config_data.get("password") == MASKED_SECRET:
            db_config_data["password"] = db_config.get("password", "")
        if db_config_data.get("apiToken") == MASKED_SECRET:
            db_config_data["apiToken"] = db_config.get("apiToken", "")

        result = reload_db_config(db_config_data)
        if result["status"] != "success":
            raise HTTPException(status_code=500, detail=result["message"])
        
        logger.info("GraphDB configuration saved successfully")
        
        return {
            "status": "success",
            "message": "GraphDB configuration saved successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving GraphDB config: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save GraphDB config: {str(e)}")


@router.post(f"{route_prefix}/config/graphrag")
async def save_graphrag_config(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    graphrag_config_data: dict = Body(...)
):
    """
    Save GraphRAG configuration.
    scope=graph saves per-graph overrides; default saves to global config.
    """
    try:
        _require_roles(credentials, {"superuser", "globaldesigner"})
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = "Basic " + base64.b64encode(
            f"{credentials.username}:{credentials.password}".encode()
        ).decode()
        if _ecc_jobs_running(graphs, auth_header):
            raise HTTPException(
                status_code=409,
                detail="ECC rebuild in progress. Please wait for it to complete before updating config."
            )
        from common.config import SERVER_CONFIG, reload_graphrag_config, _config_file_lock

        scope = graphrag_config_data.pop("scope", None)
        graphname = graphrag_config_data.pop("graphname", None)

        if scope == "graph":
            if not graphname:
                raise HTTPException(status_code=400, detail="graphname is required for per-graph config")

            graph_config_dir = f"configs/graph_configs/{graphname}"
            os.makedirs(graph_config_dir, exist_ok=True)
            graph_config_path = os.path.join(graph_config_dir, "server_config.json")

            with _config_file_lock:
                if os.path.exists(graph_config_path):
                    with open(graph_config_path, "r") as f:
                        graph_server_config = json.load(f)
                else:
                    graph_server_config = {}

                if graphrag_config_data:
                    graph_server_config["graphrag_config"] = graphrag_config_data
                else:
                    # Revert to inherit: remove overrides
                    graph_server_config.pop("graphrag_config", None)

                temp_file = f"{graph_config_path}.tmp"
                with open(temp_file, "w") as f:
                    json.dump(graph_server_config, f, indent=2)
                os.replace(temp_file, graph_config_path)

            return {
                "status": "success",
                "message": f"GraphRAG configuration saved for graph {graphname}"
            }
        else:
            # Global save
            with _config_file_lock:
                with open(SERVER_CONFIG, "r") as f:
                    server_config = json.load(f)

                server_config["graphrag_config"] = graphrag_config_data

                temp_file = f"{SERVER_CONFIG}.tmp"
                with open(temp_file, "w") as f:
                    json.dump(server_config, f, indent=2)
                os.replace(temp_file, SERVER_CONFIG)

            # Reload from file (applies defaults for missing keys like chunker/extractor)
            result = reload_graphrag_config()
            if result["status"] != "success":
                raise HTTPException(status_code=500, detail=result["message"])

            return {
                "status": "success",
                "message": "GraphRAG configuration saved successfully"
            }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving GraphRAG config: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save GraphRAG config: {str(e)}")


def split_prompt_template(prompt_content: str, prompt_type: str) -> dict:
    """
    Split prompt into editable content and template variables that users should not modify.
    Returns: {"editable_content": str, "template_variables": str}
    """
    if prompt_type == "chatbot_response":
        pattern = r'(Question: \{question\}.*?)$'
        match = re.search(pattern, prompt_content, re.DOTALL)
        if match:
            template_vars = match.group(1).strip()
            editable = prompt_content[:match.start()].strip()
            return {"editable_content": editable, "template_variables": template_vars}

    elif prompt_type == "query_generation":
        pattern = r'(\{format_instructions\}.*?)$'
        match = re.search(pattern, prompt_content, re.DOTALL)
        if match:
            template_vars = match.group(1).strip()
            editable = prompt_content[:match.start()].strip()
            return {"editable_content": editable, "template_variables": template_vars}

    elif prompt_type == "community_summarization":
        pattern = r'(#######\s*-Data-.*?)$'
        match = re.search(pattern, prompt_content, re.DOTALL)
        if match:
            template_vars = match.group(1).strip()
            editable = prompt_content[:match.start()].strip()
            return {"editable_content": editable, "template_variables": template_vars}

    return {"editable_content": prompt_content, "template_variables": ""}


@router.get(f"{route_prefix}/prompts")
async def get_prompts(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    graphname: str | None = None,
):
    """
    Get all customizable prompts.
    Returns chatbot_response, entity_relationship, community_summarization, and query_generation prompts.
    """
    try:
        access_level = _require_prompt_access(credentials, graphname)
        active_config = get_chat_config(graphname)
        default_prompt_path = active_config.get("prompt_path", "./common/prompts/openai_gpt4/")
        if default_prompt_path.startswith("./"):
            default_prompt_path = default_prompt_path[2:]
        default_prompt_path = default_prompt_path.rstrip("/")

        # Per-graph prompt overrides directory (only contains customized files)
        graph_prompt_dir = f"configs/graph_configs/{graphname}/prompts" if graphname else None

        def _resolve_prompt_file(filename: str) -> str | None:
            """Find prompt file: graph override first, then default."""
            if graph_prompt_dir:
                graph_file = os.path.join(graph_prompt_dir, filename)
                if os.path.exists(graph_file):
                    return graph_file
            default_file = os.path.join(default_prompt_path, filename)
            if os.path.exists(default_file):
                return default_file
            return None

        def _read_prompt(filename: str, prompt_type: str) -> dict:
            filepath = _resolve_prompt_file(filename)
            if filepath:
                with open(filepath, "r", encoding="utf-8") as f:
                    return split_prompt_template(f.read(), prompt_type)
            return {"editable_content": "", "template_variables": ""}

        prompts = {}
        prompts["chatbot_response"] = _read_prompt("chatbot_response.txt", "chatbot_response")
        prompts["entity_relationship"] = _read_prompt("entity_relationship_extraction.txt", "entity_relationship")
        prompts["community_summarization"] = _read_prompt("community_summarization.txt", "community_summarization")

        query_gen = _read_prompt("map_question_to_schema.txt", "query_generation")
        if not query_gen["editable_content"]:
            query_gen = _read_prompt("query_generation.txt", "query_generation")
        prompts["query_generation"] = query_gen

        # Graph-admin (chatbot_only) only sees chatbot_response
        if access_level == "chatbot_only":
            prompts = {"chatbot_response": prompts.get("chatbot_response", {"editable_content": "", "template_variables": ""})}

        return {
            "prompts": prompts,
            "prompt_path": default_prompt_path,
            "configured_provider": active_config.get("llm_service", "openai")
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching prompts: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch prompts: {str(e)}")


@router.post(f"{route_prefix}/prompts")
async def save_prompts(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    prompt_data: dict = Body(...)
):
    """
    Save customized prompts.
    Expects: {
        "prompt_type": "chatbot_response|entity_relationship|community_summarization|query_generation",
        "editable_content": "...",
        "template_variables": "...",
        "graphname": "..."  (optional - graph-admin users must supply this)
    }
    """
    try:
        graphname = prompt_data.get("graphname")
        access_level = _require_prompt_access(credentials, graphname)
        prompt_type = prompt_data.get("prompt_type")

        # Graph-admin (chatbot_only) can only edit chatbot_response prompt
        if access_level == "chatbot_only" and prompt_type != "chatbot_response":
            raise HTTPException(status_code=403, detail="Graph admins can only edit the chatbot response prompt.")
        editable_content = prompt_data.get("editable_content")
        template_variables = prompt_data.get("template_variables", "")

        if not editable_content:
            editable_content = prompt_data.get("content")

        if not prompt_type or not editable_content:
            raise HTTPException(status_code=400, detail="prompt_type and editable_content are required")

        if template_variables:
            content = editable_content + "\n\n" + template_variables
        else:
            content = editable_content

        if graphname:
            # Per-graph: only write the single customized prompt file to the override dir.
            # Non-customized prompts fall back to the global prompt_path at runtime.
            graph_prompt_dir = f"configs/graph_configs/{graphname}/prompts"
            os.makedirs(graph_prompt_dir, exist_ok=True)
            prompt_path = graph_prompt_dir
        else:
            # Global: seed persistent dir from defaults if needed
            default_prompt_path = get_chat_config().get("prompt_path", "./common/prompts/openai_gpt4/")
            if default_prompt_path.startswith("./"):
                default_prompt_path = default_prompt_path[2:]
            default_prompt_path = default_prompt_path.rstrip("/")

            persistent_prompt_dir = "configs/prompts"
            if not default_prompt_path.startswith("configs/"):
                os.makedirs(persistent_prompt_dir, exist_ok=True)
                if os.path.exists(default_prompt_path):
                    for fname in os.listdir(default_prompt_path):
                        src = os.path.join(default_prompt_path, fname)
                        dst = os.path.join(persistent_prompt_dir, fname)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            shutil.copy2(src, dst)
                from common.config import reload_llm_config, _config_file_lock
                with _config_file_lock:
                    with open(SERVER_CONFIG, "r") as f:
                        server_cfg = json.load(f)
                    server_cfg["llm_config"]["completion_service"]["prompt_path"] = f"./{persistent_prompt_dir}/"
                    temp_file = f"{SERVER_CONFIG}.tmp"
                    with open(temp_file, "w") as f:
                        json.dump(server_cfg, f, indent=2)
                    os.replace(temp_file, SERVER_CONFIG)
                reload_llm_config()
                prompt_path = persistent_prompt_dir
            else:
                prompt_path = default_prompt_path

        prompt_type_to_file = {
            "chatbot_response": "chatbot_response.txt",
            "entity_relationship": "entity_relationship_extraction.txt",
            "community_summarization": "community_summarization.txt",
            "query_generation": "map_question_to_schema.txt",
        }

        if prompt_type not in prompt_type_to_file:
            raise HTTPException(status_code=400, detail=f"Invalid prompt_type: {prompt_type}")

        file_path = os.path.join(prompt_path, prompt_type_to_file[prompt_type])
        temp_file = f"{file_path}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_file, file_path)

        messages = {
            "chatbot_response": "Chatbot response prompt saved successfully",
            "entity_relationship": "Entity relationship prompt saved successfully",
            "community_summarization": "Community summarization prompt saved successfully",
            "query_generation": "Schema instructions prompt saved successfully",
        }
        return {"status": "success", "message": messages[prompt_type]}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save prompt: {str(e)}")