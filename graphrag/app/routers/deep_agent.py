# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""REST API of the Deep Agent.

All routes act as the caller (HTTP Basic or token, resolved by the auth
middleware) on the graph in the path. Conversations (STM threads) and
memories (LTM) are private to the caller's user on that graph; they need
password (Basic) authentication, since a token does not identify a user.

* ``POST   /{graph}/agent/chat``                    ask; continues ``conversation_id``
* ``GET    /{graph}/agent/tools``                   tools the agent can use
* ``GET    /{graph}/agent/threads``                 the caller's conversations
* ``GET    /{graph}/agent/threads/{conversation}``  messages of one conversation
* ``DELETE /{graph}/agent/threads/{conversation}``  forget one conversation
* ``GET    /{graph}/agent/memories``                the caller's long-term memories
* ``GET    /{graph}/agent/memories/{path}``         read one memory file
* ``PUT    /{graph}/agent/memories/{path}``         write one memory file
* ``DELETE /{graph}/agent/memories/{path}``         delete one memory file
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from base64 import b64decode
from types import SimpleNamespace
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security.http import HTTPBase
from pydantic import BaseModel, Field

from common.logs.log import req_id_cv

logger = logging.getLogger(__name__)

router = APIRouter(tags=["DeepAgent"])
security = HTTPBase(scheme="basic", auto_error=False)
use_cypher = os.getenv("USE_CYPHER", "false").lower() == "true"


class AgentChatRequest(BaseModel):
    query: str
    conversation_id: Optional[str] = Field(
        default=None, description="Continue this conversation; omit to start a new one.")
    include_fields: Optional[List[str]] = Field(
        default=None, description="Add 'query_sources' (or 'all') to include the trace.")


class AgentChatResponse(BaseModel):
    conversation_id: Optional[str]
    natural_language_response: str
    answered_question: bool
    response_type: str
    query_sources: Optional[dict] = None


class MemoryWrite(BaseModel):
    content: str


def _access(request: Request, graphname: str):
    """The caller's verified connection and user name (None for tokens)."""
    from routers.inquiryai import _require_graph_access
    conn = _require_graph_access(request, graphname)
    user = None
    auth = request.headers.get("Authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "basic" and value:
        from deep_agent.agent import _usable_user
        try:
            user = _usable_user(b64decode(value).decode().split(":", 1)[0])
        except Exception:
            user = None
    return conn, user


def _require_user(user: Optional[str]) -> str:
    if not user:
        raise HTTPException(
            status_code=400,
            detail="Conversations and memories are kept per user and need Basic authentication.",
        )
    return user


@router.post("/{graphname}/agent/chat")
async def agent_chat(
    graphname: str,
    body: AgentChatRequest,
    request: Request,
    credentials: Annotated[HTTPBase, Depends(security)],
) -> AgentChatResponse:
    from routers.inquiryai import _caller_is_superadmin, check_embedding_store_status
    from deep_agent import DeepGraphRAGAgent
    from common.config import get_chat_config, get_embedding_service, get_embedding_store, get_llm_service

    conn, user = _access(request, graphname)
    check_embedding_store_status()
    conversation_id = (body.conversation_id or "").strip() or (str(uuid.uuid4()) if user else None)
    llm = get_llm_service(get_chat_config(graphname))
    agent = DeepGraphRAGAgent(
        llm, conn, get_embedding_service(), get_embedding_store(),
        use_cypher=use_cypher, conversation_id=conversation_id, user=user or "",
    )
    try:
        resp = await asyncio.to_thread(agent.question_for_agent, body.query)
    except Exception as e:
        from routers.ui import _agent_error_text
        logger.warning(f"/{graphname}/agent/chat request_id={req_id_cv.get()} failed: {e}")
        logger.debug_pii(
            f"/{graphname}/agent/chat request_id={req_id_cv.get()} Exception Trace:\n{traceback.format_exc()}"
        )
        return AgentChatResponse(
            conversation_id=conversation_id,
            natural_language_response=_agent_error_text(e, _caller_is_superadmin(credentials)),
            answered_question=False, response_type="deep_agent",
        )
    requested = {f.strip().lower() for f in (body.include_fields or []) if f}
    keep_sources = "all" in requested or "query_sources" in requested
    return AgentChatResponse(
        conversation_id=agent.conversation_id,
        natural_language_response=resp.natural_language_response,
        answered_question=resp.answered_question,
        response_type=resp.response_type,
        query_sources=resp.query_sources if keep_sources else None,
    )


@router.get("/{graphname}/agent/tools")
def agent_tools(graphname: str, request: Request,
                credentials: Annotated[HTTPBase, Depends(security)]):
    """Tools the Deep Agent can use on this graph, by family."""
    from common.config import get_graphrag_config
    from deep_agent.settings import agent_settings
    from deep_agent.tools import ToolEnv, build_tools

    conn, _user = _access(request, graphname)
    settings = agent_settings(get_graphrag_config(graphname))
    external = {}
    try:
        from mcp_addons import discover_tools, get_manager, run_sync
        manager = run_sync(get_manager(graphname), timeout=10.0)
        if manager and manager.server_names:
            external = discover_tools(manager)
    except Exception as exc:
        logger.info(f"deep_agent tools: mcp discovery skipped: {exc}")
    env = ToolEnv(graph_ctx=SimpleNamespace(conn=conn),
                  allow_installed_queries=settings.allow_installed_queries,
                  use_cypher=use_cypher)
    groups = build_tools(env, external)
    return {family: [{"name": t.name, "description": t.description,
                      "args": (t.args if isinstance(t.args, dict) else {})}
                     for t in tools]
            for family, tools in groups.items()}


# ----------------------------------------------------------------- threads

def _message_view(m) -> dict:
    from deep_agent.agent import _text
    view = {"type": getattr(m, "type", ""), "content": _text(getattr(m, "content", ""))}
    calls = getattr(m, "tool_calls", None)
    if calls:
        view["tool_calls"] = [{"name": c.get("name"), "args": c.get("args")} for c in calls]
    if getattr(m, "name", None):
        view["name"] = m.name
    return view


@router.get("/{graphname}/agent/threads")
def list_threads(graphname: str, request: Request,
                 credentials: Annotated[HTTPBase, Depends(security)], limit: int = 100):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    return {"threads": mem.list_threads(graphname, _require_user(user), max(1, min(limit, 1000)))}


@router.get("/{graphname}/agent/threads/{conversation_id}")
def get_thread(graphname: str, conversation_id: str, request: Request,
               credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    tid = mem.thread_id(graphname, _require_user(user), conversation_id)
    messages = mem.thread_messages(tid)
    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"conversation_id": conversation_id, "messages": [_message_view(m) for m in messages]}


@router.delete("/{graphname}/agent/threads/{conversation_id}")
def delete_thread(graphname: str, conversation_id: str, request: Request,
                  credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    mem.delete_thread(mem.thread_id(graphname, _require_user(user), conversation_id))
    return {"deleted": conversation_id}


# ---------------------------------------------------------------- memories

@router.get("/{graphname}/agent/memories")
def list_memories(graphname: str, request: Request,
                  credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    return {"memories": mem.list_memories(graphname, _require_user(user))}


@router.get("/{graphname}/agent/memories/{path:path}")
def read_memory(graphname: str, path: str, request: Request,
                credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    try:
        content = mem.read_memory(graphname, _require_user(user), path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"path": "/memories/" + path.lstrip("/").removeprefix("memories/"), "content": content}


@router.put("/{graphname}/agent/memories/{path:path}")
def write_memory(graphname: str, path: str, body: MemoryWrite, request: Request,
                 credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    try:
        written = mem.write_memory(graphname, _require_user(user), path, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": written}


@router.delete("/{graphname}/agent/memories/{path:path}")
def delete_memory(graphname: str, path: str, request: Request,
                  credentials: Annotated[HTTPBase, Depends(security)]):
    from deep_agent import memory as mem
    _conn, user = _access(request, graphname)
    try:
        found = mem.delete_memory(graphname, _require_user(user), path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": path}
