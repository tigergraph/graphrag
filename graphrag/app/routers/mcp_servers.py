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

"""External MCP-server CRUD + test endpoints.

Two scopes mirror the config layout:

  GET  /ui/mcp_servers                       → global list (env/headers redacted)
  POST /ui/mcp_servers                       → replace global list (body: list)
  GET  /ui/{graphname}/mcp_servers           → per-graph overrides (redacted)
  POST /ui/{graphname}/mcp_servers           → replace per-graph list
  GET  /ui/{graphname}/mcp_servers/resolved  → merged effective list for the graph
  POST /ui/mcp_servers/test                  → connect+list_tools for a draft spec

CRUD is whole-list-replace: the UI sends the full edited list. This keeps the
backend lock-free of item-level merge logic and lets multi-edit flows commit
atomically. Sensitive values (``env`` / ``headers``) are masked on GET; POST
substitutes the stored value back in wherever the mask sentinel is sent.

Writes invalidate the cached MCP client managers — active chat sessions will
re-connect lazily on the next planner step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Annotated, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.security import HTTPBasicCredentials

from common.config import (
    SERVER_CONFIG,
    _config_file_lock,
    validate_graphname,
    get_mcp_servers,
)
from common.mcp_config import McpServerSpec, MCP_LIB_DIR, ensure_libraries_installed

logger = logging.getLogger(__name__)

router = APIRouter()
route_prefix = "/ui"

MASKED_SECRET = "********"


# --- Auth shim --------------------------------------------------------------
# Reuse the existing ``ui_creds`` dependency + role gate from ui.py without
# pulling its entire import surface here.

def _ui_creds():
    from routers.ui import ui_creds
    return ui_creds


def _require_roles(credentials: HTTPBasicCredentials, allowed: set[str]) -> list[str]:
    from routers.ui import _require_roles as _impl
    return _impl(credentials, allowed)


# --- Persistence helpers ----------------------------------------------------

def _read_global() -> list:
    with _config_file_lock:
        with open(SERVER_CONFIG, "r") as f:
            cfg = json.load(f)
    return list(cfg.get("mcp_servers") or [])


def _read_pergraph(graphname: str) -> list:
    validate_graphname(graphname)
    path = f"configs/graph_configs/{graphname}/server_config.json"
    if not os.path.exists(path):
        return []
    with _config_file_lock:
        with open(path, "r") as f:
            cfg = json.load(f)
    return list(cfg.get("mcp_servers") or [])


def _write_global(specs: list) -> None:
    with _config_file_lock:
        with open(SERVER_CONFIG, "r") as f:
            cfg = json.load(f)
        if specs:
            cfg["mcp_servers"] = specs
        else:
            cfg.pop("mcp_servers", None)
        tmp = f"{SERVER_CONFIG}.tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, SERVER_CONFIG)


def _write_pergraph(graphname: str, specs: list) -> None:
    validate_graphname(graphname)
    dir_ = f"configs/graph_configs/{graphname}"
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, "server_config.json")
    with _config_file_lock:
        if os.path.exists(path):
            with open(path, "r") as f:
                cfg = json.load(f)
        else:
            cfg = {}
        if specs:
            cfg["mcp_servers"] = specs
        else:
            cfg.pop("mcp_servers", None)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)


async def _invalidate_manager_cache() -> None:
    """Drop all cached MCP client managers so the next request rebuilds
    them from the updated config. In-flight tool calls on the old manager
    keep their connection — only the cache lookup changes.
    """
    try:
        from mcp_addons import shutdown_all, run_async
        await run_async(shutdown_all())
    except Exception as exc:
        logger.warning(f"mcp_servers: failed to invalidate manager cache: {exc}")


# --- Secret redaction -------------------------------------------------------

_SECRET_FIELDS = ("env", "headers")


def _redact_spec(spec: dict) -> dict:
    out = dict(spec)
    for field in _SECRET_FIELDS:
        if isinstance(out.get(field), dict):
            out[field] = {k: MASKED_SECRET for k in out[field].keys()}
    return out


def _unmask_spec(submitted: dict, stored_by_name: dict) -> dict:
    """Replace mask sentinels in ``submitted`` with the corresponding
    value from ``stored_by_name[submitted['name']]``. Used on save so the
    UI can re-submit a spec without re-entering secrets every time.
    """
    name = submitted.get("name")
    prev = stored_by_name.get(name, {}) if name else {}
    out = dict(submitted)
    for field in _SECRET_FIELDS:
        cur = out.get(field)
        if not isinstance(cur, dict):
            continue
        prev_field = prev.get(field) if isinstance(prev.get(field), dict) else {}
        out[field] = {
            k: (prev_field.get(k, "") if v == MASKED_SECRET else v)
            for k, v in cur.items()
        }
    return out


# --- Validation -------------------------------------------------------------

def _validate_specs(specs_raw: list) -> list[dict]:
    """Validate via McpServerSpec; return list[dict] (model_dump) so we
    persist the canonical shape with defaults filled in.
    """
    if not isinstance(specs_raw, list):
        raise HTTPException(status_code=400, detail="mcp_servers must be a list")
    seen: set[str] = set()
    out: list[dict] = []
    for i, raw in enumerate(specs_raw):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"mcp_servers[{i}] must be an object")
        try:
            spec = McpServerSpec(**raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"mcp_servers[{i}]: {exc}")
        if spec.name in seen:
            raise HTTPException(status_code=400, detail=f"duplicate server name {spec.name!r} in list")
        seen.add(spec.name)
        out.append(spec.model_dump())
    return out


# --- Endpoints --------------------------------------------------------------

@router.get(f"{route_prefix}/mcp_servers")
async def list_global_mcp_servers(
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
):
    """Global MCP servers (env/headers masked)."""
    _require_roles(creds, {"superuser"})
    raw = _read_global()
    return {"status": "success", "data": [_redact_spec(s) for s in raw]}


@router.post(f"{route_prefix}/mcp_servers")
async def replace_global_mcp_servers(
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
    body: list = Body(...),
):
    """Replace the entire global MCP server list."""
    _require_roles(creds, {"superuser"})
    stored = {s.get("name"): s for s in _read_global() if isinstance(s, dict)}
    unmasked = [_unmask_spec(s, stored) for s in body]
    canonical = _validate_specs(unmasked)
    _write_global(canonical)
    await _invalidate_manager_cache()
    return {"status": "success", "message": f"saved {len(canonical)} global MCP server(s)"}


@router.get(route_prefix + "/{graphname}/mcp_servers")
async def list_pergraph_mcp_servers(
    graphname: str,
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
):
    """Per-graph MCP-server overrides (env/headers masked)."""
    _require_roles(creds, {"superuser"})
    raw = _read_pergraph(graphname)
    return {"status": "success", "data": [_redact_spec(s) for s in raw]}


@router.post(route_prefix + "/{graphname}/mcp_servers")
async def replace_pergraph_mcp_servers(
    graphname: str,
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
    body: list = Body(...),
):
    """Replace the per-graph MCP-server override list for ``graphname``."""
    _require_roles(creds, {"superuser"})
    stored = {s.get("name"): s for s in _read_pergraph(graphname) if isinstance(s, dict)}
    unmasked = [_unmask_spec(s, stored) for s in body]
    canonical = _validate_specs(unmasked)
    _write_pergraph(graphname, canonical)
    await _invalidate_manager_cache()
    return {
        "status": "success",
        "message": f"saved {len(canonical)} MCP server override(s) for {graphname}",
    }


@router.get(route_prefix + "/{graphname}/mcp_servers/resolved")
async def resolved_pergraph_mcp_servers(
    graphname: str,
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
):
    """Merged effective MCP-server list for the graph (global ∪ per-graph,
    with per-graph overrides applied, tombstones removed). Used by the UI
    to show what the agent will actually see.
    """
    _require_roles(creds, {"superuser"})
    specs = get_mcp_servers(graphname)
    data = [_redact_spec(s.model_dump()) for s in specs]
    return {"status": "success", "data": data}


@router.post(f"{route_prefix}/mcp_servers/test")
async def test_mcp_server(
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
    body: dict = Body(...),
):
    """Connect to a single MCP server (from the request body, NOT saved
    config) and return its tool list. Used by the UI "Test connection"
    button before save.

    Mask sentinels in ``env``/``headers`` are first resolved against the
    saved spec by the same ``name``, so the UI can test an edit without
    re-typing secrets.
    """
    _require_roles(creds, {"superuser"})

    name = body.get("name")
    stored = {s.get("name"): s for s in _read_global() if isinstance(s, dict)}
    unmasked = _unmask_spec(body, stored)
    try:
        spec = McpServerSpec(**unmasked)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid spec: {exc}")

    # Install the server's tarball (if any) before probing, so a just-uploaded
    # stdio server's console-script command exists when we launch it.
    ensure_libraries_installed([spec])

    from mcp_addons import McpClientManager, run_async

    mgr = McpClientManager([spec])

    async def _probe():
        try:
            tools = await mgr.list_tools(spec.name)
            return {
                "ok": True,
                "tools": [
                    {"name": t.name, "qualified_name": t.qualified_name,
                     "description": t.description}
                    for t in tools
                ],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                await mgr.shutdown()
            except Exception:
                pass

    result = await run_async(_probe())
    return {"status": "success", "data": result}


@router.post(f"{route_prefix}/mcp_servers/library")
async def upload_mcp_library(
    creds: Annotated[HTTPBasicCredentials, Depends(_ui_creds())],
    file: UploadFile = File(...),
):
    """Upload a source tarball (.tar.gz / .tgz) for an stdio MCP server into
    the fixed ``configs/mcp_servers/`` folder. Returns the stored filename to
    drop into the server's ``path`` field; GraphRAG pip-installs it on start.

    Superuser only — the tarball is executed inside the GraphRAG server.
    """
    _require_roles(creds, {"superuser"})
    filename = os.path.basename(file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="missing filename")
    if not (filename.endswith(".tar.gz") or filename.endswith(".tgz")):
        raise HTTPException(status_code=400, detail="only .tar.gz / .tgz tarballs are accepted")
    os.makedirs(MCP_LIB_DIR, exist_ok=True)
    dest = os.path.join(MCP_LIB_DIR, filename)
    try:
        data = await file.read()
        tmp = f"{dest}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}")
    logger.info(f"uploaded MCP server library: {dest}")
    return {"status": "success", "path": filename}
