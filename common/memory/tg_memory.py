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

"""
TigerGraph chat memory (same graph as GraphRAG).

Vertex types in GraphStudio: **conversation**, **message**, **summary** (see Memory_Schema.gsql).

Epoch fields use UTC epoch SECONDS as UINT (SupportAI-style).

message.system_content holds the model/RAG reply; map to LLM role \"assistant\" when building prompts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pyTigerGraph import TigerGraphConnection

from common.config import get_graphrag_config, graphrag_config

logger = logging.getLogger(__name__)

# `ls` output line for this vertex type (TigerGraph lists lowercase as defined)
_SCHEMA_MARKER = "- VERTEX message"
_QUERY_NAME = "get_last_n_memory_exchanges"
_QUERY_LIST_CONVOS = "list_conversations_for_user"
_QUERY_LIST_MSGS = "list_messages_for_conversation"
_QUERY_GET_SUMMARY = "get_conversation_summary"
_QUERY_UPDATE_SUMMARY = "update_conversation_summary"
_QUERY_LIST_MSGS_MEMORY = "list_messages_for_memory"
_QUERY_GET_LATEST_SUMMARY = "get_latest_summary"
_QUERY_DELETE_SUMMARIES = "delete_conversation_summaries"

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_STARTUP_MAX_WAIT_S = 300
_STARTUP_RETRY_INTERVAL_S = 5
_UUID_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _gsql_ls_contains_vertex_type(ls_output: str, vertex_type: str) -> bool:
    """True if ``gsql 'USE GRAPH g; ls'`` output lists the given vertex type."""
    return bool(
        re.search(
            rf"-\s*VERTEX\s+{re.escape(vertex_type)}\b",
            ls_output or "",
            flags=re.IGNORECASE,
        )
    )


def _gsql_find_query_ls_line(ls_output: str, query_name: str) -> str | None:
    """Return the ``ls`` line for a query by name (e.g. ``- foo(...) (installed v2)``)."""
    for line in (ls_output or "").splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        rest = s[2:].strip()
        head = rest.split("(", 1)[0].strip() if "(" in rest else (rest.split()[0] if rest else "")
        if head == query_name:
            return line
    return None


def _gsql_ls_query_installation_state(ls_output: str, query_name: str) -> str:
    """
    Classify query catalog state from ``gsql ls`` (see GSQL ref: ``(draft)``, ``(installed v2)``, …).

    Returns one of: ``missing``, ``installed``, ``needs_reinstall``, ``pending_install``,
    ``legacy_no_status`` (assume already OK on older servers that omit status).
    """
    line = _gsql_find_query_ls_line(ls_output, query_name)
    if line is None:
        return "missing"
    m = re.search(r"\(([^)]+)\)\s*$", line.strip())
    if not m:
        return "missing"
    last = m.group(1).strip().lower()
    if last.startswith("installed"):
        return "installed"
    if "pending" in last and "install" in last.replace(" ", ""):
        return "pending_install"
    if last in ("draft", "deprecated", "disabled") or "failed" in last or "compilation" in last:
        return "needs_reinstall"
    # Trailing ``(...)`` is the signature, not status — older / minimal ls output
    if re.match(
        r"^(string|int|uint|float|double|bool|datetime|vertex|edge|set|list|bag)\b",
        last,
    ):
        return "legacy_no_status"
    if "," in last or " " in last:
        return "legacy_no_status"
    return "needs_reinstall"


def _gsql_drop_query_best_effort(conn: TigerGraphConnection, graphname: str, query_name: str) -> None:
    try:
        conn.gsql(f"USE GRAPH {graphname}\nDROP QUERY {query_name}\n")
    except Exception:
        logger.debug(
            "[TG_MEMORY] DROP QUERY %s graph=%s (ignored if absent)",
            query_name,
            graphname,
            exc_info=True,
        )


def _install_named_query_from_file(
    conn: TigerGraphConnection,
    graphname: str,
    query_name: str,
    relative_path: tuple[str, ...],
    out: list[str],
) -> str:
    """
    CREATE + INSTALL one query from ``common/gsql/...``; refreshes ``ls`` and returns it.
    Caller must have USE GRAPH context via conn.gsql prefixes.
    """
    qpath = _gsql_path(*relative_path)
    with open(qpath, "r", encoding="utf-8") as f:
        q_body = f.read()
    q_res = conn.gsql(f"USE GRAPH {graphname}\nBEGIN\n{q_body}\nEND\n")
    out.append(f"memory query create {query_name}: {q_res}")
    inst = conn.gsql(f"USE GRAPH {graphname}\nINSTALL QUERY {query_name}\n")
    out.append(f"memory query install {query_name}: {inst}")
    return conn.gsql(f"USE GRAPH {graphname}\n ls")


def app_conversation_id_from_vertex_pk(vertex_pk: str) -> str:
    """
    Reverse ``_conversation_vertex_primary_id``: TG stores hyphenless UUID32 as PK;
    the UI/API use standard UUID strings.
    """
    pk = (vertex_pk or "").strip()
    if len(pk) == 32 and _UUID_HEX32.match(pk):
        return f"{pk[:8]}-{pk[8:12]}-{pk[12:16]}-{pk[16:20]}-{pk[20:]}"
    return pk


def _epoch_u_to_iso(epoch_u: Any) -> str:
    try:
        ts = int(epoch_u)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _conversation_vertex_primary_id(app_conversation_id: str) -> str:
    """
    Maps application conversation_id (often a UUID with hyphens) to a valid
    TigerGraph PRIMARY_ID for the **conversation** vertex type.

    TigerGraph rejects some primary keys for this type (e.g. strings containing
    '-'), which leaves **message** vertices without a linked conversation and
    breaks Explore Graph. **message** rows still store the original
    ``conversation_id`` attribute for GSQL lookup.
    """
    cid = (app_conversation_id or "").strip()
    if not cid:
        return "_empty_conversation"
    no_hyphen = cid.replace("-", "")
    if len(no_hyphen) == 32 and _UUID_HEX32.match(no_hyphen):
        return no_hyphen.lower()
    safe = re.sub(r"[^A-Za-z0-9_]", "_", cid).strip("_")
    return (safe or "conversation")[:250]


def _gsql_path(*parts: str) -> str:
    return os.path.join(_REPO_ROOT, "common", "gsql", *parts)


def _extract_summary_schema_job(schema_text: str) -> str:
    """Return the ``add_graphrag_chat_memory_summary`` job DDL block, or empty string."""
    key = "CREATE SCHEMA_CHANGE JOB add_graphrag_chat_memory_summary"
    if key not in schema_text:
        return ""
    i = schema_text.index(key)
    depth = 0
    started = False
    for j in range(i + len(key), len(schema_text)):
        ch = schema_text[j]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return schema_text[i : j + 1]
    return ""


def tg_memory_enabled(graphname: str | None = None) -> bool:
    cfg = get_graphrag_config(graphname) if graphname else graphrag_config
    return bool(cfg.get("tg_memory_enabled", False))


def install_memory_schema_for_all_graphs_at_startup() -> None:
    """
    Wait until TigerGraph accepts ``listGraphs``, then run ``init_memory_schema`` on
    each graph using ``db_config`` credentials.

    Skipped when ``graphrag_config["tg_memory_schema_on_startup"]`` is false.
    Intended for Docker / process startup so Explore Graph shows **conversation** and
    **message** without calling initialize_graph first.
    """
    from common.config import db_config

    if db_config.get("username") is None or db_config.get("password") is None:
        logger.warning(
            "[TG_MEMORY] db_config username/password missing; skipping startup memory schema."
        )
        return

    if not graphrag_config.get("tg_memory_schema_on_startup", True):
        logger.info("[TG_MEMORY] tg_memory_schema_on_startup is false; skipping startup schema install.")
        return

    elapsed = 0
    conn: TigerGraphConnection | None = None
    graphs: list[str] = []

    while elapsed < _STARTUP_MAX_WAIT_S:
        try:
            conn = TigerGraphConnection(
                host=db_config["hostname"],
                graphname="",
                username=db_config["username"],
                password=db_config["password"],
                restppPort=db_config.get("restppPort", "9000"),
                gsPort=db_config.get("gsPort", "14240"),
            )
            if db_config.get("getToken"):
                token = conn.getToken()[0]
                conn = TigerGraphConnection(
                    host=db_config["hostname"],
                    graphname="",
                    username=db_config["username"],
                    password=db_config["password"],
                    apiToken=token,
                    restppPort=db_config.get("restppPort", "9000"),
                    gsPort=db_config.get("gsPort", "14240"),
                )
            conn.customizeHeader(
                timeout=int(db_config.get("default_timeout", 300)) * 1000,
                responseSize=5000000,
            )
            graph_list = conn.listGraphs()
            graphs = [g["graphName"] for g in graph_list if "graphName" in g]
            break
        except Exception as e:
            logger.warning(
                "[TG_MEMORY] TigerGraph not ready (%s); retrying in %ss (%ss/%ss)",
                e,
                _STARTUP_RETRY_INTERVAL_S,
                elapsed,
                _STARTUP_MAX_WAIT_S,
            )
            time.sleep(_STARTUP_RETRY_INTERVAL_S)
            elapsed += _STARTUP_RETRY_INTERVAL_S

    if conn is None:
        logger.error(
            "[TG_MEMORY] No connection after %ss; memory schema not installed at startup.",
            _STARTUP_MAX_WAIT_S,
        )
        return

    _apply_long_gsql_timeout(conn)

    if not graphs:
        logger.info("[TG_MEMORY] No graphs found yet; memory schema install skipped (empty cluster).")
        return

    for graphname in graphs:
        try:
            init_memory_schema(conn, graphname)
        except Exception:
            logger.warning(
                "[TG_MEMORY] init_memory_schema failed for graph=%s",
                graphname,
                exc_info=True,
            )


def _apply_long_gsql_timeout(conn: Any) -> None:
    """INSTALL QUERY / schema jobs can exceed default HTTP read timeouts."""
    try:
        from common.config import db_config

        base_ms = int(db_config.get("default_timeout", 300)) * 1000
        to_ms = max(base_ms, 600_000)
        conn.customizeHeader(timeout=to_ms, responseSize=10_000_000)
    except Exception:
        pass


def init_memory_schema(conn: TigerGraphConnection, graphname: str) -> str:
    """
    Idempotent: add conversation + message + has_message; add summary + edges when missing;
    install read/update queries.

    Called on every graph init (initialize_graph) so types appear in GraphStudio
    Explore → Pick vertices, even before any chat is stored. Writes still require
    tg_memory_enabled=true.
    """
    _apply_long_gsql_timeout(conn)
    try:
        current = conn.gsql(f"USE GRAPH {graphname}\n ls")
    except Exception:
        logger.warning("[TG_MEMORY] gsql ls failed graph=%s", graphname, exc_info=True)
        return f"init_memory_schema: USE GRAPH {graphname}; ls failed (see logs)"
    out: list[str] = []

    if _SCHEMA_MARKER in current:
        out.append("memory schema: already present")
    else:
        path = _gsql_path("memory", "Memory_Schema.gsql")
        try:
            with open(path, "r", encoding="utf-8") as f:
                schema = f.read()
            res = conn.gsql(
                f"USE GRAPH {graphname}\n{schema}\nRUN SCHEMA_CHANGE JOB add_graphrag_chat_memory"
            )
            out.append(f"memory schema job: {res}")
            current = conn.gsql(f"USE GRAPH {graphname}\n ls")
        except Exception:
            logger.warning(
                "[TG_MEMORY] base memory schema job failed graph=%s",
                graphname,
                exc_info=True,
            )
            out.append("memory schema job: FAILED (see logs)")
            try:
                current = conn.gsql(f"USE GRAPH {graphname}\n ls")
            except Exception:
                pass

    _nq_state = _gsql_ls_query_installation_state(current, _QUERY_NAME)
    if _nq_state in ("installed", "legacy_no_status"):
        out.append("memory query: already present")
    elif _nq_state == "pending_install":
        out.append("memory query: pending install; skipping until catalog settles")
    else:
        if _nq_state == "needs_reinstall":
            _gsql_drop_query_best_effort(conn, graphname, _QUERY_NAME)
            out.append("memory query: dropped draft/broken get_last_n_memory_exchanges for reinstall")
        try:
            current = _install_named_query_from_file(
                conn, graphname, _QUERY_NAME, ("memory", "GetLastNMemoryExchanges.gsql"), out
            )
        except Exception:
            logger.warning(
                "[TG_MEMORY] install get_last_n_memory_exchanges failed graph=%s",
                graphname,
                exc_info=True,
            )
            out.append("memory query get_last_n_memory_exchanges: FAILED (see logs)")
            try:
                current = conn.gsql(f"USE GRAPH {graphname}\n ls")
            except Exception:
                pass

    if not _gsql_ls_contains_vertex_type(current, "summary"):
        path = _gsql_path("memory", "Memory_Schema.gsql")
        with open(path, "r", encoding="utf-8") as f:
            schema_full = f.read()
        job_sql = _extract_summary_schema_job(schema_full)
        if not job_sql:
            out.append("memory summary schema job: not found in Memory_Schema.gsql")
        else:
            try:
                res = conn.gsql(
                    f"USE GRAPH {graphname}\n{job_sql}\n"
                    f"RUN SCHEMA_CHANGE JOB add_graphrag_chat_memory_summary"
                )
                out.append(f"memory summary schema job: {res}")
            except Exception:
                logger.warning(
                    "[TG_MEMORY] add_graphrag_chat_memory_summary failed for graph=%s",
                    graphname,
                    exc_info=True,
                )
                out.append("memory summary schema job: failed (see logs)")
        current = conn.gsql(f"USE GRAPH {graphname}\n ls")
        if not _gsql_ls_contains_vertex_type(current, "summary"):
            logger.warning(
                "[TG_MEMORY] graph=%s: summary vertex type still missing after schema job. "
                "ls output (trunc): %s",
                graphname,
                (current or "")[:2000],
            )

    _EXTRA_QUERIES: tuple[tuple[str, str], ...] = (
        (_QUERY_LIST_CONVOS, "ListConversationsForUser.gsql"),
        (_QUERY_LIST_MSGS, "ListMessagesForConversation.gsql"),
        (_QUERY_GET_SUMMARY, "GetConversationSummary.gsql"),
        (_QUERY_UPDATE_SUMMARY, "UpdateConversationSummary.gsql"),
        (_QUERY_LIST_MSGS_MEMORY, "ListMessagesForMemory.gsql"),
        (_QUERY_GET_LATEST_SUMMARY, "GetLatestSummary.gsql"),
        (_QUERY_DELETE_SUMMARIES, "DeleteConversationSummaries.gsql"),
    )
    for qinst, qfile in _EXTRA_QUERIES:
        _ex_state = _gsql_ls_query_installation_state(current, qinst)
        if _ex_state in ("installed", "legacy_no_status"):
            out.append(f"memory query {qinst}: already present")
            continue
        if _ex_state == "pending_install":
            out.append(f"memory query {qinst}: pending install; skipping until catalog settles")
            continue
        if _ex_state == "needs_reinstall":
            _gsql_drop_query_best_effort(conn, graphname, qinst)
            out.append(f"memory query {qinst}: dropped draft/broken for reinstall")
        try:
            current = _install_named_query_from_file(
                conn, graphname, qinst, ("memory", qfile), out
            )
        except Exception:
            logger.warning(
                "[TG_MEMORY] install query %s (%s) failed for graph=%s",
                qinst,
                qfile,
                graphname,
                exc_info=True,
            )
            out.append(f"memory query {qinst}: FAILED (see logs)")
            try:
                current = conn.gsql(f"USE GRAPH {graphname}\n ls")
            except Exception:
                pass

    current = conn.gsql(f"USE GRAPH {graphname}\n ls")
    if _gsql_ls_contains_vertex_type(current, "message") and not _gsql_ls_contains_vertex_type(
        current, "summary"
    ):
        logger.warning(
            "[TG_MEMORY] graph=%s: chat memory is present but `summary` vertex type is missing "
            "after init_memory_schema. Rebuild graphrag with latest common/ or check GSQL errors above.",
            graphname,
        )

    summary = "\n".join(out)
    logger.info("init_memory_schema for %s: %s", graphname, summary)
    return summary


def write_exchange_to_tg_memory(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    user_id: str,
    user_content: str,
    system_content: str,
    *,
    tracelog: str = "",
    exchange_message_id: str | None = None,
    is_new_conversation: bool = False,
) -> None:
    """
    Persist one Q&A as a single **message** vertex linked to **conversation**.
    """
    if not tg_memory_enabled(graphname):
        return

    try:
        init_memory_schema(conn, graphname)

        import uuid

        mid = exchange_message_id or str(uuid.uuid4())
        ts = int(time.time())
        conv_vertex_id = _conversation_vertex_primary_id(conversation_id)

        try:
            df = conn.getVertexDataFrameById("conversation", conv_vertex_id)
            exists = df is not None and len(df) > 0
        except Exception:
            exists = False

        if exists and not is_new_conversation:
            conn.upsertVertex(
                "conversation",
                conv_vertex_id,
                attributes={
                    "user_id": user_id,
                    "epoch_processed": ts,
                },
            )
        else:
            conn.upsertVertex(
                "conversation",
                conv_vertex_id,
                attributes={
                    "user_id": user_id,
                    "epoch_added": ts,
                    "epoch_processed": ts,
                },
            )

        conn.upsertVertex(
            "message",
            mid,
            attributes={
                "conversation_id": conversation_id,
                "user_content": user_content or "",
                "system_content": system_content or "",
                "epoch_added": ts,
                "tracelog": tracelog or "",
            },
        )
        conn.upsertEdge(
            "conversation",
            conv_vertex_id,
            "has_message",
            "message",
            mid,
        )
        logger.debug(
            "tg_memory: wrote exchange message_id=%s conversation_id=%s conv_vertex_id=%s graph=%s",
            mid,
            conversation_id,
            conv_vertex_id,
            graphname,
        )
    except Exception:
        logger.warning(
            "tg_memory: failed to write exchange conv=%s graph=%s",
            conversation_id,
            graphname,
            exc_info=True,
        )


def ensure_conversation_shell_for_ui(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    user_id: str,
) -> None:
    """
    Upsert an empty **conversation** vertex as soon as the UI thread id exists.

    Lets ``GET /ui/user`` and ``GET /ui/conversation/{id}`` work **before** the first
    assistant reply (which would otherwise be the first TG write).
    """
    if not tg_memory_enabled(graphname):
        return
    try:
        init_memory_schema(conn, graphname)
        ts = int(time.time())
        conv_vertex_id = _conversation_vertex_primary_id(conversation_id)
        conn.upsertVertex(
            "conversation",
            conv_vertex_id,
            attributes={
                "user_id": user_id,
                "epoch_added": ts,
                "epoch_processed": ts,
            },
        )
        logger.debug(
            "tg_memory: ensured conversation shell conv_vertex_id=%s graph=%s",
            conv_vertex_id,
            graphname,
        )
    except Exception:
        logger.warning(
            "tg_memory: ensure_conversation_shell_for_ui failed conv=%s graph=%s",
            conversation_id,
            graphname,
            exc_info=True,
        )


def get_last_n_memory_exchanges(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    n: int = 4,
) -> list[dict[str, Any]]:
    """
    Return up to n **message** vertices, newest first by epoch_added.
    """
    if not tg_memory_enabled(graphname):
        return []
    init_memory_schema(conn, graphname)
    try:
        raw = conn.runInstalledQuery(
            _QUERY_NAME,
            params={"conv_id": conversation_id, "n": int(n)},
        )
    except Exception:
        logger.warning("get_last_n_memory_exchanges failed", exc_info=True)
        return []

    rows: list[dict[str, Any]] = []
    for block in raw or []:
        if "rows" not in block:
            continue
        for item in block["rows"]:
            vrow = _vertex_row_from_print_item(item) if isinstance(item, dict) else None
            if not vrow:
                rows.append({"raw": item})
                continue
            attrs = _attrs_from_vertex_row(vrow)
            rows.append(
                {
                    "message_id": _primary_id_from_vertex_row(vrow),
                    "user_content": attrs.get("user_content"),
                    "system_content": attrs.get("system_content"),
                    "epoch_added": attrs.get("epoch_added"),
                }
            )

    if rows and all("message_id" in r for r in rows):
        return rows

    if raw:
        logger.debug(
            "get_last_n_memory_exchanges parse fallback raw=%s",
            json.dumps(raw, default=str)[:2000],
        )
    return rows


def _vertex_row_from_print_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    GSQL ``PRINT seedSet AS rows`` yields rows like
    ``{"v_id": ..., "v_type": ..., "attributes": {...}}`` directly. Older
    SELECT-into-tuple patterns wrap them as ``{"c": {...}}`` / ``{"v": {...}}``;
    handle both.
    """
    if not isinstance(item, dict):
        return None
    if "v_id" in item or "primary_id" in item or "v_type" in item:
        return item
    for key in ("c", "m", "res", "v"):
        v = item.get(key)
        if isinstance(v, dict):
            return v
    return None


def _attrs_from_vertex_row(v: dict[str, Any]) -> dict[str, Any]:
    attr = v.get("attributes")
    if isinstance(attr, dict):
        return attr
    out: dict[str, Any] = {}
    for k in (
        "user_id",
        "epoch_added",
        "epoch_processed",
        "conversation_id",
        "user_content",
        "system_content",
        "tracelog",
        "rolling_summary",
        "summary_turn_count",
        "summary_updated_epoch",
        "text",
        "intent",
        "weight",
        "epoch_last_used",
    ):
        if k in v and k != "attributes":
            out[k] = v[k]
    return out


def _primary_id_from_vertex_row(v: dict[str, Any]) -> str | None:
    pid = v.get("id") or v.get("primary_id") or v.get("v_id")
    if pid is None:
        return None
    return str(pid)


def list_conversations_for_user(
    conn: TigerGraphConnection,
    graphname: str,
    user_id: str,
) -> list[dict[str, Any]]:
    """
    Return conversation summaries for the sidebar (JSON-serializable dicts).
    """
    if not tg_memory_enabled(graphname):
        return []
    init_memory_schema(conn, graphname)
    try:
        raw = conn.runInstalledQuery(
            _QUERY_LIST_CONVOS,
            params={"uid": user_id},
        )
    except Exception:
        logger.warning("list_conversations_for_user failed", exc_info=True)
        return []

    rows: list[dict[str, Any]] = []
    for block in raw or []:
        if "rows" not in block:
            continue
        for item in block["rows"]:
            vrow = _vertex_row_from_print_item(item) if isinstance(item, dict) else None
            if not vrow:
                continue
            pk = _primary_id_from_vertex_row(vrow)
            if not pk:
                continue
            attrs = _attrs_from_vertex_row(vrow)
            ea = attrs.get("epoch_added")
            ep = attrs.get("epoch_processed")
            cid = app_conversation_id_from_vertex_pk(pk)
            ts_create = _epoch_u_to_iso(ea)
            ts_update = _epoch_u_to_iso(ep) or ts_create
            rows.append(
                {
                    "conversation_id": cid,
                    "user_id": attrs.get("user_id") or user_id,
                    "create_ts": ts_create,
                    "update_ts": ts_update,
                    "name": "",
                }
            )

    rows.sort(
        key=lambda r: r.get("update_ts") or r.get("create_ts") or "",
        reverse=True,
    )
    return rows


def list_messages_sorted_by_epoch(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """
    Raw TG **message** rows for one conversation (application conversation_id attribute).
    Sorted ascending by epoch_added.
    """
    if not tg_memory_enabled(graphname):
        return []
    init_memory_schema(conn, graphname)
    try:
        raw = conn.runInstalledQuery(
            _QUERY_LIST_MSGS,
            params={"conv_id": conversation_id},
        )
    except Exception:
        logger.warning("list_messages_for_conversation failed", exc_info=True)
        return []

    parsed: list[tuple[int, dict[str, Any]]] = []
    for block in raw or []:
        if "rows" not in block:
            continue
        for item in block["rows"]:
            vrow = _vertex_row_from_print_item(item) if isinstance(item, dict) else None
            if not vrow:
                continue
            pk = _primary_id_from_vertex_row(vrow)
            if not pk:
                continue
            attrs = _attrs_from_vertex_row(vrow)
            try:
                ep = int(attrs.get("epoch_added") or 0)
            except (TypeError, ValueError):
                ep = 0
            parsed.append(
                (
                    ep,
                    {
                        "message_id": str(pk),
                        "user_content": attrs.get("user_content") or "",
                        "system_content": attrs.get("system_content") or "",
                        "epoch_added": attrs.get("epoch_added"),
                        "tracelog": attrs.get("tracelog") or "",
                    },
                )
            )

    parsed.sort(key=lambda x: x[0])
    return [p[1] for p in parsed]


def list_messages_for_memory(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """
    Message rows for memory / summarization: same ordering as ``list_messages_sorted_by_epoch``,
    but **never** includes ``tracelog`` in returned dicts.
    """
    if not tg_memory_enabled(graphname):
        return []
    init_memory_schema(conn, graphname)
    try:
        raw = conn.runInstalledQuery(
            _QUERY_LIST_MSGS_MEMORY,
            params={"conv_id": conversation_id},
        )
    except Exception:
        logger.warning("list_messages_for_memory failed", exc_info=True)
        return []

    parsed: list[tuple[int, dict[str, Any]]] = []
    for block in raw or []:
        if "rows" not in block:
            continue
        for item in block["rows"]:
            vrow = _vertex_row_from_print_item(item) if isinstance(item, dict) else None
            if not vrow:
                continue
            pk = _primary_id_from_vertex_row(vrow)
            if not pk:
                continue
            attrs = _attrs_from_vertex_row(vrow)
            try:
                ep = int(attrs.get("epoch_added") or 0)
            except (TypeError, ValueError):
                ep = 0
            parsed.append(
                (
                    ep,
                    {
                        "message_id": str(pk),
                        "user_content": attrs.get("user_content") or "",
                        "system_content": attrs.get("system_content") or "",
                        "epoch_added": attrs.get("epoch_added"),
                    },
                )
            )

    parsed.sort(key=lambda x: x[0])
    return [p[1] for p in parsed]


def get_conversation_summary(
    conn: TigerGraphConnection, graphname: str, conversation_id: str
) -> dict[str, Any]:
    """
    Read ``rolling_summary``, ``summary_turn_count``, ``summary_updated_epoch`` from the
    **conversation** vertex (PRIMARY_ID = hyphenless / normalized id).
    """
    out: dict[str, Any] = {
        "rolling_summary": "",
        "summary_turn_count": 0,
        "summary_updated_epoch": 0,
    }
    if not tg_memory_enabled(graphname):
        return out
    init_memory_schema(conn, graphname)
    conv_pk = _conversation_vertex_primary_id(conversation_id)
    try:
        raw = conn.runInstalledQuery(
            _QUERY_GET_SUMMARY,
            params={"conv_pk": conv_pk},
        )
    except Exception:
        logger.warning("get_conversation_summary failed", exc_info=True)
        return out

    for block in raw or []:
        if "rows" not in block:
            continue
        for item in block["rows"]:
            vrow = _vertex_row_from_print_item(item) if isinstance(item, dict) else None
            if not vrow:
                continue
            attrs = _attrs_from_vertex_row(vrow)
            out["rolling_summary"] = str(attrs.get("rolling_summary") or "")
            try:
                out["summary_turn_count"] = int(attrs.get("summary_turn_count") or 0)
            except (TypeError, ValueError):
                out["summary_turn_count"] = 0
            try:
                out["summary_updated_epoch"] = int(attrs.get("summary_updated_epoch") or 0)
            except (TypeError, ValueError):
                out["summary_updated_epoch"] = 0
            return out
    return out


def update_conversation_summary(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    *,
    new_summary: str,
    new_turn_count: int,
    epoch: int,
) -> None:
    if not tg_memory_enabled(graphname):
        return
    init_memory_schema(conn, graphname)
    conv_pk = _conversation_vertex_primary_id(conversation_id)
    try:
        conn.runInstalledQuery(
            _QUERY_UPDATE_SUMMARY,
            params={
                "conv_pk": conv_pk,
                "new_summary": new_summary or "",
                "new_turn_count": int(new_turn_count),
                "epoch": int(epoch),
            },
        )
    except Exception:
        logger.warning(
            "update_conversation_summary failed conv=%s graph=%s",
            conversation_id,
            graphname,
            exc_info=True,
        )


def save_summary_vertex(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    *,
    summary_id: str,
    text: str,
    intent: str,
    weight: float,
    epoch_added: int,
    covered_message_ids: list[str],
    max_covered: int,
) -> None:
    """Persist a ``summary`` vertex plus ``has_summary``; add ``covers_message`` edges when ids are given."""
    if not tg_memory_enabled(graphname):
        return
    init_memory_schema(conn, graphname)
    conv_pk = _conversation_vertex_primary_id(conversation_id)
    mids = [str(x) for x in covered_message_ids if x][: int(max_covered)]
    if not mids:
        logger.info(
            "[TG_MEMORY] save_summary_vertex: no message ids; writing summary + has_summary only "
            "(no covers_message edges) conv=%s graph=%s",
            conversation_id,
            graphname,
        )
    ts = int(epoch_added)
    try:
        conn.upsertVertex(
            "summary",
            summary_id,
            attributes={
                "conversation_id": conversation_id,
                "text": text or "",
                "intent": intent or "",
                "weight": float(weight),
                "epoch_added": ts,
                "epoch_last_used": ts,
            },
        )
        conn.upsertEdge(
            "conversation",
            conv_pk,
            "has_summary",
            "summary",
            summary_id,
        )
        for mid in mids:
            conn.upsertEdge(
                "summary",
                summary_id,
                "covers_message",
                "message",
                mid,
            )
    except Exception:
        logger.warning(
            "save_summary_vertex failed summary_id=%s conv=%s graph=%s",
            summary_id,
            conversation_id,
            graphname,
            exc_info=True,
        )


def _delete_conversation_summaries_gsql(
    conn: TigerGraphConnection, graphname: str, conversation_id: str
) -> None:
    if not tg_memory_enabled(graphname):
        return
    init_memory_schema(conn, graphname)
    conv_pk = _conversation_vertex_primary_id(conversation_id)
    try:
        conn.runInstalledQuery(_QUERY_DELETE_SUMMARIES, params={"conv_pk": conv_pk})
    except Exception:
        logger.warning(
            "delete_conversation_summaries failed conv=%s graph=%s",
            conversation_id,
            graphname,
            exc_info=True,
        )


def conversation_rows_to_ui_messages(
    conversation_id: str,
    rows: list[dict[str, Any]],
    *,
    model_name: str = "unknown",
) -> list[dict[str, Any]]:
    """
    Expand each TG exchange vertex into user + system messages (legacy SQLite / UI shape).
    """
    out: list[dict[str, Any]] = []
    prev_assistant_id: str | None = None
    for row in rows:
        mid = row["message_id"]
        user_mid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"graphrag:{mid}:user"))
        ts = _epoch_u_to_iso(row.get("epoch_added"))
        tl_raw = row.get("tracelog") or ""
        qs: dict[str, Any] | None = None
        answered = False
        resp_time = 0.0
        if isinstance(tl_raw, str) and tl_raw.strip():
            try:
                tl = json.loads(tl_raw)
                if isinstance(tl, dict):
                    qs = tl.get("query_sources")
                    answered = bool(tl.get("answered_question", False))
                    resp_time = float(tl.get("response_time") or 0.0)
            except json.JSONDecodeError:
                qs = None

        out.append(
            {
                "conversation_id": conversation_id,
                "message_id": user_mid,
                "parent_id": prev_assistant_id,
                "model": model_name,
                "content": row.get("user_content") or "",
                "role": "user",
                "response_time": 0.0,
                "answered_question": False,
                "response_type": "history",
                "query_sources": None,
                "create_ts": ts,
                "update_ts": ts,
            }
        )
        out.append(
            {
                "conversation_id": conversation_id,
                "message_id": mid,
                "parent_id": user_mid,
                "model": model_name,
                "content": row.get("system_content") or "",
                "role": "system",
                "response_time": resp_time,
                "answered_question": answered,
                "response_type": "inquiryai",
                "query_sources": qs if isinstance(qs, dict) else {},
                "create_ts": ts,
                "update_ts": ts,
                # Original user message that produced this assistant reply,
                # so the Trace page can show "Original Query" for history items
                # without an extra round-trip.
                "user_query": row.get("user_content") or "",
            }
        )
        prev_assistant_id = mid
    return out


def agent_history_from_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build ``question_for_agent`` history from TG exchange rows (ordered ascending)."""
    hist: list[dict[str, Any]] = []
    for row in rows:
        ts = _epoch_u_to_iso(row.get("epoch_added"))
        hist.append(
            {
                "query": row.get("user_content") or "",
                "response": row.get("system_content") or "",
                "create_ts": ts,
                "update_ts": ts,
            }
        )
    return hist


def verify_conversation_owner(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    user_id: str,
) -> bool:
    """True if a conversation vertex exists and ``user_id`` matches."""
    if not tg_memory_enabled(graphname):
        return False
    init_memory_schema(conn, graphname)
    conv_vid = _conversation_vertex_primary_id(conversation_id)
    try:
        df = conn.getVertexDataFrameById("conversation", conv_vid)
        if df is None or len(df) == 0:
            return False
        row = df.iloc[0]
        owner = row.get("user_id", "")
        return str(owner) == str(user_id)
    except Exception:
        logger.warning("verify_conversation_owner failed", exc_info=True)
        return False


def delete_message_vertices(
    conn: TigerGraphConnection,
    graphname: str,
    message_ids: list[str],
) -> int:
    """Permanently delete message vertices by primary id. Returns count deleted."""
    if not message_ids or not tg_memory_enabled(graphname):
        return 0
    init_memory_schema(conn, graphname)
    ids = [str(m) for m in message_ids if m]
    if not ids:
        return 0
    try:
        conn.delVerticesById("message", ids, permanent=True)
        return len(ids)
    except Exception:
        logger.warning(
            "delete_message_vertices failed graph=%s count=%s",
            graphname,
            len(ids),
            exc_info=True,
        )
        raise


def delete_conversation_thread(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
) -> int:
    """
    Delete all **message** vertices for ``conversation_id`` and the **conversation** vertex.
    Returns number of message vertices deleted.
    """
    if not tg_memory_enabled(graphname):
        return 0
    init_memory_schema(conn, graphname)
    _delete_conversation_summaries_gsql(conn, graphname, conversation_id)
    msgs = list_messages_sorted_by_epoch(conn, graphname, conversation_id)
    ids = [m["message_id"] for m in msgs]
    if ids:
        try:
            conn.delVerticesById("message", ids, permanent=True)
        except Exception:
            logger.warning("delete messages failed", exc_info=True)
            raise
    conv_vid = _conversation_vertex_primary_id(conversation_id)
    try:
        conn.delVerticesById("conversation", conv_vid, permanent=True)
    except Exception:
        logger.warning("delete conversation vertex failed", exc_info=True)
        raise
    return len(ids)


def get_message_tracelog(
    conn: TigerGraphConnection,
    graphname: str,
    message_id: str,
) -> dict[str, Any] | None:
    """
    Fetch and parse ``message.tracelog`` JSON from TigerGraph memory.
    Returns None when the message does not exist or tracelog is empty/invalid.
    """
    try:
        init_memory_schema(conn, graphname)
        df = conn.getVertexDataFrameById("message", message_id)
        if df is None or len(df) == 0:
            return None

        row = df.iloc[0]
        raw = row.get("tracelog", "")
        if not isinstance(raw, str) or not raw.strip():
            return None

        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"tracelog": parsed}
    except Exception:
        logger.warning(
            "tg_memory: failed to fetch tracelog message_id=%s graph=%s",
            message_id,
            graphname,
            exc_info=True,
        )
        return None
