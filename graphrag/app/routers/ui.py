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
    Header,
    HTTPException,
    Path,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPBasicCredentials
from pyTigerGraph import TigerGraphConnection
from pyTigerGraph.common.exception import TigerGraphException
from tools.validation_utils import MapQuestionToSchemaException

from common.config import db_config, graphrag_config, embedding_service, llm_config, service_status, get_chat_config, get_completion_config, get_embedding_config, get_multimodal_config, validate_graphname, get_llm_service, resolve_llm_services
from common.db.connections import get_db_connection_pwd_manual
from common.db import schema_utils as schema_utils_mod
from common.db import schema_extraction as schema_extraction_mod
from common.utils.text_extractors import TextExtractor
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics as pmetrics
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.utils.graph_locks import acquire_graph_lock, release_graph_lock, acquire_rebuild_lock, release_rebuild_lock, get_rebuilding_graph, get_current_operation

# Cache the last successful ECC status response per graph so the UI
# still sees started_at and stage when ECC is too busy to respond.
_last_ecc_status_cache: dict = {}
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


def _cleanup_old_traces(max_age_days: int = 30):
    """Delete trace log files older than max_age_days."""
    try:
        cutoff = time.time() - (max_age_days * 86400)
        for filename in os.listdir(TRACE_LOGS_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(TRACE_LOGS_DIR, filename)
            if os.path.getmtime(filepath) < cutoff:
                os.remove(filepath)
    except Exception:
        logger.warning("Failed to clean up old trace logs", exc_info=True)


def _save_trace_log(message_id: str, conversation_id: str, user_query: str, resp: GraphRAGResponse, elapsed: float, username: str):
    try:
        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
            logger.warning("Refusing to save trace log: invalid message_id %r", message_id)
            return
        if not isinstance(username, str) or not username:
            # Without an owner we cannot enforce per-user access on read, so refuse to save.
            logger.warning("Refusing to save trace log for %r: missing username", message_id)
            return

        os.makedirs(TRACE_LOGS_DIR, exist_ok=True)
        base_dir = os.path.abspath(TRACE_LOGS_DIR)
        filepath = os.path.abspath(os.path.join(base_dir, f"{message_id}.json"))
        if os.path.commonpath([base_dir, filepath]) != base_dir:
            logger.warning("Refusing to save trace log: path escapes TRACE_LOGS_DIR for %r", message_id)
            return

        _cleanup_old_traces()

        # Strip chunk text from query_sources to keep trace files small.
        # final_retrieval contains the full text of every retrieved chunk.
        query_sources = dict(resp.query_sources) if resp.query_sources else {}
        result = query_sources.get("result")
        if isinstance(result, dict) and "final_retrieval" in result:
            result = {k: v for k, v in result.items() if k != "final_retrieval"}
            query_sources = {**query_sources, "result": result}

        trace_data = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "username": username,
            "user_query": user_query,
            "response_time": elapsed,
            "response_type": resp.response_type,
            "answered_question": resp.answered_question,
            "query_sources": query_sources,
            "natural_language_response": resp.natural_language_response,
            "timestamp": time.time(),
        }
        with open(filepath, "w") as f:
            json.dump(trace_data, f, default=str)
    except Exception:
        logger.warning(f"Failed to save trace log for message {message_id}", exc_info=True)

# Validated graph name path parameter — rejects path traversal characters
ValidGraphName = Annotated[str, Path(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

use_cypher = os.getenv("USE_CYPHER", "false").lower() == "true"
route_prefix = "/ui"  # APIRouter's prefix doesn't work with the websocket, so it has to be done here
router = APIRouter(tags=["UI"])
llm_config_lock = asyncio.Lock()

# Cache for user role lookups (avoids repeated GSQL calls)
# Key: (username, password_hash) -> (timestamp, (global_roles, graph_roles))
_role_cache: dict[tuple[str, str], tuple[float, tuple[list[str], dict[str, list[str]]]]] = {}
_role_cache_lock = threading.Lock()
# Role changes (granting/revoking TG roles) are infrequent operator
# actions, and the cache key already includes a password hash so credential
# changes are picked up immediately. Match the UI idle timeout (1 hour) —
# past that point the user gets logged out anyway and the next sign-in
# refreshes roles. Increase if your operator workflows can wait longer.
_ROLE_CACHE_TTL = 60 * 60  # seconds (1 hour)

def _normalize_roles(raw_roles: str) -> list[str]:
    cleaned = re.sub(r"[\[\]]", "", raw_roles).strip()
    if not cleaned or cleaned.lower() == "none":
        return []
    return [r.strip().lower() for r in re.split(r"[,\s]+", cleaned) if r.strip()]


def _parse_user_roles_detail(user_info: str) -> tuple[list[str], dict[str, list[str]], str]:
    """Single-pass parser for ``SHOW USER`` output. Returns
    ``(global_roles, graph_roles, current_user)`` where ``current_user``
    is the username flagged by TG's ``*`` marker (the effective user
    for the session that ran the call). Roles are extracted only from
    that ``*``-marked block.

    Returning the resolved user lets callers handle the case where the
    login name was a sentinel like ``__GSQL__secret`` and the real
    identity is whoever the secret belongs to.
    """
    global_roles: list[str] = []
    graph_roles: dict[str, list[str]] = {}
    current_user = ""
    is_user_section = False
    for line in user_info.splitlines():
        line_stripped = line.lstrip()
        # Capture the leading marker (``*`` for current user, ``-`` for
        # the other users, possibly absent on a header) so we can pick
        # the right block.
        match = re.match(
            r"^([\*\-])?\s*-?\s*(?:Name|User Name|User)\s*:\s*(.+)$",
            line_stripped,
            re.IGNORECASE,
        )
        if match:
            marker = match.group(1)
            name = match.group(2).strip()
            if marker == "*":
                current_user = name
                is_user_section = True
            else:
                is_user_section = False
            continue
        if not is_user_section:
            continue

        line_stripped = line_stripped.strip()
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

    return global_roles, graph_roles, current_user


def _parse_user_roles(user_info: str, username: str = "") -> list[str]:
    # ``username`` kept for back-compat; the parser now resolves the
    # active user from SHOW USER's ``*`` marker.
    global_roles, _, _ = _parse_user_roles_detail(user_info)
    return global_roles

def _get_user_role_details(
    username: str, password: str
) -> tuple[list[str], dict[str, list[str]], str]:
    """Get user roles + resolved username with a short TTL cache.

    Returns ``(global_roles, graph_roles, resolved_username)`` where
    ``resolved_username`` is the user TG marks as current in ``SHOW
    USER`` output. For sentinel logins (e.g. ``__GSQL__secret``) this
    is the secret's owner; for classic user/password logins it matches
    the input.
    """
    # Use the full SHA-256 hex (64 chars). Token logins share the
    # ``_UI_TOKEN_SENTINEL`` username, so the hash is the only thing
    # distinguishing one token from another in the cache key — a
    # truncated hash would let two distinct tokens whose hash prefixes
    # collide serve each other's cached roles.
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    cache_key = (username, pwd_hash)
    now = time.time()

    with _role_cache_lock:
        cached = _role_cache.get(cache_key)
        if cached and (now - cached[0]) < _ROLE_CACHE_TTL:
            return cached[1]

    # Mirror the auth() dispatch — API-token logins build the
    # connection with ``apiToken``; secret logins
    # (``__GSQL__secret``) and classic user/password both go through
    # the username/password slots (pyTigerGraph routes the secret
    # case natively).
    connection_kwargs = {
        "host": db_config.get("hostname"),
        "graphname": "",
    }
    if db_config.get("gsPort") is not None:
        connection_kwargs["gsPort"] = db_config["gsPort"]
    if db_config.get("restppPort") is not None:
        connection_kwargs["restppPort"] = db_config["restppPort"]

    if username == _UI_TOKEN_SENTINEL:
        conn = TigerGraphConnection(
            **connection_kwargs,
            apiToken=password,
        )
    else:
        conn = TigerGraphConnection(
            **connection_kwargs,
            username=username,
            password=password,
        )

    # Transient GSQL hiccups when the role-cache TTL expires were
    # surfacing as 403 "Unable to verify user roles" banners on the
    # config pages. Retry once with a short backoff before giving up —
    # the next attempt usually succeeds when the blip is over.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            user_info = conn.gsql("SHOW USER")
            roles, graph_roles, resolved = _parse_user_roles_detail(user_info)
            if not resolved:
                resolved = username
            result = (roles, graph_roles, resolved)
            with _role_cache_lock:
                _role_cache[cache_key] = (now, result)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


def _get_user_roles(username: str, password: str) -> list[str]:
    global_roles, _, _ = _get_user_role_details(username, password)
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
        global_roles, graph_roles, _ = _get_user_role_details(credentials.username, credentials.password)
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
        global_roles, graph_roles, _ = _get_user_role_details(
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


_UI_TOKEN_SENTINEL = "__graphrag_token__"


def _parse_auth_header(authorization: str | None) -> HTTPBasicCredentials:
    """Parse an ``Authorization`` header value into ``HTTPBasicCredentials``.

    ``Basic <b64>`` decodes to the real username/password pair.
    ``Bearer <token>`` is mapped to a synthetic
    ``(_UI_TOKEN_SENTINEL, token)`` pair so downstream code that already
    dispatches on the sentinel for API-token logins keeps working
    unchanged.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        scheme, _, value = authorization.partition(" ")
    except Exception:
        scheme, value = "", ""
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "basic" and value:
        try:
            decoded = base64.b64decode(value).decode()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed Basic credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        # RFC 7617: Basic payload MUST be ``user-id ":" password``. Reject
        # payloads with no colon outright — partition silently produces an
        # empty password otherwise, which would turn a malformed header
        # into an empty-password login attempt.
        if ":" not in decoded:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed Basic credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        username, _, password = decoded.partition(":")
        return HTTPBasicCredentials(username=username, password=password)
    if scheme == "bearer" and value:
        return HTTPBasicCredentials(username=_UI_TOKEN_SENTINEL, password=value)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unsupported Authorization scheme",
        headers={"WWW-Authenticate": "Basic"},
    )


def _chat_history_auth_header(creds: HTTPBasicCredentials) -> str:
    """Build the Basic-auth header used when proxying to chat-history.

    Chat-history identifies the caller by the Basic-auth username only
    (it ignores the password). For sentinel logins
    (``__graphrag_token__`` / ``__GSQL__secret``) we substitute the
    TG-resolved username so conversations get stored / fetched under
    the user's real identity instead of the sentinel string.
    """
    try:
        _, _, resolved = _get_user_role_details(creds.username, creds.password)
    except Exception:
        resolved = creds.username
    username = resolved or creds.username
    encoded = base64.b64encode(f"{username}:{creds.password}".encode()).decode()
    return f"Basic {encoded}"


def _ecc_auth_header(creds: HTTPBasicCredentials) -> str:
    """Build the Authorization header used when forwarding to ECC.

    API-token logins arrive as the ``__graphrag_token__`` sentinel;
    forward them as ``Bearer <token>`` since ECC connects with the
    token directly. Classic user/password and ``__GSQL__secret`` logins
    forward as Basic, which ECC / pyTigerGraph handle natively.
    """
    if creds.username == _UI_TOKEN_SENTINEL:
        return f"Bearer {creds.password}"
    encoded = base64.b64encode(
        f"{creds.username}:{creds.password}".encode()
    ).decode()
    return f"Basic {encoded}"


def auth(usr: str, password: str, conn=None) -> tuple[list[str], TigerGraphConnection]:
    if conn is None:
        # Three Basic-auth shapes share the wire:
        #   * regular ``user:password`` → classic mode
        #   * ``__graphrag_token__:<jwt>`` → API token mode; pass the
        #     token to pyTigerGraph as ``apiToken``
        #   * ``__GSQL__secret:<secret>`` → TigerGraph's native secret
        #     convention; pyTigerGraph already understands it when sent
        #     as plain username/password, so no special handling here.
        connection_kwargs = {
            "host": db_config["hostname"],
            "graphname": "",
        }
        if db_config.get("gsPort") is not None:
            connection_kwargs["gsPort"] = db_config["gsPort"]
        if db_config.get("restppPort") is not None:
            connection_kwargs["restppPort"] = db_config["restppPort"]

        if usr == _UI_TOKEN_SENTINEL:
            conn = TigerGraphConnection(
                **connection_kwargs,
                apiToken=password,
            )
        else:
            conn = TigerGraphConnection(
                **connection_kwargs,
                username=usr,
                password=password,
            )

    try:
        graph_list = conn.listGraphs()
        graphs = [g["graphName"] for g in graph_list if "graphName" in g]

    except requests.exceptions.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    except TigerGraphException as e:
        # pyTigerGraph wraps auth rejections as a TigerGraphException
        # ("Authentication failed.", ...) rather than HTTPError. Convert
        # that class explicitly so the client sees a clean 401, not a
        # generic 500.
        msg = (str(e.args[0]) if e.args else str(e)).lower()
        if "authentic" in msg or "token" in msg or "password" in msg:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )
        raise
    except Exception as e:
        raise e
    return graphs, conn


def ws_basic_auth(auth_info: str, graphname=None):
    """Authenticate a WebSocket / internal call from a raw Authorization
    header value (``Basic <b64>`` or ``Bearer <token>``).
    """
    creds = _parse_auth_header(auth_info)
    if creds.username == _UI_TOKEN_SENTINEL:
        # API-token logins: build a TG connection directly with the
        # token; ``get_db_connection_pwd_manual`` only handles
        # username/password. Mirror the customizeHeader + Proxy wrap
        # used by the password path so downstream code that depends on
        # proxy-only attributes (e.g. version checks) works the same
        # for token logins.
        raw_conn = TigerGraphConnection(
            host=db_config["hostname"],
            graphname=graphname or "",
            apiToken=creds.password,
            restppPort=db_config.get("restppPort", "9000"),
            gsPort=db_config.get("gsPort", "14240"),
        )
        raw_conn.customizeHeader(
            timeout=db_config.get("default_timeout", 60) * 1000,
            responseSize=5000000,
        )
        conn = TigerGraphConnectionProxy(raw_conn, auth_mode="token")
    else:
        conn = get_db_connection_pwd_manual(
            graphname, creds.username, creds.password
        )
    return auth(creds.username, creds.password, conn)


def ui_creds(
    authorization: Annotated[str | None, Header()] = None,
) -> HTTPBasicCredentials:
    """Parse ``Authorization`` (Basic or Bearer) into
    ``HTTPBasicCredentials`` without contacting TigerGraph. Used by
    endpoints that only need the caller's identity.
    """
    return _parse_auth_header(authorization)


def ui_basic_auth(
    creds: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
) -> tuple[list[str], HTTPBasicCredentials]:
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
    # Fetch roles + resolved username at login so the frontend doesn't
    # need separate /roles or /whoami calls. ``resolved`` differs from
    # ``creds.username`` only when the caller logged in via a sentinel
    # (e.g. ``__GSQL__secret``), in which case ``resolved`` is the
    # user the secret belongs to.
    try:
        global_roles, graph_roles, resolved = _get_user_role_details(
            creds.username, creds.password
        )
    except Exception as e:
        logger.warning(f"Failed to fetch roles at login: {e}")
        global_roles, graph_roles, resolved = [], {}, creds.username
    return {
        "graphs": graphs,
        "roles": global_roles,
        "graph_roles": graph_roles,
        "username": resolved or creds.username,
    }


def _read_local_version(component: str) -> dict:
    """Read the ``/code/VERSION`` (repo-root file copied into image)
    and ``/code/BUILD_DATE`` (stamped at image build time).
    """
    def _safe_read(path: str) -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    return {
        "component": component,
        "version": _safe_read("/code/VERSION"),
        "build_date": _safe_read("/code/BUILD_DATE"),
    }


def _unknown_version(component: str) -> dict:
    return {"component": component, "version": "unknown", "build_date": "unknown"}


def _coerce_version_payload(payload, component: str) -> dict:
    """Return a payload shaped like ``_unknown_version`` regardless of
    what a remote ``/version`` endpoint actually sent. A malformed or
    compromised response (non-dict, missing keys, non-string values)
    falls back to the unknown shape so clients always see the same
    schema.
    """
    if not isinstance(payload, dict):
        return _unknown_version(component)
    result = _unknown_version(component)
    for key in ("component", "version", "build_date"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


@router.get(f"{route_prefix}/version")
def get_version():
    """Return image-build version info for all running components.

    The graphrag container reads its own ``/code/VERSION`` directly;
    ``ecc`` and ``graphrag-ui`` are fetched over the network so this
    one call surfaces every component a UI client cares about.
    Unreachable components return ``unknown`` rather than failing the
    whole call.
    """
    graphrag_version = _read_local_version("graphrag")

    ecc_base = graphrag_config.get("ecc", "http://graphrag-ecc:8001")
    try:
        ecc_resp = httpx.get(f"{ecc_base}/version", timeout=5.0)
        if ecc_resp.status_code == 200:
            ecc_version = _coerce_version_payload(ecc_resp.json(), "graphrag-ecc")
        else:
            ecc_version = _unknown_version("graphrag-ecc")
    except Exception:
        ecc_version = _unknown_version("graphrag-ecc")

    ui_version = _unknown_version("graphrag-ui")
    try:
        # ``serve`` exposes static files at port 3000 inside the
        # compose network; fall through quietly if it isn't reachable
        # (e.g. running graphrag in isolation).
        ui_resp = httpx.get("http://graphrag-ui:3000/version.json", timeout=5.0)
        if ui_resp.status_code == 200:
            ui_version = _coerce_version_payload(ui_resp.json(), "graphrag-ui")
    except Exception:
        pass

    return {
        "graphrag": graphrag_version,
        "graphrag_ecc": ecc_version,
        "graphrag_ui": ui_version,
    }


@router.get(f"{route_prefix}/admin/embedding_store_status")
def embedding_store_status(
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Return the current vector-store status without re-running init.
    Used by the Graph Database Config page to poll status; only routed
    through nginx's ``/ui/`` path so the UI can reach it.
    """
    _require_roles(creds[1], {"superuser", "globaldesigner"})
    return service_status["embedding_store"]


@router.post(f"{route_prefix}/admin/retry_embedding_store")
def retry_embedding_store_now(
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Re-run the embedding-store init right now. Use after fixing the
    underlying issue (e.g. TigerGraph just came back up) instead of
    waiting for the background retry loop to wake.

    Restricted to superuser / globaldesigner — the call holds the
    request thread while the init runs (typically <1s when TG is
    reachable, longer if it isn't).
    """
    _require_roles(creds[1], {"superuser", "globaldesigner"})
    from common.config import _init_embedding_store, _embedding_store_ready
    _embedding_store_ready.clear()
    _init_embedding_store()
    return service_status["embedding_store"]


@router.get(f"{route_prefix}/list_graphs")
def list_graphs(auth: Annotated[list[str], Depends(ui_basic_auth)]):
    """Return the live list of graphs the authenticated user has access
    to. UI clients call this on mount to refresh their cached graph
    list, so a graph created or initialized after login (or during a
    session where the init request failed client-side but succeeded
    server-side) becomes visible without re-login.
    """
    return {"graphs": auth[0]}


@router.get(f"{route_prefix}/schema_reserved_names")
def schema_reserved_names(
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Return name sets the UI uses to reject suggested types up-front
    in the Initialize Graph dialog. The downstream parser silently
    drops these anyway, but inline rejection gives the user a clear
    reason instead of a confusing "type didn't appear in the draft".

    Returns three lists:
      * ``gsql_keywords``           — GSQL reserved words (sourced from
        pyTigerGraph). Naming a vertex/edge type after one would crash
        the schema-change job.
      * ``structural_vertex_types`` — GraphRAG always-present vertex
        types (Document, DocumentChunk, Entity, ...).
      * ``structural_edge_types``   — GraphRAG always-present edge
        types (HAS_CONTENT, CONTAINS_ENTITY, ...).
    """
    return {
        "gsql_keywords": sorted(schema_utils_mod.get_gsql_reserved_words()),
        "structural_vertex_types": sorted(
            schema_utils_mod.GRAPHRAG_STRUCTURAL_VERTEX_TYPES
        ),
        "structural_edge_types": sorted(
            schema_utils_mod.GRAPHRAG_STRUCTURAL_EDGE_TYPES
        ),
    }


@router.post(f"{route_prefix}/feedback")
def add_feedback(
    message: Message,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    try:
        res = httpx.post(
            f"{graphrag_config['chat_history_api']}/conversation",
            json=message.model_dump(),
            headers={"Authorization": _chat_history_auth_header(creds)},
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
    # Trace logs contain user queries (potentially PII), full LLM responses,
    # internal cypher, schema mappings, and per-call cost.
    # Two layers of access control:
    #   1. Role: must be a superuser.
    #   2. Ownership: must be the user who originated the trace.
    # This prevents cross-user disclosure even between superusers.
    _require_roles(creds[1], {"superuser"})

    if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
        raise HTTPException(status_code=400, detail="Invalid message_id")
    base_dir = os.path.abspath(TRACE_LOGS_DIR)
    filepath = os.path.abspath(os.path.join(base_dir, f"{message_id}.json"))
    if os.path.commonpath([base_dir, filepath]) != base_dir:
        raise HTTPException(status_code=400, detail="Invalid message_id")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Trace log not found")

    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read trace log %r", message_id, exc_info=True)
        raise HTTPException(status_code=404, detail="Trace log not found")

    # Per-user segregation. Legacy files (saved before this fix) have no
    # "username" field and therefore can't pass this check — they will 404
    # for everyone and age out via the existing 30-day cleanup.
    # Compare against the TG-resolved username so sentinel logins (e.g.
    # ``__GSQL__secret``) can still read their own traces.
    owner = data.get("username")
    try:
        _, _, resolved = _get_user_role_details(creds[1].username, creds[1].password)
    except Exception:
        resolved = creds[1].username
    if owner != (resolved or creds[1].username):
        logger.warning(
            "User %r (resolved=%r) attempted to read trace owned by %r (message_id=%s)",
            creds[1].username, resolved, owner, message_id,
        )
        raise HTTPException(status_code=404, detail="Trace log not found")

    return data



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
        auth = "Basic " + base64.b64encode(
            f"{creds.username}:{creds.password}".encode()
        ).decode()
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


# Per-graph init state store. Init runs as a BackgroundTask so the
# HTTP request returns immediately; clients poll /initialize_status
# for progress / completion. Avoids browser timeouts on long inits
# (TG schema-change + retriever installs can run for 10+ minutes).
_init_state: dict[str, dict] = {}
_init_state_lock = threading.Lock()


def _set_init_state(graphname: str, **fields) -> None:
    with _init_state_lock:
        cur = _init_state.get(graphname, {})
        cur.update(fields)
        _init_state[graphname] = cur


def _get_init_state(graphname: str) -> dict:
    with _init_state_lock:
        return dict(_init_state.get(graphname, {"state": "unknown"}))


def _try_reserve_init(graphname: str) -> str | None:
    """Atomically transition the graph into the ``queued`` state. Returns
    ``None`` on success; returns the existing state string when another
    request has already reserved or is running the init. Combines the
    in-progress check and the queued-state set under the same lock so
    concurrent ``POST /initialize_graph`` requests for the same graph
    can't both pass the gate and enqueue duplicate background jobs.
    """
    with _init_state_lock:
        cur = _init_state.get(graphname, {"state": "unknown"})
        if cur.get("state") in {"queued", "running"}:
            return cur.get("state")
        _init_state[graphname] = {
            "state": "queued",
            "message": "Initialization queued",
            "started_at": time.time(),
            "completed_at": None,
            "result": None,
            "error": None,
        }
        return None


def _build_proposal_from_live_schema(
    conn,
    vertex_descriptions: dict | None = None,
    edge_descriptions: dict | None = None,
):
    """Build a :class:`SchemaProposal` from the graph's current
    user-defined vertex/edge types, suitable for feeding into
    :func:`apply_proposal` on the ``use_existing_schema`` path.

    The proposal carries names + edge pairs and optionally
    user-supplied descriptions (collected by the Precheck dialog or
    seeded by the suggest-description LLM call). Diff against the live
    schema is a no-op; ``apply_proposal`` only installs retrievers and
    writes type metadata.
    """
    from common.db.schema_utils import (
        EdgeProposal,
        GRAPHRAG_STRUCTURAL_EDGE_TYPES,
        GRAPHRAG_STRUCTURAL_VERTEX_TYPES,
        SchemaProposal,
        VertexProposal,
        read_existing_schema,
    )
    structural_v = {t.casefold() for t in GRAPHRAG_STRUCTURAL_VERTEX_TYPES}
    structural_e = {t.casefold() for t in GRAPHRAG_STRUCTURAL_EDGE_TYPES}

    vd = vertex_descriptions or {}
    ed = edge_descriptions or {}

    existing = read_existing_schema(conn)
    vertices = [
        VertexProposal(name=v, description=(vd.get(v) or "").strip())
        for v in sorted(existing.vertex_types)
        if v.casefold() not in structural_v
    ]
    edges: list[EdgeProposal] = []
    for et, pairs in existing.edge_pairs.items():
        folded = et.casefold()
        if folded in structural_e or folded.startswith("reverse_"):
            continue
        edges.append(
            EdgeProposal(
                name=et,
                pairs=list(pairs),
                directed=et in existing.directed_edges,
                description=(ed.get(et) or "").strip(),
            )
        )
    return SchemaProposal(vertices=vertices, edges=edges)


def _check_init_eligibility(auth_header: str, graphname: str) -> dict:
    """Introspect *graphname* and categorize its current schema state.

    Returns a dict with key ``state`` set to one of:

    * ``"empty"`` — graph has no schema, or none of its existing types
      are GraphRAG structural or user-defined. Safe to initialize from
      scratch.
    * ``"structural_present"`` — graph already has one or more
      GraphRAG structural vertex/edge types. Caller must reject.
      ``structural_types`` lists the offending names.
    * ``"user_types_present"`` — graph has user-defined vertex/edge
      types (none structural). Lists in ``user_vertex_types`` and
      ``user_edge_types``. Caller decides whether to reject or adopt.

    Graphs that don't yet exist in TigerGraph behave like ``empty`` —
    ``getVertexTypes`` raises or returns empty for missing graphs, which
    we treat as "no schema yet".
    """
    from common.db.schema_utils import (
        GRAPHRAG_STRUCTURAL_VERTEX_TYPES,
        GRAPHRAG_STRUCTURAL_EDGE_TYPES,
    )
    structural_v = {t.casefold() for t in GRAPHRAG_STRUCTURAL_VERTEX_TYPES}
    structural_e = {t.casefold() for t in GRAPHRAG_STRUCTURAL_EDGE_TYPES}

    try:
        _, conn = ws_basic_auth(auth_header, graphname)
    except Exception:
        # Graph doesn't exist (or auth failed mid-flight); treat as empty
        # so the create_graph + init path handles it.
        return {"state": "empty"}

    try:
        vertex_types = list(conn.getVertexTypes() or [])
        edge_types = list(conn.getEdgeTypes() or [])
    except Exception:
        return {"state": "empty"}

    structural_hits: list[str] = []
    user_vts: list[str] = []
    for vt in vertex_types:
        if vt.casefold() in structural_v:
            structural_hits.append(vt)
        else:
            user_vts.append(vt)
    user_edges: list[str] = []
    for et in edge_types:
        folded = et.casefold()
        if folded in structural_e or folded.startswith("reverse_"):
            structural_hits.append(et)
        else:
            user_edges.append(et)

    if structural_hits:
        return {
            "state": "structural_present",
            "structural_types": sorted(set(structural_hits)),
        }
    if user_vts or user_edges:
        return {
            "state": "user_types_present",
            "user_vertex_types": sorted(user_vts),
            "user_edge_types": sorted(user_edges),
        }
    return {"state": "empty"}


@router.get(route_prefix + "/{graphname}/check_init_eligibility")
def check_init_eligibility(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Introspect *graphname* and return an init-eligibility verdict.

    Used by the Initialize Knowledge Graph dialog's *Precheck* button to
    surface the same categorization that ``POST /initialize_graph`` runs
    internally, without starting an init job.

    Response::

        {
          "state": "empty" | "user_types_present" | "structural_present",
          "structural_types": [...],          # present when state=structural_present
          "user_vertex_types": [...],         # present when state=user_types_present
          "user_edge_types": [...],           # present when state=user_types_present
          "user_edge_pairs": {edge: [[from, to], ...]}  # for description hints
        }
    """
    cred_obj = creds[1]
    auth_header = "Basic " + base64.b64encode(
        f"{cred_obj.username}:{cred_obj.password}".encode()
    ).decode()
    result = _check_init_eligibility(auth_header, graphname)
    # Include edge endpoint pairs so the UI can show "FILED_BY (Filing → Company)"
    # alongside each edge name in the description-edit dialog.
    if result.get("state") == "user_types_present" and result.get("user_edge_types"):
        try:
            _, conn = ws_basic_auth(auth_header, graphname)
            from common.db.schema_utils import read_existing_schema
            existing = read_existing_schema(conn)
            pairs_map: dict[str, list[list[str]]] = {}
            for et in result["user_edge_types"]:
                pairs = existing.edge_pairs.get(et, set())
                pairs_map[et] = [[s, t] for s, t in sorted(pairs)]
            result["user_edge_pairs"] = pairs_map
        except Exception:
            result["user_edge_pairs"] = {}
    return result


@router.post(route_prefix + "/{graphname}/suggest_type_descriptions")
def suggest_type_descriptions(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    payload: Annotated[dict, Body(...)],
):
    """Call the completion LLM to suggest one-sentence descriptions for a
    set of vertex/edge type names.

    Request body::

        {
          "vertex_types": ["Company", "Person", ...],
          "edge_types":   [{"name": "FILED_BY", "from": "Filing", "to": "Company"}, ...]
        }

    Response::

        {
          "vertex_descriptions": {"Company": "...", "Person": "...", ...},
          "edge_descriptions":   {"FILED_BY": "...", ...}
        }

    Best-effort: on any LLM failure, the corresponding keys are empty
    strings so the dialog can still render an editable form.
    """
    from langchain_core.prompts import PromptTemplate
    from langchain_core.output_parsers import JsonOutputParser

    vertex_types = [
        str(v) for v in (payload.get("vertex_types") or [])
        if isinstance(v, str) and v
    ]
    edge_items = payload.get("edge_types") or []
    edges_brief: list[str] = []
    for e in edge_items:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        f = e.get("from") or ""
        t = e.get("to") or ""
        if not name:
            continue
        if f and t:
            edges_brief.append(f"{name} (FROM {f}, TO {t})")
        else:
            edges_brief.append(str(name))

    if not vertex_types and not edges_brief:
        return {"vertex_descriptions": {}, "edge_descriptions": {}}

    llm_service = get_llm_service(get_completion_config(graphname))
    prompt = PromptTemplate.from_template(
        "Given the following graph-schema type names from a domain knowledge "
        "graph, write a concise one-sentence description for each. Use plain "
        "English; describe what the type represents, not its attributes.\n\n"
        "Vertex types: {vertex_types}\n"
        "Edge types: {edge_types}\n\n"
        "Return JSON with this exact shape:\n"
        "{{\"vertex_descriptions\": {{\"<name>\": \"<one sentence>\"}}, "
        "\"edge_descriptions\": {{\"<name>\": \"<one sentence>\"}}}}\n"
    )
    try:
        parsed = llm_service.invoke_with_parser(
            prompt,
            JsonOutputParser(),
            {
                "vertex_types": ", ".join(vertex_types) or "(none)",
                "edge_types": ", ".join(edges_brief) or "(none)",
            },
            caller_name="suggest_type_descriptions",
        )
    except Exception as exc:
        LogWriter.warning(
            f"suggest_type_descriptions LLM call failed for {graphname}: {exc}"
        )
        return {
            "vertex_descriptions": {v: "" for v in vertex_types},
            "edge_descriptions": {
                (e.get("name") if isinstance(e, dict) else ""): ""
                for e in edge_items
            },
        }

    vds = parsed.get("vertex_descriptions") if isinstance(parsed, dict) else {}
    eds = parsed.get("edge_descriptions") if isinstance(parsed, dict) else {}
    return {
        "vertex_descriptions": {
            v: (vds.get(v) or "").strip() if isinstance(vds, dict) else ""
            for v in vertex_types
        },
        "edge_descriptions": {
            (e.get("name") if isinstance(e, dict) else ""): (
                (eds.get(e.get("name")) or "").strip()
                if isinstance(eds, dict) and isinstance(e, dict)
                else ""
            )
            for e in edge_items
        },
    }


def _get_domain_schema_for_render(conn, graphname: str):
    """Snapshot the live domain schema for templated-retriever rendering.

    Returns ``(domain_vts, domain_edges, include_entity)`` sorted for
    deterministic rendering. Returns empty lists on any failure — the
    caller falls back to comparing the unrendered template (acceptable
    for graphs without dynamic schema).
    """
    try:
        from common.db.schema_utils import read_existing_schema, is_structural_type
        snapshot = read_existing_schema(conn)
        domain_vts = sorted(
            v for v in snapshot.vertex_types if not is_structural_type(v)
        )
        domain_edges = sorted(
            e for e in snapshot.edge_pairs.keys()
            if not is_structural_type(e) and not e.startswith("reverse_")
        )
        # Always include Entity in the community-walk start set, matching
        # what install_retrievers does at init.
        return domain_vts, domain_edges, True
    except Exception as e:
        logger.warning(
            f"_get_domain_schema_for_render({graphname}) failed: {e}; "
            f"templated retrievers will be checked against unrendered template"
        )
        return [], [], False


def _local_query_hash(q_path: str, q_name: str,
                     domain_vts: list, domain_edges: list, include_entity: bool):
    """Return the normalized hash for the LOCAL query body.

    For templated retrievers, render the template with the graph's live
    domain VTs / edges first — otherwise the local template (no domain
    types injected) won't match the installed body (which has them
    baked in at install time).
    """
    from common.db.migrate import _gsql_hash, _read_local_query
    from common.db.retriever_render import TEMPLATED_RETRIEVERS, render_retriever_body

    body = _read_local_query(q_path)
    if body is None:
        return None
    if q_name in TEMPLATED_RETRIEVERS:
        body = render_retriever_body(
            body,
            domain_vts=domain_vts,
            domain_edges=domain_edges,
            include_entity=include_entity,
        )
    return _gsql_hash(body)


# The queries the migration assistant checks for a GraphRAG graph come from the
# shared canonical list (common.db.query_sets) so they can't drift from what the
# ECC rebuild and SupportAI init actually install. The opt-in ECC-checker
# queries are intentionally excluded — the checker is off by default, so those
# queries aren't part of what a graph normally needs. Loading-job and
# schema-change .gsql files aren't queries and are tracked separately by
# ``common.db.migrate.check_and_apply_schema``.
from common.db.query_sets import MIGRATION_QUERIES, with_gsql
from common.db.query_errors import (
    concise_gsql_error,
    create_response_error,
    http_error_response_body,
)
from common.db.schema_utils import gsql_output_error
_MIGRATION_QUERY_PATHS = with_gsql(MIGRATION_QUERIES)


@router.get(route_prefix + "/{graphname}/migration/status")
def migration_status(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Compatibility check for an existing graph. Reports each shipped
    GSQL query as ``up_to_date``, ``outdated`` (installed but body
    drifted from local), or ``not_installed`` (query expected by the
    current release but absent on TG).

    Read-only — does NOT modify the graph. Pair with
    ``POST /migration/apply`` to actually repair.

    Schema-attribute drift is reported as ``{}`` for now; the detection
    is stubbed in ``common.db.migrate.check_and_apply_schema``.
    """
    from common.db.migrate import _gsql_hash, get_installed_query_names, get_installed_query_body

    import os.path

    cred_obj = creds[1]
    conn = get_db_connection_pwd_manual(graphname, cred_obj.username, cred_obj.password)

    # Read live domain schema once for templated-retriever rendering.
    domain_vts, domain_edges, include_entity = _get_domain_schema_for_render(conn, graphname)

    outdated: list[str] = []
    up_to_date: list[str] = []
    not_installed: list[str] = []
    missing_files: list[str] = []

    # Install state (authoritative) comes from the installed-query endpoints in
    # one batched call; a query absent here needs installing. For the installed
    # ones, the body is read via the query API (getQueryContent) and compared to
    # local to detect drift.
    installed_names = get_installed_query_names(conn, graphname)
    for q_path in _MIGRATION_QUERY_PATHS:
        if not os.path.exists(q_path):
            missing_files.append(q_path)
            continue
        q_name = os.path.splitext(os.path.basename(q_path))[0]
        local_hash = _local_query_hash(
            q_path, q_name, domain_vts, domain_edges, include_entity
        )
        if local_hash is None:
            missing_files.append(q_path)
            continue
        if q_name not in installed_names:
            not_installed.append(q_name)
            continue
        try:
            installed_body = get_installed_query_body(conn, graphname, q_name)
        except Exception as e:
            logger.warning(f"migration_status: reading query {q_name} failed: {e}")
            not_installed.append(q_name)
            continue
        if not installed_body:
            # Installed endpoint but no readable body — treat as needing repair.
            not_installed.append(q_name)
            continue
        if _gsql_hash(installed_body) != local_hash:
            outdated.append(q_name)
        else:
            up_to_date.append(q_name)

    # Prompt-override compatibility: DETERMINISTIC, LOCAL checks only. This
    # endpoint is user-triggered and must stay fast, cheap, and side-effect
    # free, so it performs NO LLM calls. For each split-prompt override present
    # for this graph, report (a) a legacy full-prompt override that will be
    # ignored at runtime, and (b) placeholder tokens that get stripped on save.
    # The LLM-based system-rule conflict review is intentionally not run here
    # (slow, costly, quota-sensitive, nondeterministic); it runs at the explicit
    # prompt-save path, where a single edited prompt is reviewed on demand.
    # Best-effort, never fatal.
    prompt_issues: dict = {}
    try:
        from common.utils.prompt_validation import find_placeholders
        from common.llm_services.base_llm import LLM_Model

        review_svc = get_llm_service(get_chat_config(graphname))
        graph_prompt_dir = os.path.join(
            "configs", "graph_configs", graphname, "prompts"
        )
        for fname in LLM_Model._SPLIT_PROMPT_SPEC:
            p = os.path.join(graph_prompt_dir, fname)
            if not os.path.exists(p):
                continue
            try:
                raw = open(p, encoding="utf-8").read()
            except Exception:
                continue
            sys_attr, _ = LLM_Model._SPLIT_PROMPT_SPEC[fname]
            if review_svc._is_legacy_full_prompt(raw, getattr(review_svc, sys_attr)):
                prompt_issues[fname] = {"legacy_full_prompt": True}
                continue
            placeholders = find_placeholders(raw)
            if placeholders:
                prompt_issues[fname] = {"removed_placeholders": placeholders}
    except Exception as e:
        logger.warning(f"migration_status prompt check failed: {e}")

    return {
        "graphname": graphname,
        "queries": {
            "outdated": outdated,
            "up_to_date": up_to_date,
            "not_installed": not_installed,
            "missing_files": missing_files,
        },
        "schema": {
            # Populated from common.db.migrate.check_and_apply_schema once
            # attribute additions are tracked; empty until then.
            "missing_attributes": {},
            "schema_change_required": False,
        },
        "prompts": prompt_issues,
        "needs_repair": bool(outdated) or bool(not_installed) or bool(prompt_issues),
    }


@router.post(route_prefix + "/{graphname}/migration/apply")
def migration_apply(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    payload: Annotated[dict | None, Body()] = None,
):
    """Apply repairs reported by ``GET /migration/status``.

    Request body (all optional):

        {
          "outdated": ["Q1", ...],      # queries whose installed body drifted from local
          "not_installed": ["Q2", ...], # required queries missing / not installed
          "apply_schema": false         # stubbed — no-op until check_and_apply_schema is implemented
        }

    The goal is that every required query ends up installed and current, so
    each listed query is (re)created and (re)installed — there is no
    per-category opt-in. When neither list is provided the endpoint detects
    the repair set itself. Only shipped query names are honored.

    Acquires the per-graph lock for the duration of the repair so that
    a concurrent ingest / rebuild / schema-extraction on the same graph
    cannot race against the CREATE OR REPLACE + INSTALL QUERY
    sequence. Also rejects upfront if any rebuild is in flight
    (rebuilds hold their own catalog locks on TG and would deadlock).
    """
    from common.db.migrate import _gsql_hash, _extract_query_body, _read_local_query, check_and_apply_schema

    import os.path

    body = payload or {}
    # Explicit repair lists from the status check. The repair button is only
    # enabled after detection produced them, so when present apply trusts them
    # and skips its own per-query re-detection. The goal is simply that every
    # required query ends up installed and current, so every listed query is
    # (re)created and (re)installed — no per-category opt-in.
    queries_outdated = body.get("outdated")
    queries_not_installed = body.get("not_installed")
    apply_schema = bool(body.get("apply_schema", False))

    # Pre-flight: reject if any rebuild is in flight. The rebuild's
    # INSTALL QUERY ALL holds TG catalog locks that would cause our
    # CREATE OR REPLACE calls to time-out, leaving the graph half-
    # migrated.
    currently_rebuilding = get_rebuilding_graph()
    if currently_rebuilding:
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{currently_rebuilding}' is currently being rebuilt. "
                   f"Migration repair cannot run while a rebuild is in flight."
        )

    # Acquire the per-graph lock so concurrent create_ingest / ingest /
    # schema_extraction can't race against our query reinstalls. They
    # all use the same lock and will 409 until we release.
    if not acquire_graph_lock(graphname, "migration"):
        current = get_current_operation(graphname) or "another operation"
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently busy with '{current}'. "
                   f"Wait for it to finish before running migration repair."
        )

    try:
        cred_obj = creds[1]
        conn = get_db_connection_pwd_manual(graphname, cred_obj.username, cred_obj.password)
        return _migration_apply_inner(
            graphname, conn,
            queries_outdated=queries_outdated,
            queries_not_installed=queries_not_installed,
            apply_schema=apply_schema,
        )
    finally:
        release_graph_lock(graphname, "migration")


def _migration_apply_inner(
    graphname: str,
    conn,
    apply_schema: bool,
    queries_outdated: list[str] | None = None,
    queries_not_installed: list[str] | None = None,
):
    """Body of migration_apply, separated so the outer wrapper handles
    the graph-lock acquire/release boilerplate.
    """
    from common.db.migrate import _gsql_hash, get_installed_query_names, get_installed_query_body, check_and_apply_schema
    from common.db.query_install import install_query_set
    import os.path

    # Read live domain schema for templated-retriever rendering.
    domain_vts, domain_edges, include_entity = _get_domain_schema_for_render(conn, graphname)

    reinstalled: list[str] = []
    installed_new: list[str] = []
    errors: list[dict] = []

    # Shipped query name -> local .gsql path.
    name_to_path = {
        os.path.splitext(os.path.basename(p))[0]: p
        for p in _MIGRATION_QUERY_PATHS if os.path.exists(p)
    }

    paths_to_create: list[tuple[str, bool]] = []  # (path, was_installed)

    if queries_outdated is not None or queries_not_installed is not None:
        # The status check already detected what needs repair and enabled the
        # repair button with these lists — trust them and skip the redundant
        # re-detection. Every listed query is (re)created and (re)installed;
        # only shipped query names are honored.
        for name in (queries_outdated or []):
            p = name_to_path.get(name)
            if p:
                paths_to_create.append((p, True))
            else:
                errors.append({"query": name, "phase": "detect", "error": "unknown query"})
        for name in (queries_not_installed or []):
            p = name_to_path.get(name)
            if p:
                paths_to_create.append((p, False))
            else:
                errors.append({"query": name, "phase": "detect", "error": "unknown query"})
    else:
        # No explicit lists: detect what needs repair ourselves. A required
        # query needs (re)create + (re)install if it isn't installed (absent
        # from the installed-query endpoints — covers "never created" too) or
        # its installed body differs from local (after rendering templated
        # retrievers). Install state comes from one batched call.
        installed_names = get_installed_query_names(conn, graphname)
        for q_path in _MIGRATION_QUERY_PATHS:
            if not os.path.exists(q_path):
                continue
            q_name = os.path.splitext(os.path.basename(q_path))[0]
            local_hash = _local_query_hash(
                q_path, q_name, domain_vts, domain_edges, include_entity
            )
            if local_hash is None:
                continue
            if q_name not in installed_names:
                paths_to_create.append((q_path, False))
                continue
            try:
                installed_body = get_installed_query_body(conn, graphname, q_name)
            except Exception as e:
                errors.append({"query": q_name, "phase": "detect", "error": str(e)})
                continue
            if not installed_body or _gsql_hash(installed_body) != local_hash:
                paths_to_create.append((q_path, True))

    # Pass 1: re-create each drifted/missing query body (CREATE OR REPLACE).
    # Templated retrievers get rendered with the live domain schema before
    # being sent — same as install_retrievers does at init time. Otherwise
    # we'd push the un-templated body and the next rebuild's hybrid/
    # community walks wouldn't traverse domain edges.
    from common.db.retriever_render import TEMPLATED_RETRIEVERS, render_retriever_body
    for q_path, was_installed in paths_to_create:
        q_name = os.path.splitext(os.path.basename(q_path))[0]
        try:
            with open(q_path, "r") as f:
                q_body = f.read()
            if q_name in TEMPLATED_RETRIEVERS:
                q_body = render_retriever_body(
                    q_body,
                    domain_vts=domain_vts,
                    domain_edges=domain_edges,
                    include_entity=include_entity,
                )
            # Create/replace the body via the pyTigerGraph query API
            # (createQuery -> POST /gsql/v1/queries). Distinguish a TigerGraph
            # query error — the body fails type/semantic checks, so TG saves it
            # as a draft and returns an explanatory ``message`` — from a genuine
            # HTTP/transport error. A query error is definitive: report the
            # response ``message`` directly, with no retry. Only a transport
            # error (no TG query-error body) falls back to a GSQL CREATE, whose
            # error text (returned as a string, not raised) is checked and
            # compressed for display.
            conn.graphname = graphname
            tg_err = None
            try:
                res = conn.createQuery(q_body)
                tg_err = create_response_error(res)
            except Exception as create_exc:
                tg_err = create_response_error(http_error_response_body(create_exc))
                if not tg_err:
                    logger.info(f"Migration: createQuery transport error for '{q_name}'; gsql fallback: {create_exc}")
                    gres = conn.gsql(f"USE GRAPH {graphname}\nBEGIN\n{q_body}\nEND\n")
                    if gsql_output_error(gres):
                        logger.debug(f"Migration: full gsql result for '{q_name}': {gres}")
                        tg_err = concise_gsql_error(gres)
            if tg_err:
                raise Exception(tg_err)
            logger.info(f"Migration: created/updated '{q_name}'")
            if was_installed:
                reinstalled.append(q_name)
            else:
                installed_new.append(q_name)
        except Exception as e:
            # ``e`` already carries a display-ready message (the TG response
            # ``message`` for a query error, or a compressed gsql error);
            # other failures (file read / render) surface their own text.
            logger.error(f"Migration: failed to create '{q_name}': {e}", exc_info=True)
            errors.append({"query": q_name, "phase": "create", "error": str(e)[:400]})

    # Pass 2: install ONLY the queries just re-created, by name — never
    # INSTALL QUERY ALL, which recompiles every query on the graph and is the
    # dominant cost of a repair. Uses the shared async-submit + poll utility
    # (common.db.query_install) rather than pyTigerGraph's installQueries.
    to_install = reinstalled + installed_new
    if to_install:
        try:
            install_query_set(conn, to_install)
            logger.info(f"Migration: installed {len(to_install)} query(ies): {', '.join(to_install)}")
        except Exception as e:
            logger.error(f"Migration: installing {to_install} failed: {e}", exc_info=True)
            errors.append({"query": ", ".join(to_install), "phase": "install", "error": concise_gsql_error(e)})

    schema_result = {"applied": [], "skipped_reason": "skipped by request"}
    if apply_schema:
        try:
            schema_result = check_and_apply_schema(conn, graphname)
        except Exception as e:
            logger.error(f"Migration: schema repair failed: {e}", exc_info=True)
            errors.append({"query": "*", "phase": "schema", "error": str(e)})

    return {
        "graphname": graphname,
        "queries_reinstalled": reinstalled,
        "queries_installed_new": installed_new,
        "schema_result": schema_result,
        "errors": errors,
        "success": not errors,
    }


@router.post(route_prefix + "/{graphname}/initialize_graph")
def init_graph(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    bg_tasks: BackgroundTasks,
    payload: Annotated[dict | None, Body()] = None,
):
    """
    Submit a TigerGraph knowledge-graph initialization job.

    Returns 202 immediately with ``{"status": "submitted", "graphname": ...}``;
    the long-running work (structural schema, optional domain schema apply,
    retriever installs) runs in a BackgroundTask. UI clients poll
    ``GET /ui/{graphname}/initialize_status`` for state and final result.

    The structural GraphRAG schema (Document, DocumentChunk, Entity,
    EntityType, RelationshipType, Content, Community, Image and their
    structural edges) is always created if missing.

    Optionally accepts a JSON body:

        {"schema_gsql": "ADD VERTEX Company(...); ...",
         "use_existing_schema": true}

    When ``schema_gsql`` is provided, the pasted text is parsed
    permissively, structural-type collisions and dangling pairs are
    dropped, the diff against the current graph is computed, and the
    additive delta is applied. Existing types are never dropped.

    When ``use_existing_schema`` is true, the graph's current
    user-defined vertex/edge types are adopted as the domain schema
    (retrievers are installed against them). Mutually exclusive with
    ``schema_gsql`` — sending both is a 400.

    Pre-flight eligibility check rejects:
      * ``structural_present`` — graph already has GraphRAG structural
        types (Entity / Document / etc.). User must manually drop them
        before re-initializing.
      * ``user_types_present_strict`` — graph has user-defined types
        AND the caller asked for a new schema (``schema_gsql``). Mixing
        a fresh schema on top of pre-existing types risks corruption;
        force a manual cleanup.
      * ``user_types_present`` — graph has user-defined types and the
        caller asked for ``none`` (no domain schema). The UI re-submits
        with ``use_existing_schema=true`` if the user confirms.
    """
    schema_gsql = (
        (payload or {}).get("schema_gsql") if isinstance(payload, dict) else None
    )
    use_existing_schema = bool(
        (payload or {}).get("use_existing_schema") if isinstance(payload, dict) else False
    )
    existing_vertex_descs = (payload or {}).get("vertex_descriptions") or {}
    existing_edge_descs = (payload or {}).get("edge_descriptions") or {}
    if not isinstance(existing_vertex_descs, dict):
        existing_vertex_descs = {}
    if not isinstance(existing_edge_descs, dict):
        existing_edge_descs = {}
    if schema_gsql and use_existing_schema:
        raise HTTPException(
            status_code=400,
            detail="schema_gsql and use_existing_schema are mutually exclusive.",
        )
    cred_obj = creds[1]
    auth_header = "Basic " + base64.b64encode(
        f"{cred_obj.username}:{cred_obj.password}".encode()
    ).decode()

    # Pre-flight eligibility check: introspect the live schema and
    # decide whether to proceed, reject, or adopt existing types.
    eligibility = _check_init_eligibility(auth_header, graphname)
    if eligibility["state"] == "structural_present":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "structural_present",
                "message": "Existing GraphRAG schema detected, manual cleanup required.",
                "structural_types": eligibility["structural_types"],
            },
        )
    if eligibility["state"] == "user_types_present":
        user_types = eligibility["user_vertex_types"] + eligibility["user_edge_types"]
        if schema_gsql:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "user_types_present_strict",
                    "message": (
                        f"Graph already has types: {', '.join(user_types)}. "
                        "Manual cleanup required before extracting or applying a new schema."
                    ),
                    "user_vertex_types": eligibility["user_vertex_types"],
                    "user_edge_types": eligibility["user_edge_types"],
                },
            )
        if not use_existing_schema:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "user_types_present",
                    "message": (
                        f"Graph '{graphname}' already has types: "
                        f"{', '.join(user_types)}. "
                        "Use them as the domain schema, or cancel and clean manually."
                    ),
                    "user_vertex_types": eligibility["user_vertex_types"],
                    "user_edge_types": eligibility["user_edge_types"],
                },
            )
    # else: state == "empty" → proceed normally

    # Atomically check-and-reserve so two concurrent /initialize_graph
    # requests for the same graph can't both enqueue background jobs.
    reserved_collision = _try_reserve_init(graphname)
    if reserved_collision is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Initialization already in progress for graph '{graphname}'",
        )

    def _run_init():
        try:
            _set_init_state(
                graphname, state="running",
                message="Initializing structural schema",
            )
            _, conn = ws_basic_auth(auth_header, graphname)
            LogWriter.info(f"Initializing graph: {graphname}")
            resp = supportai.init_supportai(conn, graphname)
            schema_res, index_res, query_res = resp[0], resp[1], resp[2]

            domain_schema_status: dict | None = None
            proposal = None
            if isinstance(schema_gsql, str) and schema_gsql.strip():
                proposal = schema_utils_mod.parse_gsql_schema(schema_gsql)
                proposal.drop_dangling_pairs()
            elif use_existing_schema:
                # Build a proposal from the live user-defined types so
                # apply_proposal registers them as the domain schema and
                # installs retrievers. The diff is a no-op because the
                # types already exist; this run only writes type
                # metadata (descriptions) and re-creates retriever queries.
                proposal = _build_proposal_from_live_schema(
                    conn,
                    vertex_descriptions=existing_vertex_descs,
                    edge_descriptions=existing_edge_descs,
                )
                LogWriter.info(
                    f"Adopting existing schema as domain for {graphname}: "
                    f"{len(proposal.vertices)} vertex types, "
                    f"{len(proposal.edges)} edge types"
                )

            if proposal is not None:
                _set_init_state(graphname, message="Applying domain schema")
                LogWriter.info(
                    f"Applying domain schema proposal for graph: {graphname}"
                )
                # Surface apply_proposal's sub-phases (schema-change,
                # metadata, retriever installs) in the init-dialog
                # poll instead of a static "Applying domain schema".
                domain_schema_status = schema_utils_mod.apply_proposal(
                    conn, graphname, proposal,
                    progress=lambda msg: _set_init_state(
                        graphname, message=msg
                    ),
                )
                LogWriter.info(
                    f"Domain schema status for {graphname}: "
                    f"{domain_schema_status['status']} "
                    f"({len(domain_schema_status['statements'])} stmts)"
                )
                if domain_schema_status.get("status") == "error":
                    LogWriter.error(
                        f"Domain schema apply failed for {graphname}: "
                        f"{domain_schema_status.get('error')}"
                    )
                    _set_init_state(
                        graphname,
                        state="error",
                        message="Domain schema apply failed",
                        error=domain_schema_status.get("error"),
                        completed_at=time.time(),
                        result={"domain_schema_status": domain_schema_status},
                    )
                    return

            LogWriter.info(f"Graph initialization completed for: {graphname}")

            result = {
                "status": "success",
                "message": f"Graph '{graphname}' initialized successfully",
                "graphname": graphname,
                "host_name": conn._tg_connection.host,
                "schema_creation_status": json.dumps(schema_res),
                "index_creation_status": json.dumps(index_res),
                "query_creation_status": json.dumps(query_res),
            }
            if domain_schema_status is not None:
                result["domain_schema_status"] = domain_schema_status

            _set_init_state(
                graphname,
                state="completed",
                message="Initialization completed successfully",
                completed_at=time.time(),
                result=result,
            )
        except Exception as e:
            LogWriter.error(f"Error initializing graph {graphname}: {str(e)}")
            _set_init_state(
                graphname,
                state="error",
                message=f"Initialization failed: {e}",
                error=str(e),
                completed_at=time.time(),
            )

    bg_tasks.add_task(_run_init)
    return {
        "status": "submitted",
        "graphname": graphname,
        "message": "Initialization started; poll initialize_status for progress.",
    }


@router.get(route_prefix + "/{graphname}/initialize_status")
def get_initialize_status(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Return the current init state for *graphname*.

    States:
      * ``unknown``  — no init has ever been submitted for this graph
        (or the worker restarted, dropping in-memory state).
      * ``queued``   — submitted, background task not yet running.
      * ``running``  — backend is doing work; ``message`` describes the phase.
      * ``completed``— done; ``result`` carries the final init payload.
      * ``error``    — failed; ``error`` carries the failure reason.
    """
    return _get_init_state(graphname)


def _sweep_legacy_schema_subdirs(graphname: str) -> None:
    """Remove any ``_schema_<id>/`` staging subdirectories under the
    graph's uploads tree. Idempotent — safe to call on every
    sample-upload request.
    """
    for parent in (
        os.path.join("uploads", graphname),
        os.path.join("uploads", "ingestion_temp", graphname),
    ):
        if not os.path.isdir(parent):
            continue
        for name in os.listdir(parent):
            if name.startswith("_schema_"):
                stale = os.path.join(parent, name)
                if os.path.isdir(stale):
                    try:
                        shutil.rmtree(stale)
                    except OSError as exc:
                        logger.warning(
                            f"Could not remove legacy schema subdir {stale}: {exc}"
                        )


@router.post(route_prefix + "/{graphname}/convert_sample_files")
async def convert_sample_files(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    files: Annotated[list[UploadFile], File(description="Sample documents (≤5)")],
    overwrite: bool = False,
    skip: str | None = None,
):
    """
    Step 1/2 of the sample-doc schema extraction flow:

    Save uploaded sample files to ``uploads/<graphname>/`` and convert
    each to JSONL under ``uploads/ingestion_temp/<graphname>/``. Files
    are persisted so the Ingest Document dialog can reuse them, and
    the JSONL cache means a subsequent Ingest run won't re-convert.

    Returns the list of saved filenames so the caller can pass them
    to ``POST /ui/<graph>/extract_schema_from_jsonl``.

    Collision handling mirrors ``POST /uploads``:
      * ``overwrite=false`` (default) and any filename already exists →
        ``{"status": "conflict", "existing_files": [...]}`` is returned
        and nothing is written. Pre-flight via ``POST /uploads/check``
        before sending bytes to avoid re-uploading on conflict.
      * ``overwrite=true`` replaces the existing file (and its cached
        JSONL) before re-converting.
      * ``skip`` is a comma-separated list of filenames to drop from the
        incoming set silently — useful when the user chose "skip" on a
        conflict prompt for a subset of files.

    Concurrent schema-extraction requests against the same graph are
    rejected with 409 — only one runs at a time.

    No LLM call. Caps come from ``graphrag_config``:
      * ``schema_max_sample_files`` (default 5) — file count
      * ``schema_max_total_mb`` (default 50) — cumulative upload size

    Per-file size is bounded only by the cumulative cap, so a single
    file may use the full budget.
    """
    max_files = int(graphrag_config.get("schema_max_sample_files", 5))
    max_total_mb = int(graphrag_config.get("schema_max_total_mb", 50))
    max_total_bytes = max_total_mb * 1024 * 1024

    skip_set: set[str] = set()
    if skip:
        skip_set = {os.path.basename(s.strip()) for s in skip.split(",") if s.strip()}

    accepted = [f for f in files if os.path.basename(f.filename or "") not in skip_set]
    if len(accepted) > max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files: got {len(accepted)}, max is {max_files}.",
        )
    if not accepted:
        raise HTTPException(status_code=400, detail="No files supplied.")

    acquired = await asyncio.to_thread(
        acquire_graph_lock, graphname, "schema_extraction"
    )
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Graph '{graphname}' is already running schema extraction "
                "or another ingest operation. Please wait and try again."
            ),
        )

    try:
        _sweep_legacy_schema_subdirs(graphname)

        upload_dir = os.path.join("uploads", graphname)
        os.makedirs(upload_dir, exist_ok=True)
        temp_folder = os.path.join("uploads", "ingestion_temp", graphname)
        os.makedirs(temp_folder, exist_ok=True)

        if not overwrite:
            existing = [
                os.path.basename(f.filename or "")
                for f in accepted
                if os.path.exists(
                    os.path.join(upload_dir, os.path.basename(f.filename or ""))
                )
            ]
            if existing:
                return {
                    "status": "conflict",
                    "message": (
                        "Some files already exist. Resend with overwrite=true "
                        "to replace them, or with skip=<filename,...> to drop "
                        "specific files from the upload set."
                    ),
                    "existing_files": existing,
                }

        saved_basenames: list[str] = []
        total_bytes = 0
        for f in accepted:
            data = await f.read()
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"Total upload exceeds {max_total_mb} MB cap.",
                )
            safe_name = os.path.basename(f.filename or "sample")
            if safe_name in saved_basenames:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Duplicate filename '{safe_name}' in upload set. "
                        "Rename one of the files and try again."
                    ),
                )

            # On overwrite, drop the cached JSONL so the new bytes
            # are re-converted instead of silently reusing the stale
            # extract.
            if overwrite:
                stem = os.path.splitext(safe_name)[0]
                cached_jsonl = os.path.join(temp_folder, f"{stem}.jsonl")
                if os.path.exists(cached_jsonl):
                    try:
                        os.remove(cached_jsonl)
                    except OSError as exc:
                        logger.warning(
                            f"Could not remove cached jsonl {cached_jsonl}: {exc}"
                        )

            target = os.path.join(upload_dir, safe_name)
            with open(target, "wb") as out:
                out.write(data)
            saved_basenames.append(safe_name)

        extractor = TextExtractor()
        try:
            # Restrict the conversion walk to just this request's files
            # so unrelated files that already live in ``upload_dir`` from
            # earlier uploads are not re-converted.
            result = await extractor._process_folder_async(
                upload_dir, graphname, temp_folder,
                filenames=saved_basenames,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Text extraction failed: {exc}",
            )

        LogWriter.info(
            f"Converted sample files for {graphname}: "
            f"{len(accepted)} uploaded, {result.get('num_documents', 0)} docs in JSONL"
        )
        return {
            "status": "success",
            "graphname": graphname,
            "saved_files": list(saved_basenames),
            "skipped_files": sorted(skip_set),
            "num_documents": result.get("num_documents", 0),
        }
    finally:
        await asyncio.to_thread(release_graph_lock, graphname, "schema_extraction")


@router.post(route_prefix + "/{graphname}/extract_schema_from_jsonl")
def extract_schema_from_jsonl(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    payload: Annotated[dict | None, Body()] = None,
):
    """
    Step 2/2 of the sample-doc schema extraction flow:

    Read the previously-converted JSONLs (from ``convert_sample_files``)
    and run the schema-extraction LLM over them. Returns the proposed
    domain schema as GSQL plus a structured proposal dict for the
    form-mode editor.

    Body:
        ``{"filenames": ["report1.pdf", "report2.docx"]}``
    The endpoint reads ``uploads/ingestion_temp/<graphname>/<stem>.jsonl``
    for each listed name. ``filenames`` is required and must be a
    non-empty list — every sample file the caller wants fed to the
    schema-extraction LLM must be named explicitly.

    Concurrent schema-extraction requests against the same graph are
    rejected with 409 — only one runs at a time.
    """
    requested = []
    if isinstance(payload, dict):
        requested = payload.get("filenames") or []
    if not requested:
        raise HTTPException(
            status_code=400,
            detail=(
                "No sample files specified. Pass 'filenames' as a non-empty "
                "list naming each previously-converted sample to feed the "
                "schema-extraction LLM."
            ),
        )

    acquired = acquire_graph_lock(graphname, "schema_extraction")
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Graph '{graphname}' is already running schema extraction "
                "or another ingest operation. Please wait and try again."
            ),
        )

    try:
        temp_folder = os.path.join("uploads", "ingestion_temp", graphname)
        if not os.path.isdir(temp_folder):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No converted JSONLs found for graph {graphname}. "
                    "Run convert_sample_files first."
                ),
            )

        jsonl_paths = []
        missing_jsonls = []
        for name in requested:
            stem = os.path.splitext(os.path.basename(name))[0]
            p = os.path.join(temp_folder, f"{stem}.jsonl")
            if os.path.exists(p):
                jsonl_paths.append(p)
            else:
                missing_jsonls.append(name)
        if missing_jsonls:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Converted JSONL not found for: "
                    + ", ".join(missing_jsonls)
                    + ". Run convert_sample_files first for those files."
                ),
            )

        samples: list[dict] = []
        for jp in jsonl_paths:
            with open(jp, "r", encoding="utf-8") as jf:
                for line in jf:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        samples.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        if not samples:
            raise HTTPException(
                status_code=400,
                detail="No extractable text in the converted files.",
            )

        # Optional structured hints from the UI (TagInput chips). Each
        # hint is ``{"name": str, "description": str}``. Backend ignores
        # malformed entries silently — names are validated client-side.
        vertex_hints = (payload or {}).get("vertex_hints") if isinstance(payload, dict) else None
        edge_hints = (payload or {}).get("edge_hints") if isinstance(payload, dict) else None

        LogWriter.info(
            f"Running schema extraction LLM for {graphname} "
            f"({len(jsonl_paths)} JSONLs, {len(samples)} doc parts, "
            f"{len(vertex_hints or [])} vertex hints, {len(edge_hints or [])} edge hints)"
        )
        llm_service = get_llm_service(get_chat_config(graphname))
        gsql_text, rendered_prompt = schema_extraction_mod.extract_schema_gsql(
            llm_service, samples,
            vertex_hints=vertex_hints, edge_hints=edge_hints,
        )
        proposal = schema_utils_mod.parse_gsql_schema(gsql_text)
        proposal.drop_dangling_pairs()
        return {
            "status": "success",
            "graphname": graphname,
            "schema_gsql": gsql_text,
            "preview_gsql": schema_utils_mod.emit_preview_gsql(proposal),
            "proposal": proposal.to_dict(),
            "summary": schema_utils_mod.summarize(proposal),
            # The fully-rendered prompt (default + suggested-types block).
            # The UI saves this verbatim as the per-graph override after a
            # successful initialize_graph so the addendum survives the
            # session.
            "rendered_prompt": rendered_prompt,
        }
    finally:
        release_graph_lock(graphname, "schema_extraction")


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

    # Reject if a per-graph operation (migration repair, schema
    # extraction, ingest job creation) is currently holding the graph
    # lock — running INSTALL QUERY ALL concurrently with a rebuild's
    # query install would deadlock on TG's catalog lock.
    current_op = get_current_operation(graphname)
    if current_op and current_op not in ("rebuild",):
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is currently busy with '{current_op}'. "
                   f"Wait for it to finish before triggering a rebuild."
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
    auth_header = _ecc_auth_header(creds)

    ecc_base = graphrag_config.get("ecc", "http://graphrag-ecc:8001")
    ecc_update_url = f"{ecc_base}/{graphname}/graphrag/consistency_update"
    ecc_status_url = f"{ecc_base}/{graphname}/graphrag/rebuild_status"

    LogWriter.info(f"Sending ECC rebuild request to: {ecc_update_url}")

    # Background task to trigger rebuild, monitor completion, and release lock
    async def rebuild_and_monitor():
        try:
            # Step 1: Trigger the ECC rebuild (non-blocking)
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(ecc_update_url, headers={"Authorization": auth_header})
                if response.status_code not in [200, 202]:
                    LogWriter.error(f"ECC rebuild trigger failed for {graphname}: {response.status_code} - {response.text}")
                    return
            
            LogWriter.info(f"ECC rebuild triggered for {graphname}, now monitoring status...")
            
            # Step 2: Poll ECC status until all 4 stages complete (non-blocking)
            max_wait_time = 7200  # 2 hours max
            poll_interval = 5  # Check every 5 seconds
            elapsed = 0
            
            while elapsed < max_wait_time:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        status_response = await client.get(
                            ecc_status_url,
                            headers={"Authorization": auth_header}
                        )
                    
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        is_running = status_data.get("is_running", False)
                        status = status_data.get("status", "unknown")

                        # Cache last known good response so the UI endpoint
                        # can fall back to it when ECC is too busy to respond.
                        if is_running and status_data.get("started_at"):
                            _last_ecc_status_cache[graphname] = status_data
                        
                        # Log every minute to avoid spam
                        if elapsed % 60 == 0:
                            LogWriter.info(f"ECC status for {graphname}: {status} (running={is_running}) - elapsed {elapsed}s")
                        
                        # Check if ALL stages are complete
                        if not is_running and status in ["completed", "failed", "idle"]:
                            LogWriter.info(f"ECC rebuild finished for {graphname} with status: {status} after {elapsed}s")
                            _last_ecc_status_cache.pop(graphname, None)
                            break
                    else:
                        LogWriter.warning(f"ECC status check returned {status_response.status_code} for {graphname}")
                        
                except Exception as e:
                    LogWriter.warning(f"Failed to check ECC status for {graphname}: {e}")
                    # Continue polling - ECC might still be working
            
            if elapsed >= max_wait_time:
                LogWriter.error(f"ECC rebuild monitoring timed out for {graphname} after {max_wait_time}s")
                _last_ecc_status_cache.pop(graphname, None)
                
        except Exception as e:
            LogWriter.error(f"Error during ECC rebuild monitoring for {graphname}: {e}")
            import traceback
            LogWriter.error(traceback.format_exc())
        finally:
            # Always drop cached status when monitoring ends (success,
            # timeout, or unexpected failure) so timeout fallbacks do
            # not keep reporting a stale rebuild as active.
            _last_ecc_status_cache.pop(graphname, None)
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
    auth_header = _ecc_auth_header(creds)

    try:
        ecc_status_url = (
            graphrag_config.get("ecc", "http://graphrag-ecc:8001")
            + f"/{graphname}/graphrag/rebuild_status"
        )
        LogWriter.info(f"Checking ECC status at: {ecc_status_url}")

        response = httpx.get(
            ecc_status_url,
            headers={"Authorization": auth_header},
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
        # ECC is busy (heavy processing) - assume rebuild is still running.
        # Return the last cached status so the UI keeps started_at and stage.
        LogWriter.warning(f"ECC status check timed out (ECC may be busy): {str(e)}")
        cached = _last_ecc_status_cache.get(graphname, {})
        return {
            **cached,
            "graphname": graphname,
            "is_running": True,
            "status": cached.get("status", "unknown"),
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
        auth = "Basic " + base64.b64encode(
            f"{creds.username}:{creds.password}".encode()
        ).decode()
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
        auth = "Basic " + base64.b64encode(
            f"{creds.username}:{creds.password}".encode()
        ).decode()
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
        auth = "Basic " + base64.b64encode(
            f"{creds.username}:{creds.password}".encode()
        ).decode()
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
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/user/{user_id}",
                headers={"Authorization": _chat_history_auth_header(creds)},
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)]
):
    roles, graph_roles, _ = _get_user_role_details(
        credentials.username, credentials.password
    )
    return {"roles": roles, "graph_roles": graph_roles}


@router.get(route_prefix + "/conversation/{conversation_id}")
async def get_conversation_contents(
    conversation_id: str,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    creds = creds[1]
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/conversation/{conversation_id}",
                headers={"Authorization": _chat_history_auth_header(creds)},
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
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{graphrag_config['chat_history_api']}/get_feedback",
                headers={"Authorization": _chat_history_auth_header(creds)},
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
    try:
        async with httpx.AsyncClient() as client:
            res = await client.delete(
                f"{graphrag_config['chat_history_api']}/conversation/{conversation_id}",
                headers={"Authorization": _chat_history_auth_header(creds)},
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


# When agent execution fails, non-superadmins get a generic message;
# superadmins additionally see the exception's root cause so they can
# diagnose backend failures (e.g. an LLM provider quota/auth error)
# without exposing internals to regular users. The full stack stays out
# of the chat bubble. See GML-2136.
# Matches the trace-log read gate (get_trace_log) so error-detail and
# trace visibility share one definition of "superadmin".
SUPERADMIN_ROLES = {"superuser"}


def _is_superadmin(global_roles: list[str]) -> bool:
    return any(r in SUPERADMIN_ROLES for r in (global_roles or []))


def _agent_error_text(e: Exception, is_superadmin: bool = False) -> str:
    # asyncio.TaskGroup wraps a failing sub-task in an ExceptionGroup whose
    # message is only "unhandled errors in a TaskGroup (N sub-exception(s))".
    # Unwrap to the underlying cause so the admin detail shows the real error.
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    error_msg = str(e)
    if "does not exist" in error_msg or "not found" in error_msg.lower():
        return f"Error: {error_msg}. Please check the knowledge graph name and try again."
    generic = (
        "GraphRAG had an issue answering your question. "
        "Please try again, or rephrase your prompt."
    )
    # Never return raw exception text to the client. It can carry sensitive
    # configuration, URLs, request fragments, or credentials, and the chat
    # response is persisted to conversation history. Admins instead get a
    # reference ID that correlates to the full detail in the protected server
    # logs (logged here at ERROR so the mapping is guaranteed).
    if is_superadmin and error_msg:
        ref = req_id_cv.get() or "n/a"
        logger.error(f"agent error [ref={ref}]: {error_msg}")
        return f"{generic}\n\n(Admin reference ID: {ref} — see server logs for details.)"
    return generic


async def run_agent(
    agent: TigerGraphAgent,
    data: str,
    conversation_history: list[dict[str, str]],
    graphname,
    ws: WebSocket,
    is_superadmin: bool = False,
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
        resp.natural_language_response = _agent_error_text(e, is_superadmin)
        # Preserve the steps the agent completed before failing so the
        # (superuser-only) trace log shows how far it got (GML-2136).
        partial_steps = getattr(agent, "_last_agent_steps", None)
        resp.query_sources = {"agent_steps": partial_steps} if partial_steps else {}
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


async def load_conversation_history(
    conversation_id: str, usr_creds: HTTPBasicCredentials
) -> list[dict[str, str]]:
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

    headers = {"Authorization": _chat_history_auth_header(usr_creds)}
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


async def write_message_to_history(
    message: Message, usr_creds: HTTPBasicCredentials
):
    ch = graphrag_config.get("chat_history_api")
    if ch is not None:
        headers = {"Authorization": _chat_history_auth_header(usr_creds)}
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

# Recognized agentic orchestrator styles. A value routed to the agentic engine
# that is not one of these (e.g. a classic retriever name like "hybrid") would
# otherwise be silently coerced into a bogus style, so it is normalized instead.
_AGENT_STYLES = {"auto", "planned", "reactive", "react"}


def _chat_agent(graphname, conn, use_cypher, mode, value, ws=None):
    """Build the chat agent from one menu selection.

    ``mode`` (``"agentic"`` | ``"classic"`` | ``None`` → graph config) picks the
    engine; ``value`` is the single menu value — the agent style (``"auto"`` |
    ``"planned"`` | ``"reactive"``) when agentic, or the retriever (``"auto"`` |
    a name) when classic.

    ``value`` is overloaded, so it is validated against the *resolved* engine
    before use — a value belonging to the other engine is never mis-mapped
    (e.g. a classic retriever name reaching the agentic orchestrator). When the
    caller sends no ``mode``, a non-``auto`` value that isn't an agent style is
    treated as a retriever and routed to the classic engine, preserving
    pre-agentic clients that selected a retriever via ``rag_method`` alone.
    """
    from common.config import get_agent_mode

    value = (value or "auto").strip()
    vlow = value.lower()
    resolved_mode = (mode or "").strip().lower()
    if resolved_mode not in ("agentic", "classic"):
        if vlow == "auto":
            resolved_mode = get_agent_mode(graphname)      # ambiguous -> graph config
        elif vlow in _AGENT_STYLES:
            resolved_mode = "agentic"                       # an agent style implies agentic
        else:
            resolved_mode = "classic"                       # a retriever name implies classic

    if resolved_mode == "classic":
        # Classic side maps an unknown value to its default retriever.
        retriever, style = value, "auto"
    else:
        # Only a real agent style reaches the orchestrator; else fall back.
        retriever, style = "auto", (value if vlow in _AGENT_STYLES else "auto")

    return make_agent(
        graphname, conn, use_cypher, ws=ws, mode=resolved_mode,
        supportai_retriever=retriever, agent_style=style,
    )


def _select_message_fields(message: Message, include_fields: str | None) -> Message:
    """Trim optional fields from the response copy of a chat message.

    By default the response carries the answer envelope only. ``include_fields``
    is a comma-separated list; pass ``query_sources`` (or ``all``) to include the
    supporting sources / trace. The persisted message keeps the full set
    regardless — only the returned payload is trimmed.
    """
    requested = {f.strip().lower() for f in (include_fields or "").split(",") if f.strip()}
    if "all" in requested:
        return message
    out = message.model_copy()
    if "query_sources" not in requested:
        out.query_sources = None
    return out


@router.get(route_prefix + "/{graphname}/query")
async def graph_query(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    q: str | None = None,
    rag_pattern: str | None = None,
    mode: str | None = None,
    conversation_id: str | None = None,
    include_fields: str | None = None,
):
    is_superadmin = _is_superadmin(creds[0])
    creds = creds[1]
    auth_header = "Basic " + base64.b64encode(
        f"{creds.username}:{creds.password}".encode()
    ).decode()
    _, conn = ws_basic_auth(auth_header, graphname)
    try:
        # Load conversation history if conversation_id is provided
        conversation_history = await load_conversation_history(conversation_id, creds) if conversation_id else []

        # Use provided conversation ID or generate new one
        if not conversation_id or conversation_id == "new":
            convo_id = str(uuid.uuid4())
            LogWriter.info(f"Starting new conversation with ID: {convo_id}")
        else:
            convo_id = conversation_id
            LogWriter.info(f"Continuing conversation with ID: {convo_id}")

        # create agent from the menu selection (engine + style/retriever)
        agent = _chat_agent(graphname, conn, use_cypher, mode, rag_pattern)

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
        await write_message_to_history(message, creds)
        prev_id = message.message_id

        # generate response and keep track of response time
        start = time.monotonic()
        resp = await run_agent(
            agent, data, conversation_history, graphname, None,
            is_superadmin=is_superadmin,
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
        await write_message_to_history(message, creds)
        await asyncio.to_thread(_save_trace_log, message.message_id, convo_id, data, resp, elapsed, creds.username)
        prev_id = message.message_id

        # reply — trim to the answer envelope unless extra fields were requested
        return _select_message_fields(message, include_fields).model_dump_json()
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
    mode: str | None = None,
):
    """
    WebSocket endpoint for chat functionality with conversation history support.

    Expected message flow:
    1. Authentication: full Authorization header value, ``Basic <b64>``
       or ``Bearer <token>``.
    2. RAG pattern (e.g., "hybridsearch", "similaritysearch", etc.)
    3. Conversation ID (or "new" for new conversation)
    4. User messages
    """
    # Embedding store unavailable: WebSocket routes can't return an
    # HTTPException — ASGI requires the handshake to be sent (or the
    # connection explicitly closed) before the callable returns.
    await websocket.accept()

    # AUTH with proper error handling and timeout
    try:
        logger.info(f"WebSocket connected, waiting for authentication for graph: {graphname}")
        usr_auth = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        logger.info(f"Received authentication data, length: {len(usr_auth)}")
        _, conn = ws_basic_auth(usr_auth, graphname)

        # If the embedding store is currently unavailable, advise the
        # client now that the caller is authenticated. The chat still
        # proceeds: agent paths that rely on graph traversal
        # (generate_function / generate_cypher / entity-relationship
        # retrieval) work without vector search, and the auto-mode
        # selector skips vector retrievers downstream. Only questions
        # that genuinely require a vector lookup return a graceful
        # per-question error through the synthesizer.
        if service_status["embedding_store"]["status"] != "ok":
            try:
                await websocket.send_json({
                    "notice": "vector_search_unavailable",
                    "status": service_status["embedding_store"]["status"],
                    "message": (
                        "Vector search is currently unavailable; graph "
                        "traversal questions still work and the service "
                        "will recover automatically."
                    ),
                })
            except Exception:
                pass
        # Extract the authenticated username for trace-log ownership
        # tracking. For sentinel logins (API token / secret) this is
        # the sentinel itself; we resolve to the real TG identity below.
        usr_creds = _parse_auth_header(usr_auth)
        ws_is_superadmin = False
        try:
            ws_global_roles, _, ws_username = _get_user_role_details(
                usr_creds.username, usr_creds.password
            )
            ws_is_superadmin = _is_superadmin(ws_global_roles)
        except Exception:
            ws_username = usr_creds.username
        ws_username = ws_username or usr_creds.username
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

    # Get RAG pattern; default "auto" lets RetrieverSelector pick.
    rag_pattern = rag_pattern or "auto"

    # Get conversation ID
    try:
        conversation_id = await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected before conversation ID received")
        return
    logger.info(
        f"WebSocket conversation_id received: {conversation_id or 'empty'} "
        f"(graph={graphname}, mode={mode or 'config'}, selection={rag_pattern})"
    )
    
    # Load conversation history if not a new conversation
    conversation_history = await load_conversation_history(conversation_id, usr_creds)
    
    # Use provided conversation ID or generate new one
    if conversation_id == "new" or not conversation_id:
        convo_id = str(uuid.uuid4())
        LogWriter.info(f"Starting new conversation with ID: {convo_id}")
    else:
        convo_id = conversation_id
        LogWriter.info(f"Continuing conversation with ID: {convo_id}")

    # Send conversation ID to frontend
    await websocket.send_text(json.dumps({"conversation_id": convo_id}))

    # create agent from the menu selection (engine + style/retriever)
    agent = _chat_agent(graphname, conn, use_cypher, mode, rag_pattern, ws=websocket)
    # If Agent mode was requested but the model can't tool-call, make_agent
    # downgraded to the Classic engine — tell the user once.
    if getattr(agent, "engine_note", None):
        await websocket.send_text(json.dumps({"system_note": agent.engine_note}))

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
            await write_message_to_history(message, usr_creds)
            prev_id = message.message_id

            # generate response and keep track of response time
            start = time.monotonic()
            resp = await run_agent(
                agent, data, conversation_history, graphname, websocket,
                is_superadmin=ws_is_superadmin,
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
            await write_message_to_history(message, usr_creds)
            await asyncio.to_thread(_save_trace_log, message.message_id, convo_id, data, resp, elapsed, ws_username)
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

@router.get(route_prefix + "/{graphname}/upload_status")
async def get_upload_status(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
):
    """Report whether a long-running upload/ingest operation is currently
    holding the graph's lock. The Document Ingestion dialog polls this
    on mount and during its lifetime so the Ingest button reflects
    server-side state even after the dialog is closed and reopened.

    Response::

        {
          "graphname": str,
          "processing": bool,
          "operation": "create_ingest" | "ingest" | "upload_files" |
                       "schema_extraction" | "rebuild" | null
        }
    """
    op = get_current_operation(graphname)
    # The rebuild lock is a separate (global) lock — surface it under the
    # same flag so the UI doesn't need a second endpoint.
    if op is None and get_rebuilding_graph() == graphname:
        op = "rebuild"
    return {
        "graphname": graphname,
        "processing": op is not None,
        "operation": op,
    }


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


@router.post(route_prefix + "/{graphname}/uploads/check")
async def check_upload_conflicts(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    payload: Annotated[dict, Body(...)],
):
    """
    Pre-flight a planned upload: given a list of filenames, return which
    ones already exist for ``graphname``. The client can then prompt the
    user once and resend the actual bytes with ``overwrite=true`` or
    ``skip=<filename,...>`` — so a conflict response doesn't waste the
    upload bandwidth.

    Body:
        ``{"filenames": ["report.pdf", "transactions.csv", ...]}``
    Response:
        ``{"conflicts": ["report.pdf"]}``
    """
    requested = payload.get("filenames") or []
    if not isinstance(requested, list):
        raise HTTPException(
            status_code=400, detail="'filenames' must be a list of strings.",
        )

    upload_dir = os.path.join("uploads", graphname)
    if not os.path.isdir(upload_dir):
        return {"graphname": graphname, "conflicts": []}

    conflicts = []
    for name in requested:
        if not isinstance(name, str) or not name:
            continue
        safe_name = os.path.basename(name)
        if os.path.exists(os.path.join(upload_dir, safe_name)):
            conflicts.append(safe_name)
    return {"graphname": graphname, "conflicts": conflicts}


@router.post(route_prefix + "/{graphname}/uploads")
async def upload_files(
    graphname: ValidGraphName,
    creds: Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)],
    files: list[UploadFile] = File(...),
    overwrite: bool = False,
    skip: str | None = None,
):
    """
    Upload one or multiple files for a specific graphname.
    Files are stored in uploads/{graphname}/ directory.

    Parameters:
    - graphname: The graph name to associate files with
    - files: List of files to upload
    - overwrite: If False (default), will reject if any non-skipped file
      already exists (all-or-nothing conflict response). If True, replace
      existing files and drop their cached JSONLs so the next ingest
      re-converts the new bytes.
    - skip: Optional comma-separated list of filenames to silently drop
      from the upload set. Used after the client prompts the user on a
      pre-flight conflict and the user chose "skip" for a subset of
      files.

    Pre-flight via ``POST /uploads/check`` to avoid re-uploading bytes
    when a collision is hit.
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
        temp_folder = os.path.join("uploads", "ingestion_temp", graphname)

        skip_set: set[str] = set()
        if skip:
            skip_set = {
                os.path.basename(s.strip()) for s in skip.split(",") if s.strip()
            }
        accepted = [
            f for f in files
            if os.path.basename(f.filename or "") not in skip_set
        ]

        # Check for existing files if overwrite is False
        if not overwrite:
            existing_files = []
            for file in accepted:
                file_path = os.path.join(
                    upload_dir, os.path.basename(file.filename or "")
                )
                if os.path.exists(file_path):
                    existing_files.append(os.path.basename(file.filename or ""))

            if existing_files:
                return {
                    "status": "conflict",
                    "message": (
                        "Some files already exist. Resend with overwrite=true "
                        "to replace them, or with skip=<filename,...> to drop "
                        "specific files from the upload set."
                    ),
                    "existing_files": existing_files,
                }

        # Save uploaded files
        uploaded_files = []
        total_size = 0

        for file in accepted:
            safe_name = os.path.basename(file.filename or "")
            file_path = os.path.join(upload_dir, safe_name)

            # On overwrite, drop the cached JSONL so the next ingest
            # re-converts the new bytes instead of silently reusing the
            # stale extract.
            if overwrite and os.path.isdir(temp_folder):
                stem = os.path.splitext(safe_name)[0]
                cached_jsonl = os.path.join(temp_folder, f"{stem}.jsonl")
                if os.path.exists(cached_jsonl):
                    try:
                        os.remove(cached_jsonl)
                    except OSError as exc:
                        logger.warning(
                            f"Could not remove cached jsonl {cached_jsonl}: {exc}"
                        )

            # Write file to disk
            with open(file_path, "wb") as f:
                content = await file.read()
                f.write(content)
                file_size = len(content)
                total_size += file_size

            uploaded_files.append({
                "filename": safe_name,
                "size": file_size,
                "path": file_path,
            })

            logger.info(f"Uploaded file {safe_name} ({file_size} bytes) for graph {graphname}")

        return {
            "status": "success",
            "message": f"Successfully uploaded {len(uploaded_files)} file(s)",
            "graphname": graphname,
            "uploaded_files": uploaded_files,
            "skipped_files": sorted(skip_set),
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    llm_config_data: dict = Body(...)
):
    """
    Save LLM configuration and reload services.
    """
    try:
        graphname = llm_config_data.get("graphname")
        llm_access_mode = _resolve_llm_config_access(credentials, graphname)
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = _ecc_auth_header(credentials)
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
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

    # Normalize prompt_path: promote from completion_service to top
    # level, strip per-service copies if identical (same pattern as
    # auth / region). The UI doesn't expose per-service prompt_paths;
    # in practice all services share the completion value.
    if "prompt_path" not in llm_config_data:
        completion_svc = llm_config_data.get("completion_service")
        if isinstance(completion_svc, dict) and "prompt_path" in completion_svc:
            llm_config_data["prompt_path"] = completion_svc["prompt_path"]

    top_prompt_path = llm_config_data.get("prompt_path")
    if top_prompt_path:
        # Embedding excluded — embedding services never use prompt_path.
        for svc_key in ["completion_service", "multimodal_service", "chat_service"]:
            svc = llm_config_data.get(svc_key)
            if isinstance(svc, dict) and svc.get("prompt_path") == top_prompt_path:
                del svc["prompt_path"]
        # If embedding_service somehow has a prompt_path on disk, strip
        # it — it's never read.
        emb = llm_config_data.get("embedding_service")
        if isinstance(emb, dict) and "prompt_path" in emb:
            del emb["prompt_path"]

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


@router.get(f"{route_prefix}/chat_capabilities")
async def get_chat_capabilities(
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    graphname: str | None = None,
):
    """Tool-calling / thinking capability of the resolved chat model.

    Lets the UI warn when the agentic engine is unavailable (the model can't
    tool-call) and disable the Agentic options in the chat menu.
    """
    from common.config import get_chat_config
    from common.llm_services.capabilities import model_capabilities

    caps = model_capabilities(get_chat_config(graphname))
    return {
        "supports_tool_calling": caps["supports_tool_calling"],
        "supports_thinking": caps["supports_thinking"],
        "agentic_available": caps["supports_tool_calling"],
    }


@router.get(f"{route_prefix}/config")
async def get_config(
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    db_test_config: dict = Body(...)
):
    """
    Test database connection with provided credentials from UI.

    Also probes vector-search capability (TigerGraph version and whether
    the ``gds.vector`` package is installed) so the operator knows
    upfront whether saving this configuration will yield a working
    vector store, not just a reachable database.
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

        # listGraphs() exercises the credentials; pyTigerGraph mints a
        # REST++ token on demand if the instance requires one.
        test_conn.listGraphs()

        # Vector capability probe — separate from the auth/reachability
        # check so version / GDS results report independently.
        # Hard requirement is TG version >= 4.2; the GDS package is
        # installed automatically by the embedding-store init on first
        # use if missing, so its absence is informational, not a
        # failure.
        tg_version = ""
        vector_supported = False
        vector_details = ""
        try:
            tg_version = str(test_conn.getVer())
            ver_parts = tg_version.split(".")
            major = int(ver_parts[0]) if ver_parts and ver_parts[0].isdigit() else 0
            minor = int(ver_parts[1]) if len(ver_parts) > 1 and ver_parts[1].isdigit() else 0
            if major < 4 or (major == 4 and minor < 2):
                vector_supported = False
                vector_details = (
                    f"TigerGraph {tg_version} does not support vector search "
                    "(4.2 or later required)."
                )
            else:
                vector_supported = True
                try:
                    sub_packages = test_conn.gsql("SHOW PACKAGE gds")
                except Exception:
                    sub_packages = ""
                if "- vector" in str(sub_packages):
                    vector_details = "GDS installed."
                else:
                    vector_details = (
                        "GDS will be installed automatically on first init "
                        "(may take a few minutes)."
                    )
        except Exception as vec_err:
            vector_details = f"Vector capability probe failed: {vec_err}"

        return {
            "status": "success",
            "message": "Connection successful",
            "tg_version": tg_version,
            "vector_supported": vector_supported,
            "vector_details": vector_details,
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    db_config_data: dict = Body(...)
):
    """
    Save GraphDB configuration to server_config.json.
    """
    try:
        _require_roles(credentials, {"superuser"})
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = _ecc_auth_header(credentials)
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
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    graphrag_config_data: dict = Body(...)
):
    """
    Save GraphRAG configuration.
    scope=graph saves per-graph overrides; default saves to global config.
    """
    try:
        _require_roles(credentials, {"superuser", "globaldesigner"})
        graphs = auth(credentials.username, credentials.password)[0]
        auth_header = _ecc_auth_header(credentials)
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


#: Per-prompt-type list of regex patterns that mark the start of the
#: placeholder-variables block. The first matching pattern wins.
#: Patterns are tried in order so the canonical Markdown headers
#: (``## Inputs`` / ``## Data``) match first; additional patterns
#: are kept as fallbacks for files saved under earlier formats.
_TEMPLATE_VAR_MARKERS = {
    "chatbot_response": [
        r'(?ms)^##\s*Inputs\b.*$',
        r'(?ms)^Question:\s*\{question\}.*$',
    ],
    "entity_relationship": [
        # No placeholders in the entity-relationship system prompt.
        # The whole content is editable.
    ],
    "community_summarization": [
        r'(?ms)^##\s*Data\b.*$',
        r'(?ms)^##\s*Inputs\b.*$',
        r'(?ms)^#######\s*-Data-.*$',
    ],
    "query_generation": [
        # ``{query_guidance}`` is a runtime-supplied partial — the user
        # must not be able to delete it from the editable body, so the
        # template-variables block starts at that placeholder line.
        r'(?m)^\{query_guidance\}\s*$',
        r'(?ms)^##\s*Inputs\b.*$',
        r'(?ms)^\{format_instructions\}.*$',
    ],
    "schema_extraction": [
        r'(?ms)^##\s*Inputs\b.*$',
    ],
}


def split_prompt_template(prompt_content: str, prompt_type: str) -> dict:
    """Split a prompt into editable prose and the trailing placeholder
    block that users should not modify.

    The placeholder block — everything from a canonical marker to end
    of file — is preserved verbatim so the saved file always renders
    with the original ``{placeholder}`` set even when the user's edit
    inadvertently removes them from the prose. POST ``/prompts``
    re-concatenates ``editable_content + "\\n\\n" + template_variables``
    on save.

    Returns ``{"editable_content": str, "template_variables": str}``.
    """
    for pattern in _TEMPLATE_VAR_MARKERS.get(prompt_type, []):
        match = re.search(pattern, prompt_content)
        if match:
            template_vars = prompt_content[match.start():].strip()
            editable = prompt_content[:match.start()].rstrip()
            return {
                "editable_content": editable,
                "template_variables": template_vars,
            }
    return {"editable_content": prompt_content, "template_variables": ""}


@router.get(f"{route_prefix}/prompts")
async def get_prompts(
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    graphname: str | None = None,
):
    """
    Get all customizable prompts.
    Returns chatbot_response, entity_relationship, community_summarization, and query_generation prompts.
    """
    try:
        access_level = _require_prompt_access(credentials, graphname)
        chat_cfg = dict(get_chat_config(graphname))
        completion_cfg = dict(get_completion_config(graphname))
        if graphname:
            chat_cfg["graphname"] = graphname
            completion_cfg["graphname"] = graphname

        # ``chatbot_response`` and ``schema_extraction`` are consumed
        # by the chat agent and the schema-extraction LLM call, both of
        # which run through the chat service. Every other prompt is
        # consumed by completion-side code paths (entity / relationship
        # extraction, community summarization, schema mapping) and
        # resolves through the completion service's ``prompt_path``.
        # When no ``chat_service`` is configured, ``get_chat_config``
        # already falls back to ``completion_service`` so this routing
        # stays correct for single-service deployments.
        chat_llm = get_llm_service(chat_cfg)
        completion_llm = get_llm_service(completion_cfg)

        # Each entry: (LLM service, base_llm property name). The
        # property's resolution chain is graph-override →
        # ``prompt_path`` file → hardcoded default in base_llm.py, so
        # this single delegation gives the editor the right text in
        # every case.
        _PROMPT_SOURCE = {
            "chatbot_response":
                (chat_llm, "chatbot_response_prompt"),
            "entity_relationship":
                (completion_llm, "entity_relationship_extraction_prompt"),
            "community_summarization":
                (completion_llm, "community_summarize_prompt"),
            "query_generation":
                (completion_llm, "map_question_schema_prompt"),
            "schema_extraction":
                (chat_llm, "schema_extraction_prompt"),
            # Free-form partial injected into the four query-related
            # templates (map_question_to_schema, generate_function,
            # generate_cypher, generate_gsql). Empty by default.
            "query_guidance":
                (completion_llm, "query_guidance_prompt"),
            # Agentic (react) agent system prompt — runs through the chat service.
            "agentic_agent":
                (chat_llm, "agentic_agent_prompt"),
            # Agentic planner system prompt — runs through the chat service.
            "agentic_planner":
                (chat_llm, "agentic_planner_prompt"),
            # Front-desk triage / routing gate — runs through the chat service.
            "agentic_triage":
                (chat_llm, "agentic_triage_prompt"),
        }

        # Split prompts expose ONLY the user portion; the system prompt (rules
        # + runtime placeholders) is hardcoded in base_llm and never returned.
        _SPLIT_FILE = {
            "chatbot_response": "chatbot_response.txt",
            "entity_relationship": "entity_relationship_extraction.txt",
            "community_summarization": "community_summarization.txt",
            "schema_extraction": "schema_extraction.txt",
            "agentic_agent": "agentic_agent.txt",
            "agentic_planner": "agentic_planner.txt",
            "agentic_triage": "agentic_triage.txt",
        }

        def _get_prompt(prompt_type: str) -> dict:
            svc, prop = _PROMPT_SOURCE[prompt_type]
            if prompt_type in _SPLIT_FILE:
                try:
                    return {
                        "editable_content": svc.get_user_portion(
                            _SPLIT_FILE[prompt_type]
                        )
                    }
                except Exception as exc:
                    logger.warning(
                        f"Falling back to empty user portion for {prompt_type}: {exc}"
                    )
                    return {"editable_content": ""}
            # Non-split (query_generation, query_guidance): legacy full-template.
            try:
                text = getattr(svc, prop, "") or ""
            except Exception as exc:
                logger.warning(
                    f"Falling back to empty content for {prompt_type}: {exc}"
                )
                text = ""
            if not text:
                return {"editable_content": "", "template_variables": ""}
            return split_prompt_template(text, prompt_type)

        prompts = {pt: _get_prompt(pt) for pt in _PROMPT_SOURCE}

        default_prompt_path = chat_cfg.get(
            "prompt_path", "./common/prompts/openai_gpt4/"
        )
        if default_prompt_path.startswith("./"):
            default_prompt_path = default_prompt_path[2:]
        default_prompt_path = default_prompt_path.rstrip("/")

        # Graph-admin (chatbot_only) only sees chatbot_response
        if access_level == "chatbot_only":
            prompts = {"chatbot_response": prompts.get("chatbot_response", {"editable_content": ""})}

        return {
            "prompts": prompts,
            "prompt_path": default_prompt_path,
            "configured_provider": chat_cfg.get("llm_service", "openai"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching prompts: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch prompts: {str(e)}")


@router.post(f"{route_prefix}/prompts")
async def save_prompts(
    credentials: Annotated[HTTPBasicCredentials, Depends(ui_creds)],
    prompt_data: dict = Body(...)
):
    """
    Save customized prompts.
    Expects: {
        "prompt_type": "chatbot_response|entity_relationship|community_summarization|query_generation|schema_extraction|query_guidance",
        "editable_content": "...",
        "graphname": "..."  (optional - graph-admin users must supply this)
    }
    For split prompts ``editable_content`` is the user portion only; the system
    rules are hardcoded and never accepted here.
    """
    try:
        graphname = prompt_data.get("graphname")
        access_level = _require_prompt_access(credentials, graphname)
        prompt_type = prompt_data.get("prompt_type")

        # Graph-admin (chatbot_only) can only edit chatbot_response prompt
        if access_level == "chatbot_only" and prompt_type != "chatbot_response":
            raise HTTPException(status_code=403, detail="Graph admins can only edit the chatbot response prompt.")
        editable_content = prompt_data.get("editable_content")
        if editable_content is None:
            editable_content = prompt_data.get("content")

        if not prompt_type:
            raise HTTPException(status_code=400, detail="prompt_type is required")

        # ``template_variables`` is obsolete under the system/user split — the
        # saved file is the user portion only. An empty user portion is valid
        # for split prompts (reverts to the default, no additional instructions);
        # non-split prompts fail the required-placeholder check below.
        content = editable_content or ""

        if graphname:
            # Per-graph: only write the single customized prompt file to the override dir.
            # Non-customized prompts fall back to the global prompt_path at runtime.
            graph_prompt_dir = f"configs/graph_configs/{graphname}/prompts"
            os.makedirs(graph_prompt_dir, exist_ok=True)
            prompt_path = graph_prompt_dir
        else:
            # Global: route writes to the persistent override dir
            # ``configs/prompts/`` so user edits survive container
            # restarts. The dir starts empty — base_llm.py serves the
            # hardcoded default for every prompt the user hasn't
            # touched.
            #
            # ``prompt_path`` lives at the top level of ``llm_config``
            # and is injected into every service that doesn't override
            # it (mirrors the ``authentication_configuration`` /
            # ``region_name`` pattern). One write here suffices for
            # every consumer (chatbot_response via chat_service,
            # entity_relationship / schema_extraction via
            # completion_service, multimodal via multimodal_service).
            persistent_prompt_dir = "configs/prompts"
            os.makedirs(persistent_prompt_dir, exist_ok=True)
            new_path = f"./{persistent_prompt_dir}/"

            from common.config import reload_llm_config, _config_file_lock, SERVER_CONFIG
            # Acquire the lock to read-modify-write the server config,
            # then RELEASE before calling ``reload_llm_config()`` —
            # reload acquires the same lock internally, so calling it
            # while held would deadlock.
            changed = False
            with _config_file_lock:
                with open(SERVER_CONFIG, "r") as f:
                    server_cfg = json.load(f)
                llm_cfg = server_cfg.setdefault("llm_config", {})
                if (llm_cfg.get("prompt_path") or "").rstrip("/") != new_path.rstrip("/"):
                    llm_cfg["prompt_path"] = new_path
                    changed = True
                # Strip per-service copies — they're redundant once
                # the top-level field is set. Keeps the config clean
                # and avoids stale per-service entries shadowing the
                # global value. ``embedding_service`` is included
                # only to scrub stray entries; embedding models never
                # read prompt_path.
                for svc_key in (
                    "completion_service",
                    "chat_service",
                    "multimodal_service",
                    "embedding_service",
                ):
                    svc = llm_cfg.get(svc_key)
                    if isinstance(svc, dict) and "prompt_path" in svc:
                        del svc["prompt_path"]
                        changed = True
                if changed:
                    temp_file = f"{SERVER_CONFIG}.tmp"
                    with open(temp_file, "w") as f:
                        json.dump(server_cfg, f, indent=2)
                    os.replace(temp_file, SERVER_CONFIG)
            if changed:
                reload_llm_config()
            prompt_path = persistent_prompt_dir

        prompt_type_to_file = {
            "chatbot_response": "chatbot_response.txt",
            "entity_relationship": "entity_relationship_extraction.txt",
            "community_summarization": "community_summarization.txt",
            "query_generation": "map_question_to_schema.txt",
            "schema_extraction": "schema_extraction.txt",
            "query_guidance": "query_guidance.txt",
            "agentic_agent": "agentic_agent.txt",
            "agentic_planner": "agentic_planner.txt",
            "agentic_triage": "agentic_triage.txt",
        }

        if prompt_type not in prompt_type_to_file:
            raise HTTPException(status_code=400, detail=f"Invalid prompt_type: {prompt_type}")

        from common.utils.prompt_validation import (
            validate_and_escape_prompt,
            sanitize_user_portion,
            find_placeholders,
            SPLIT_PROMPT_TYPES,
        )

        # Hard length cap on user-portion prompts (split prompts + the
        # free-form Query Guidance partial). Runaway content can push the
        # surrounding hardcoded prompt past the LLM's context window. 8000
        # chars ≈ 2K tokens is plenty for instructions + a half-dozen examples.
        USER_PORTION_MAX_CHARS = 8000
        if (
            prompt_type in SPLIT_PROMPT_TYPES or prompt_type == "query_guidance"
        ) and len(content) > USER_PORTION_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Prompt is too long ({len(content)} characters); "
                    f"keep it under {USER_PORTION_MAX_CHARS}."
                ),
            )

        removed_placeholders: list = []
        if prompt_type in SPLIT_PROMPT_TYPES:
            # Split prompt: the saved file is the user portion only. Detect any
            # placeholder-style ``{token}`` first (to report back to the user),
            # then strip them — the system prompt owns all runtime placeholders.
            removed_placeholders = find_placeholders(content)
            content = sanitize_user_portion(content)
        else:
            # Non-split (query_generation full template, query_guidance): escape
            # stray ``{token}`` occurrences and reject missing required placeholders.
            content, missing = validate_and_escape_prompt(content, prompt_type)
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Prompt is missing required placeholders: "
                        + ", ".join("{" + m + "}" for m in missing)
                        + ". Add them to the prompt before saving."
                    ),
                )

        file_path = os.path.join(prompt_path, prompt_type_to_file[prompt_type])
        # For a split prompt, an empty user portion means "revert to the shipped
        # default" — remove the override file so the built-in default user
        # portion is served, rather than persisting an empty file that would
        # shadow it.
        if prompt_type in SPLIT_PROMPT_TYPES and not content.strip():
            if os.path.exists(file_path):
                os.remove(file_path)
        else:
            temp_file = f"{file_path}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(temp_file, file_path)

        messages = {
            "chatbot_response": "Chatbot response prompt saved successfully",
            "entity_relationship": "Entity relationship prompt saved successfully",
            "community_summarization": "Community summarization prompt saved successfully",
            "query_generation": "Question-to-schema mapping prompt saved successfully",
            "schema_extraction": "Schema extraction prompt saved successfully",
            "query_guidance": "Query guidance saved successfully",
        }
        resp = {"status": "success", "message": messages.get(prompt_type, "Prompt saved successfully")}
        # Heads-up (non-blocking) for split prompts: (1) which placeholder tokens
        # were removed, and (2) an LLM check for lines that try to override the
        # fixed system rules. The save still succeeds — the rules win at answer
        # time — so the UI can warn and offer the cleaned text.
        if prompt_type in SPLIT_PROMPT_TYPES:
            if removed_placeholders:
                resp["removed_placeholders"] = removed_placeholders
            if content.strip():
                try:
                    review_svc = get_llm_service(get_chat_config(graphname))
                    review = await asyncio.to_thread(
                        review_svc.review_user_portion_llm,
                        prompt_type_to_file[prompt_type],
                        content,
                    )
                    if review.get("has_conflict"):
                        resp["review"] = review
                except Exception as exc:
                    logger.warning(f"prompt conflict review failed: {exc}")
        return resp

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save prompt: {str(e)}")