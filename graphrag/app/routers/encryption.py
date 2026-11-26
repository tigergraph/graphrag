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

"""Admin API of the data encryption keys (superuser and globaldesigner;
downloading a key is superuser only). Every action, also a refused one, goes
to the audit log. Keys never appear in a response except in the download."""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

import data_encryption
from common.config import validate_graphname
from data_encryption.key_manager import NeedsPreviousKey, _audit
from routers.ui import _get_user_role_details, _require_roles, ui_basic_auth

router = APIRouter(tags=["Encryption"])
PREFIX = "/ui/encryption"
ADMIN_ROLES = {"superuser", "globaldesigner"}
CHAT = "chat_history"

Creds = Annotated[tuple[list[str], HTTPBasicCredentials], Depends(ui_basic_auth)]


class GraphBody(BaseModel):
    graphname: Optional[str] = None


class KeyBody(GraphBody):
    key: str


class RotateBody(GraphBody):
    key: Optional[str] = None               # managed key: use this key instead of a new one
    previous_key: Optional[str] = None      # mounted key: the key it replaces


class OwnKeyBody(GraphBody):
    key: Optional[str] = None               # omit to generate one


class TargetBody(BaseModel):
    target: str                             # a graph name, "chat_history" or "all"


class ResetBody(TargetBody):
    confirm: str                            # must repeat the target


class DeleteMemoryBody(BaseModel):
    graphs: list[str]
    confirm: str                            # the graph name, or "delete" for several


def _graph(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    try:
        return validate_graphname(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid graph name")


def _target(name: str) -> str:
    return data_encryption.CHAT_TARGET if name == CHAT else _graph(name)


def _actor(creds: HTTPBasicCredentials) -> str:
    try:
        return _get_user_role_details(creds.username, creds.password)[2] or creds.username
    except Exception:
        return creds.username


async def _run(creds, roles, action: str, graph: Optional[str], fn, *args):
    user = await asyncio.to_thread(_actor, creds[1])
    try:
        await asyncio.to_thread(_require_roles, creds[1], roles)
    except HTTPException:
        _audit(action, user, graph, "rejected", detail="insufficient permissions")
        raise
    try:
        return await asyncio.to_thread(fn, *args, user)
    except NeedsPreviousKey as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc),
                                                     "needed_key_ids": exc.needed})
    except data_encryption.Rejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except data_encryption.Locked as exc:
        raise HTTPException(status_code=423, detail=f"The data is locked ({exc.reason}).")


def _deep_agent(graph: str) -> bool:
    """Whether the graph runs Agent mode on the Deep Agent, the only engine
    that keeps agent memory."""
    from common.config import get_graphrag_config
    from deep_agent.settings import use_deep_agent
    try:
        return use_deep_agent(get_graphrag_config(graph))
    except Exception:
        return True


def _require_deep_agent(graph: Optional[str]) -> None:
    if graph and not _deep_agent(graph):
        raise data_encryption.Rejected("This graph does not use the Deep Agent.")


def _with_deep_agent(status: dict) -> dict:
    if "target" in status:
        status["deep_agent"] = _deep_agent(status["graph"])
    for t in status.get("targets", []):
        if t["target"] != CHAT:
            t["deep_agent"] = _deep_agent(t["target"])
    return status


def _live_graphs() -> list:
    """The graphs that exist in TigerGraph (GraphRAG's own database user)."""
    from common.config import db_config
    from routers.ui import auth
    return auth(db_config["username"], db_config["password"])[0]


@router.get(PREFIX + "/status")
async def status(creds: Creds, graphname: Optional[str] = None):
    await asyncio.to_thread(_require_roles, creds[1], ADMIN_ROLES)
    try:
        live = await asyncio.to_thread(_live_graphs)
    except Exception:
        live = None                         # removed graphs are then not marked
    m = data_encryption.manager()
    graph = _graph(graphname)

    def build():
        st = _with_deep_agent(m.status(graph, live))
        if graph:
            st["stored"] = any(t["target"] == graph for t in m.status()["targets"])
        return st
    return await asyncio.to_thread(build)


@router.post(PREFIX + "/delete_memory")
async def delete_memory(body: DeleteMemoryBody, creds: Creds):
    graphs = [_graph(g) for g in body.graphs if g]
    if not graphs:
        raise HTTPException(status_code=400, detail="No graph given.")
    expected = graphs[0] if len(graphs) == 1 else "delete"
    if body.confirm != expected:
        raise HTTPException(status_code=400, detail="Type the confirmation to continue.")
    m = data_encryption.manager()

    def delete(user):
        live = _live_graphs()               # fails closed: nothing is deleted on error
        for g in graphs:
            m.delete_memory(g, live, user)
        return m.status(None, live)
    return await _run(creds, ADMIN_ROLES, "delete_memory", None, delete)


@router.post(PREFIX + "/rotate_master")
async def rotate_master(body: RotateBody, creds: Creds):
    m = data_encryption.manager()
    graph = _graph(body.graphname)
    def rotate(user):
        _require_deep_agent(graph)
        return m.rotate_master(graph, body.key, body.previous_key, user)
    return await _run(creds, ADMIN_ROLES, "rotate_master", graph, rotate)


@router.post(PREFIX + "/key")
async def upload_key(body: KeyBody, creds: Creds):
    m = data_encryption.manager()
    graph = _graph(body.graphname)
    def upload(user):
        _require_deep_agent(graph)
        return m.upload_key(body.key, graph, user)
    return await _run(creds, ADMIN_ROLES, "upload", graph, upload)


@router.post(PREFIX + "/graph_key")
async def use_graph_key(body: OwnKeyBody, creds: Creds):
    graph = _graph(body.graphname)
    if not graph:
        raise HTTPException(status_code=400, detail="graphname is required")
    m = data_encryption.manager()
    def use(user):
        _require_deep_agent(graph)
        return m.use_graph_key(graph, body.key, user)
    return await _run(creds, ADMIN_ROLES, "use_graph_key", graph, use)


@router.post(PREFIX + "/global_key")
async def use_global_key(body: GraphBody, creds: Creds):
    graph = _graph(body.graphname)
    if not graph:
        raise HTTPException(status_code=400, detail="graphname is required")
    m = data_encryption.manager()
    def use(user):
        _require_deep_agent(graph)
        return m.use_global_key(graph, user)
    return await _run(creds, ADMIN_ROLES, "use_global_key", graph, use)


@router.get(PREFIX + "/key/download")
async def download_key(creds: Creds, graphname: Optional[str] = None):
    m = data_encryption.manager()
    graph = _graph(graphname)
    return await _run(creds, {"superuser"}, "download", graph,
                      lambda user: m.download(graph, user))


@router.post(PREFIX + "/rotate_data_keys")
async def rotate_data_keys(body: TargetBody, creds: Creds):
    m = data_encryption.manager()
    if body.target == "all":
        def rotate_all(user):
            skipped = []
            for t in m.status()["targets"]:
                if t["state"] == "locked" or (t["target"] != CHAT and not _deep_agent(t["target"])):
                    skipped.append(t["target"])
                    continue
                m.rotate_data_key(_target(t["target"]), user)
            return {**m.status(), "skipped_locked": skipped}
        return await _run(creds, ADMIN_ROLES, "rotate_data_key", None, rotate_all)
    target = _target(body.target)
    def rotate(user):
        if body.target != CHAT:
            _require_deep_agent(target)
        return m.rotate_data_key(target, user)
    return await _run(creds, ADMIN_ROLES, "rotate_data_key", body.target, rotate)


@router.post(PREFIX + "/reset")
async def reset(body: ResetBody, creds: Creds):
    if body.confirm != body.target:
        raise HTTPException(status_code=400, detail="Type the name to confirm.")
    target = _target(body.target)
    m = data_encryption.manager()
    return await _run(creds, ADMIN_ROLES, "reset", body.target,
                      lambda user: m.reset(target, user))
