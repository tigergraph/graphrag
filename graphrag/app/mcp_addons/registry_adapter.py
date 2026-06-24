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

"""Adapt external MCP tools into the agentic tool registry.

Discovers every enabled tool from an ``McpClientManager``, applies each
server's allowlist (``McpServerSpec.allowed_tools``), and wraps the
result as a ``ToolSpec`` the registry's ``catalog`` / ``run`` /
``lc_tools_spec`` can serve. The sync agent executor calls each
dispatcher; the dispatcher schedules onto the dedicated MCP event loop
so the manager's async state stays consistent.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from tools.tool_guards import is_tool_allowed
from tools.tool_registry import ToolSpec

from mcp_addons.client_manager import McpClientManager, McpToolInfo
from mcp_addons.result_normalize import normalize_call_tool_result
from mcp_addons.runtime import run_sync

logger = logging.getLogger(__name__)

# Per-tool wall-clock limit; widened later if a server consistently runs
# longer (or made configurable per-spec). External tools doing remote
# I/O are typically a few seconds; 30s leaves headroom for cold-start
# stdio subprocess launches.
_DEFAULT_TOOL_TIMEOUT_S = 30.0


def _make_dispatcher(info: McpToolInfo, manager: McpClientManager):
    """Build the sync ``fn(ctx, **kwargs)`` the registry will invoke."""
    qualified = info.qualified_name
    server = info.server
    tool = info.name

    def _fn(ctx, **kwargs) -> dict:
        user = getattr(ctx, "user", None) or getattr(getattr(ctx, "conn", None), "username", None)
        try:
            result = run_sync(
                manager.call_tool(server, tool, kwargs, user=user, timeout=_DEFAULT_TOOL_TIMEOUT_S),
                timeout=_DEFAULT_TOOL_TIMEOUT_S + 5.0,
            )
        except Exception as exc:
            logger.warning(f"mcp_addons: {qualified} call failed: {exc}", exc_info=True)
            return {
                "ok": False,
                "summary": f"{qualified} failed: {exc}",
                "context": None,
                "citations": [],
            }
        return normalize_call_tool_result(result, qualified)

    return _fn


def _spec_for(info: McpToolInfo, manager: McpClientManager) -> ToolSpec:
    return ToolSpec(
        name=info.qualified_name,
        description=info.description or f"External MCP tool {info.qualified_name}",
        args_schema_json=dict(info.input_schema or {}),
        fn=_make_dispatcher(info, manager),
    )


def discover_tools(manager: McpClientManager) -> Dict[str, ToolSpec]:
    """Enumerate every enabled MCP tool the manager exposes, apply each
    server's allowlist, and return the registry-ready mapping.

    One bad server doesn't blank the catalog — ``manager.list_all_tools``
    already isolates per-server failures. A tool the allowlist rejects
    is silently dropped (no entry, no error) so the planner never sees it.
    """
    tools: List[McpToolInfo] = run_sync(manager.list_all_tools(), timeout=20.0)
    out: Dict[str, ToolSpec] = {}
    for info in tools:
        try:
            allowed = manager.get_spec(info.server).allowed_tools
        except KeyError:
            continue
        if not is_tool_allowed(allowed, info.name):
            logger.info(
                f"mcp_addons: allowlist denied server={info.server} tool={info.name}"
            )
            continue
        out[info.qualified_name] = _spec_for(info, manager)
    return out
