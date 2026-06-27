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

"""External MCP-server config.

Typed schema and merge logic for ``mcp_servers``, the top-level config
section (sibling of ``graphrag_config``) that catalogs outside Model
Context Protocol servers the agentic engine may dispatch tools to.

Two scopes — global (``configs/server_config.json``) and per-graph
(``configs/graph_configs/<g>/server_config.json``). Per-graph entries
override global ones by ``name``; a per-graph entry with ``enabled=False``
acts as a tombstone that suppresses a same-named global entry.

The MCP client manager consumes ``resolve_mcp_servers(...)`` and wires
each enabled spec into the agentic tool registry.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import sys
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class McpServerSpec(BaseModel):
    """One external MCP server.

    Tool names this server exposes are surfaced to the planner under the
    ``"<name>.<tool>"`` namespace (e.g. ``"weather.get_forecast"``) so
    they never collide with the built-in GraphRAG tools.
    """

    name: str = Field(min_length=1, description="Unique within scope. Becomes the planner-visible tool prefix.")
    transport: Literal["stdio", "http"]
    enabled: bool = True
    description: str = ""
    # One-paragraph hint of what data lives here and when to use it.
    # Surfaced only when ``graphrag_config.tool_selection`` is set to
    # ``"purpose_filter"`` (deferred); ignored in the default ``"flat"``
    # mode.
    purpose: str = ""

    # stdio
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    # Optional path to a source tarball (e.g. "configs/mcp_servers/foo.tar.gz")
    # that GraphRAG pip-installs at startup so this server's ``command`` (the
    # console script the package ships) is available. Omit when ``command`` is
    # already on PATH (e.g. a bundled server).
    path: Optional[str] = None

    # http
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)

    # identity
    forward_user: bool = False
    user_header: str = "X-User"

    # security
    allowed_tools: List[str] = Field(default_factory=lambda: ["*"])

    @field_validator("name")
    @classmethod
    def _name_no_dot(cls, v: str) -> str:
        # "." is the registry namespace separator between server and tool
        # names; allowing it inside a server name would make dispatch
        # ambiguous.
        if "." in v:
            raise ValueError("name must not contain '.'")
        return v

    @model_validator(mode="after")
    def _transport_requirements(self) -> "McpServerSpec":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("http transport requires 'url'")
        return self


def resolve_mcp_servers(
    global_raw: Optional[List[dict]],
    graph_raw: Optional[List[dict]],
) -> List[McpServerSpec]:
    """Merge global and per-graph specs; return enabled set.

    - Order: global entries first (in their declared order), then per-graph
      entries that introduce new names.
    - Override: when both scopes declare the same ``name``, the per-graph
      entry replaces the global one in-place (its declared order slot).
    - Tombstone: ``enabled=False`` removes the entry from the returned
      list, whether the disable comes from global or per-graph.
    """
    by_name: Dict[str, McpServerSpec] = {}
    order: List[str] = []

    for raw in global_raw or []:
        spec = McpServerSpec(**raw)
        if spec.name not in by_name:
            order.append(spec.name)
        by_name[spec.name] = spec

    for raw in graph_raw or []:
        spec = McpServerSpec(**raw)
        if spec.name not in by_name:
            order.append(spec.name)
        by_name[spec.name] = spec  # per-graph wins

    return [by_name[n] for n in order if by_name[n].enabled]


# --- source-tarball install for stdio servers --------------------------------

# Tarball paths already pip-installed in this process, so repeated startup /
# agent-build calls don't reinstall. Cleared on restart — a fresh container
# reinstalls from the persisted tarballs, which is what makes them stick.
# The library folder is fixed and lives under the mounted ``configs/`` dir, so
# a spec's ``path`` is just the tarball filename (e.g. "my_server-1.0.tar.gz").
MCP_LIB_DIR = "configs/mcp_servers"

_installed_paths: set = set()


def _resolve_tarball_path(path: str) -> str:
    """Resolve a tarball ``path`` (a filename) under the fixed ``MCP_LIB_DIR``."""
    p = (path or "").strip()
    if os.path.isabs(p):
        return p
    p = p.lstrip("/")
    prefix = MCP_LIB_DIR + "/"
    if p.startswith(prefix):  # tolerate a pasted full path
        p = p[len(prefix):]
    return os.path.join(os.getcwd(), MCP_LIB_DIR, p)


def ensure_libraries_installed(specs) -> None:
    """pip-install the source tarballs referenced by stdio MCP specs.

    Each spec's optional ``path`` points at a ``.tar.gz`` that, once installed,
    provides the server's ``command`` (console script) plus its dependencies.
    Idempotent within a process and best-effort — a failed install is logged,
    not raised, so one bad addon can't block startup or chat.
    """
    for spec in specs or []:
        transport = getattr(spec, "transport", None)
        path = getattr(spec, "path", None)
        enabled = getattr(spec, "enabled", True)
        if transport != "stdio" or not path or not enabled:
            continue
        resolved = _resolve_tarball_path(path)
        if resolved in _installed_paths:
            continue
        if not os.path.isfile(resolved):
            logger.warning(f"MCP library tarball not found, skipping: {resolved}")
            continue
        try:
            logger.info(f"Installing MCP server library: {resolved}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-input", resolved],
                check=True, capture_output=True, text=True,
            )
            _installed_paths.add(resolved)
            logger.info(f"Installed MCP server library: {resolved}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to install MCP library {resolved}: {e.stderr or e}")
        except Exception as e:
            logger.error(f"Failed to install MCP library {resolved}: {e}")


def _collect_all_specs() -> List[McpServerSpec]:
    """Every configured stdio spec across all scopes (global + per-graph),
    used at startup to decide which tarballs to install."""
    from common.config import server_config, SERVER_CONFIG

    specs: List[McpServerSpec] = []

    def _parse(raw_list):
        for raw in raw_list or []:
            try:
                specs.append(McpServerSpec(**raw))
            except Exception as e:
                logger.warning(f"Skipping invalid mcp_servers entry: {e}")

    _parse(server_config.get("mcp_servers"))

    cfg_dir = (
        os.path.dirname(os.path.abspath(SERVER_CONFIG))
        if isinstance(SERVER_CONFIG, str) and SERVER_CONFIG.endswith(".json")
        else os.path.join(os.getcwd(), "configs")
    )
    for gc in glob.glob(os.path.join(cfg_dir, "graph_configs", "*", "server_config.json")):
        try:
            with open(gc) as f:
                _parse(json.load(f).get("mcp_servers"))
        except Exception as e:
            logger.warning(f"Could not read {gc}: {e}")

    return specs


def install_configured_libraries() -> None:
    """Startup hook: install the tarballs referenced by the MCP config at all
    levels (global + per-graph)."""
    try:
        ensure_libraries_installed(_collect_all_specs())
    except Exception as e:
        logger.error(f"MCP library startup install failed: {e}")
