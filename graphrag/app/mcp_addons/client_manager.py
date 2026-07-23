# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""External MCP-server client manager.

Owns long-lived sessions to the outside Model Context Protocol servers
configured under ``mcp_servers`` (see ``common.mcp_config``). One manager
per graph; connections are lazily opened on first ``list_tools`` /
``call_tool`` and reused across the agentic engine's requests.

Identity forwarding rides MCP's per-call ``_meta`` field, which the SDK
exposes via ``ClientSession.call_tool(..., meta=...)``. That keeps a
single shared session per server safe to use across concurrent users —
the user identity travels with each request, not the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, Tool

from common.mcp_config import McpServerSpec

logger = logging.getLogger(__name__)


@dataclass
class McpToolInfo:
    """Planner-facing view of one tool exposed by an external MCP server."""
    server: str
    name: str                       # raw tool name on the server
    qualified_name: str             # "<server>.<name>" — registry key
    description: str
    input_schema: Dict[str, Any]    # JSON Schema for arguments


@dataclass
class _Conn:
    spec: McpServerSpec
    session: ClientSession
    stack: contextlib.AsyncExitStack
    tools_cache: Optional[List[Tool]] = None
    cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpClientManager:
    """Manages connections to a graph's configured external MCP servers.

    Lifecycle: construct with the resolved spec list, then call
    ``list_tools()`` / ``call_tool()`` lazily. Call ``shutdown()`` at app
    shutdown to close stdio subprocesses and HTTP sessions cleanly.
    """

    def __init__(self, specs: List[McpServerSpec]):
        self._specs: Dict[str, McpServerSpec] = {s.name: s for s in specs}
        self._conns: Dict[str, _Conn] = {}
        self._connect_lock = asyncio.Lock()

    @property
    def server_names(self) -> List[str]:
        return list(self._specs.keys())

    def get_spec(self, server: str) -> McpServerSpec:
        if server not in self._specs:
            raise KeyError(f"unknown MCP server: {server!r}")
        return self._specs[server]

    async def _open(self, spec: McpServerSpec) -> _Conn:
        stack = contextlib.AsyncExitStack()
        try:
            if spec.transport == "stdio":
                params = StdioServerParameters(
                    command=spec.command,
                    args=list(spec.args),
                    env=dict(spec.env) if spec.env else None,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:  # http
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(
                        url=spec.url,
                        headers=dict(spec.headers) if spec.headers else None,
                    )
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return _Conn(spec=spec, session=session, stack=stack)
        except BaseException:
            await stack.aclose()
            raise

    async def _conn(self, server: str) -> _Conn:
        if server in self._conns:
            return self._conns[server]
        async with self._connect_lock:
            if server in self._conns:
                return self._conns[server]
            spec = self.get_spec(server)
            conn = await self._open(spec)
            self._conns[server] = conn
            logger.info(
                f"mcp_addons: connected server={spec.name} transport={spec.transport}"
            )
            return conn

    async def list_tools(self, server: str) -> List[McpToolInfo]:
        conn = await self._conn(server)
        async with conn.cache_lock:
            if conn.tools_cache is None:
                resp = await conn.session.list_tools()
                conn.tools_cache = list(resp.tools)
            tools = list(conn.tools_cache)
        return [
            McpToolInfo(
                server=server,
                name=t.name,
                qualified_name=f"{server}.{t.name}",
                description=t.description or "",
                input_schema=dict(t.inputSchema or {}),
            )
            for t in tools
        ]

    async def list_all_tools(self) -> List[McpToolInfo]:
        out: List[McpToolInfo] = []
        for name in self._specs:
            try:
                out.extend(await self.list_tools(name))
            except Exception as e:
                # One bad server shouldn't blank the catalog
                logger.warning(f"mcp_addons: list_tools failed server={name}: {e}")
        return out

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        user: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CallToolResult:
        conn = await self._conn(server)
        meta: Optional[Dict[str, Any]] = None
        if conn.spec.forward_user and user:
            # MCP-native per-call user injection: rides the JSON-RPC
            # request's ``_meta`` field. Servers that authenticate
            # per-call user read it from there; servers that don't
            # ignore it. Same wire for stdio and http.
            meta = {"user": user}
        kwargs: Dict[str, Any] = {"arguments": arguments or {}}
        if meta is not None:
            kwargs["meta"] = meta
        if timeout is not None:
            kwargs["read_timeout_seconds"] = timedelta(seconds=timeout)
        return await conn.session.call_tool(tool, **kwargs)

    async def shutdown(self) -> None:
        for name, conn in list(self._conns.items()):
            try:
                await conn.stack.aclose()
            except Exception as e:
                logger.warning(f"mcp_addons: shutdown error server={name}: {e}")
        self._conns.clear()


# --- Per-graph singleton registry ------------------------------------------

_managers: Dict[str, McpClientManager] = {}
_managers_lock = asyncio.Lock()


async def get_manager(graphname: Optional[str]) -> McpClientManager:
    """Return (and lazily create) the manager for a graph.

    The spec list is resolved at construction time; admins who change
    ``mcp_servers`` config need to call ``shutdown_all()`` (or the
    matching REST endpoint, once Phase 4 lands) to force a rebuild.
    """
    key = graphname or ""
    if key in _managers:
        return _managers[key]
    async with _managers_lock:
        if key in _managers:
            return _managers[key]
        from common.config import get_mcp_servers
        specs = get_mcp_servers(graphname)
        mgr = McpClientManager(specs)
        _managers[key] = mgr
        return mgr


async def shutdown_all() -> None:
    """Close every cached manager. Call on app shutdown."""
    async with _managers_lock:
        items = list(_managers.items())
        _managers.clear()
    for key, mgr in items:
        try:
            await mgr.shutdown()
        except Exception as e:
            logger.warning(f"mcp_addons: shutdown_all error graph={key!r}: {e}")
