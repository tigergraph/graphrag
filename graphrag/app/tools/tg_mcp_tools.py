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

"""tigergraph-mcp adapter — in-process, per-user.

Wraps a read-only subset of ``tigergraph-mcp`` tools so the agentic engine
can leverage them for raw TigerGraph operations (interpreted-query
execution, installed-query execution, neighbor expansion). Each call runs
as the **logged-in user**: a per-request ``connection_config`` is held in a
ContextVar and injected into ``tigergraph_mcp``'s ``get_connection`` via a
process-wide patch that reads that (per-request) var — so concurrent users
never share a connection.

Import-guarded: if ``tigergraph-mcp`` isn't installed, ``AVAILABLE`` is
False and the registry simply skips these tools (the engine falls back to
the GraphRAG-native tools).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Per-request TigerGraph credentials for the tg-mcp tools.
_tg_conn_cfg: contextvars.ContextVar = contextvars.ContextVar(
    "tg_mcp_conn_cfg", default=None
)

try:
    from tigergraph_mcp import connection_manager as _cm
    from tigergraph_mcp.tools import query_tools as _qt
    from tigergraph_mcp.tools import schema_tools as _st

    _orig_get_connection = _cm.get_connection

    def _patched_get_connection(profile=None, graph_name=None, connection_config=None):
        # Inject the current request's per-user creds when the caller (the
        # stock tool) didn't pass an explicit connection_config.
        cfg = connection_config or _tg_conn_cfg.get()
        return _orig_get_connection(
            profile=profile, graph_name=graph_name, connection_config=cfg
        )

    # Patch the name bound inside each tool module we use.
    _qt.get_connection = _patched_get_connection
    _st.get_connection = _patched_get_connection
    AVAILABLE = True
except Exception as exc:  # pragma: no cover - import guard
    logger.info(f"tigergraph-mcp not available, tg-mcp tools disabled: {exc}")
    AVAILABLE = False


def set_user_connection_config(cfg: Optional[dict]) -> None:
    """Set the per-request connection_config the tg-mcp tools run under."""
    _tg_conn_cfg.set(cfg)


def conn_config_from_conn(conn, graphname: str) -> dict:
    """Build a tigergraph-mcp connection_config from a pyTigerGraph conn."""
    return {
        "host": getattr(conn, "host", None) or getattr(conn, "gsUrl", ""),
        "graphname": graphname or getattr(conn, "graphname", ""),
        "username": getattr(conn, "username", "") or "",
        "password": getattr(conn, "password", "") or "",
        "apiToken": getattr(conn, "apiToken", "") or "",
        "restppPort": getattr(conn, "restppPort", "9000"),
        "gsPort": getattr(conn, "gsPort", "14240"),
    }


def _run(coro):
    """Run an async tg-mcp tool from the sync executor (fresh event loop in
    the current worker thread; the ContextVar value is copied into it).
    """
    return asyncio.run(coro)


def _normalize(res, label: str) -> dict:
    """Turn a tg-mcp ``List[TextContent]`` into our tool result dict."""
    item = res[0] if isinstance(res, list) and res else res
    text = getattr(item, "text", None) or (item if isinstance(item, str) else str(item))
    ok = True
    parsed = None
    try:
        body = text.strip()
        if body.startswith("```"):
            body = body.split("```", 2)[1]
            if body.startswith("json"):
                body = body[4:]
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("success") is False:
            ok = False
    except Exception:
        parsed = text
    return {
        "ok": ok,
        "summary": f"{label}: {'ok' if ok else 'failed'}",
        "context": {"function_call": label, "result": parsed},
        "citations": [],
    }


# --- read-only tool wrappers (ctx is the GraphRAGToolContext) --------------

def _ensure(ctx):
    """Bind the per-request creds before each call (idempotent)."""
    cfg = getattr(ctx, "tg_connection_config", None)
    if cfg is None:
        cfg = conn_config_from_conn(ctx.conn, getattr(ctx.conn, "graphname", ""))
    set_user_connection_config(cfg)


def tg_run_query(ctx, query_text: str) -> dict:
    """Run an interpreted (dynamic) GSQL query as the logged-in user."""
    if not AVAILABLE:
        return {"ok": False, "summary": "tigergraph-mcp unavailable", "context": None, "citations": []}
    ctx.emit("Running a graph query (tigergraph-mcp)")
    _ensure(ctx)
    g = getattr(ctx.conn, "graphname", "")
    return _normalize(_run(_qt.run_query(query_text=query_text, graph_name=g)), "tg_run_query")


def tg_run_installed_query(ctx, query_name: str, params: Optional[dict] = None) -> dict:
    """Run a pre-installed query by name as the logged-in user."""
    if not AVAILABLE:
        return {"ok": False, "summary": "tigergraph-mcp unavailable", "context": None, "citations": []}
    ctx.emit(f"Running installed query {query_name} (tigergraph-mcp)")
    _ensure(ctx)
    g = getattr(ctx.conn, "graphname", "")
    return _normalize(
        _run(_qt.run_installed_query(query_name=query_name, params=params or {}, graph_name=g)),
        "tg_run_installed_query",
    )


def tg_get_neighbors(ctx, vertex_type: str, vertex_id: str,
                     edge_type: Optional[str] = None,
                     target_vertex_type: Optional[str] = None,
                     limit: Optional[int] = None) -> dict:
    """Expand neighbors of a vertex as the logged-in user (no GSQL needed)."""
    if not AVAILABLE:
        return {"ok": False, "summary": "tigergraph-mcp unavailable", "context": None, "citations": []}
    ctx.emit("Expanding neighbors (tigergraph-mcp)")
    _ensure(ctx)
    g = getattr(ctx.conn, "graphname", "")
    return _normalize(
        _run(_qt.get_neighbors(
            vertex_type=vertex_type, vertex_id=vertex_id, edge_type=edge_type,
            target_vertex_type=target_vertex_type, limit=limit, graph_name=g)),
        "tg_get_neighbors",
    )
