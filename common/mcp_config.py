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

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
