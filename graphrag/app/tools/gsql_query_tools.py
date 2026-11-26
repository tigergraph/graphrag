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

"""Installed GSQL queries as agent tools, in two stages.

Stage 1, description. A query's TigerGraph description holds a tool block in
a fixed format (see ``parse_tool_block``); each parameter's meaning, format and
example go in TigerGraph's own parameter descriptions. Writing the block does
not make the query a tool.

Stage 2, registration. A graph admin discovers the installed queries, sees
which ones have a valid block, and registers some of them. Registration saves
each tool (name, description, parameters) in
``configs/graph_configs/<graph>/query_tools.json``. The agents build their
tools from that file only, so answering a question makes no TigerGraph call to
find them; TigerGraph is called when a tool runs, with the signed-in user's
connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from tools.tool_registry import ToolSpec

logger = logging.getLogger(__name__)

TOOL_PREFIX = "graphrag__gsql__"
BLOCK_START = "[GRAPHRAG_TOOL]"
BLOCK_END = "[/GRAPHRAG_TOOL]"
TOOLS_FILE = "query_tools.json"
# Query names per getQueryDescription call; pyTigerGraph puts them in the URL.
DESCRIPTION_BATCH = 50
# Longest query result, in characters of JSON, handed to an agent.
MAX_RESULT_CHARS = 30000

# Labels of the tool block, in the order they are written.
FIELDS = (
    ("returns", "Returns"),
    ("use_for", "Use for"),
    ("do_not_use_for", "Do not use for"),
    ("output", "Output"),
)
REQUIRED_FIELDS = ("returns", "use_for")
_LABEL_RE = re.compile(
    r"^\s*(returns|use for|do not use for|output)\s*:\s*(.*)$", re.IGNORECASE
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def tool_name_for(query_name: str) -> str:
    return f"{TOOL_PREFIX}{query_name}"


def valid_query_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


# --------------------------------------------------------------------------
# Stage 1: the tool block in a query description
# --------------------------------------------------------------------------

def _clean(text: Optional[str]) -> str:
    t = (text or "").strip()
    if t.lower() == "null":
        return ""
    return t.replace("\\n", "\n").replace('\\"', '"')


def _split_block(text: str) -> tuple[Optional[str], str]:
    """(the text inside the tool block or None, the text outside it)."""
    start = text.find(BLOCK_START)
    if start < 0:
        return None, text.strip()
    end = text.find(BLOCK_END, start + len(BLOCK_START))
    if end < 0:
        inner = text[start + len(BLOCK_START):]
        outside = text[:start]
    else:
        inner = text[start + len(BLOCK_START):end]
        outside = text[:start] + text[end + len(BLOCK_END):]
    return inner, outside.strip()


def parse_tool_block(description: Optional[str]) -> tuple[Optional[dict], str, Optional[str]]:
    """Parse the tool block of a query description.

    The block is::

        [GRAPHRAG_TOOL]
        Returns: <what the query returns>
        Use for: <the questions it answers>
        Do not use for: <questions it must not be used for>   (optional)
        Output: <the output fields and their units>           (optional)
        [/GRAPHRAG_TOOL]

    Text outside the block is kept as it is. A value may continue on the
    following lines. Returns ``(fields, other_text, problem)``: ``fields`` is
    None when there is no valid block, and ``problem`` then says why.
    """
    text = _clean(description)
    inner, outside = _split_block(text)
    if inner is None:
        return None, outside, "no tool block"
    if BLOCK_END not in text:
        return None, outside, f"the tool block has no closing {BLOCK_END}"
    fields: dict[str, str] = {}
    current = None
    for raw in inner.splitlines():
        m = _LABEL_RE.match(raw)
        if m:
            current = m.group(1).lower().replace(" ", "_")
            fields[current] = m.group(2).strip()
        elif raw.strip():
            if current is None:
                return None, outside, f"unexpected text in the tool block: {raw.strip()[:60]!r}"
            fields[current] = (fields[current] + " " + raw.strip()).strip()
    missing = [label for key, label in FIELDS if key in REQUIRED_FIELDS and not fields.get(key)]
    if missing:
        return None, outside, "the tool block has no " + " and no ".join(f'"{m}:"' for m in missing)
    return {key: fields.get(key, "") for key, _ in FIELDS}, outside, None


def compose_tool_block(fields: dict) -> str:
    lines = [BLOCK_START]
    for key, label in FIELDS:
        value = " ".join(str(fields.get(key) or "").split())
        if value:
            lines.append(f"{label}: {value}")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def with_tool_block(description: Optional[str], fields: dict) -> str:
    """The description with its tool block replaced by ``fields``; the text
    outside the block is kept."""
    _, outside = _split_block(_clean(description))
    block = compose_tool_block(fields)
    return f"{outside}\n{block}" if outside else block


def without_tool_block(description: Optional[str]) -> str:
    """The description with only the tool block removed."""
    _, outside = _split_block(_clean(description))
    return outside


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

def _inner_type(t: str) -> str:
    m = re.match(r"^\s*(?:LIST|SET|BAG)\s*<\s*(.+)\s*>\s*$", t, re.IGNORECASE)
    return m.group(1) if m else ""


def _vertex_type(t: str) -> Optional[str]:
    """'' for an untyped VERTEX, the type for VERTEX<T>, None otherwise."""
    m = re.match(r"^\s*VERTEX\s*(?:<\s*([A-Za-z_][A-Za-z0-9_]*)\s*>)?\s*$", t, re.IGNORECASE)
    if not m:
        return None
    return m.group(1) or ""


def _json_type(t: str) -> dict:
    u = (t or "STRING").strip()
    inner = _inner_type(u)
    if inner:
        return {"type": "array", "items": _json_type(inner)}
    vt = _vertex_type(u)
    if vt == "":
        return {
            "type": "object",
            "properties": {"id": {"type": "string"}, "type": {"type": "string"}},
            "required": ["id", "type"],
            "description": "A vertex: its id and its vertex type.",
        }
    if vt:
        return {"type": "string", "description": f"The id of a {vt} vertex."}
    up = u.upper()
    if up in ("INT", "INT64", "INT32"):
        return {"type": "integer"}
    if up in ("UINT", "UINT64", "UINT32"):
        return {"type": "integer", "minimum": 0}
    if up in ("FLOAT", "DOUBLE"):
        return {"type": "number"}
    if up in ("BOOL", "BOOLEAN"):
        return {"type": "boolean"}
    if up == "DATETIME":
        return {"type": "string", "description": "A date and time, YYYY-MM-DD HH:MM:SS."}
    return {"type": "string"}


def parse_default(raw: Optional[str], ptype: str) -> Any:
    """A TigerGraph default value (``5l``, ``1.5``, ``true``, ``abc``) as JSON."""
    if raw is None:
        return None
    up = (ptype or "").strip().upper()
    text = str(raw)
    try:
        if up.startswith(("INT", "UINT")):
            return int(text.rstrip("lLuU"))
        if up in ("FLOAT", "DOUBLE"):
            return float(text.rstrip("fFdD"))
    except ValueError:
        return text
    if up in ("BOOL", "BOOLEAN"):
        return text.strip().lower() == "true"
    return text


def build_params(described: list, typed: list) -> list[dict]:
    """Parameters from ``getQueryDescription`` (descriptions, defaults) and
    ``getQueryMetadata`` (types), in the query's order."""
    types: dict[str, str] = {}
    order: list[str] = []
    for item in typed or []:
        if isinstance(item, dict):
            for k, v in item.items():
                types[str(k)] = str(v)
                order.append(str(k))
    info = {str(p.get("paramName")): p for p in (described or []) if isinstance(p, dict)}
    for name in info:
        if name not in types:
            order.append(name)
    out = []
    for name in order:
        p = info.get(name, {})
        ptype = types.get(name, "STRING")
        has_default = "defaultValue" in p
        out.append({
            "name": name,
            "type": ptype,
            "description": _clean(p.get("description")),
            "default": parse_default(p.get("defaultValue"), ptype) if has_default else None,
            "has_default": has_default,
        })
    return out


def params_problem(params: list[dict]) -> Optional[str]:
    """Why the parameters keep the query from being a tool, or None."""
    missing = [p["name"] for p in params if not p["has_default"] and not p["description"]]
    if missing:
        return "no description for parameter " + ", ".join(missing)
    return None


def args_schema(params: list[dict]) -> dict:
    props: dict[str, Any] = {}
    required = []
    for p in params:
        js = dict(_json_type(p["type"]))
        parts = [p["description"]] if p["description"] else []
        if js.get("description"):
            parts.append(js["description"])
        if p["has_default"]:
            parts.append(f"Default: {json.dumps(p['default'], ensure_ascii=False)}.")
            js["default"] = p["default"]
        else:
            required.append(p["name"])
        js["description"] = " ".join(parts) or f"{p['name']} ({p['type']})"
        props[p["name"]] = js
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def agent_description(fields: dict) -> str:
    parts = [f"Returns {fields['returns'].rstrip('.')}." ]
    parts.append(f"Use for {fields['use_for'].rstrip('.')}.")
    if fields.get("do_not_use_for"):
        parts.append(f"Do not use for {fields['do_not_use_for'].rstrip('.')}.")
    if fields.get("output"):
        parts.append(f"Output: {fields['output'].rstrip('.')}.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Reading TigerGraph (Discover and Register only, never per question)
# --------------------------------------------------------------------------

def read_descriptions(conn, graphname: str, names: list[str]) -> dict[str, dict]:
    """Query descriptions and parameter descriptions, in one call per
    ``DESCRIPTION_BATCH`` names."""
    names = list(names or [])
    out = {}
    conn.graphname = graphname
    for i in range(0, len(names), DESCRIPTION_BATCH):
        rows = conn.getQueryDescription(names[i:i + DESCRIPTION_BATCH]) or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if isinstance(row, dict) and row.get("queryName"):
                out[str(row["queryName"])] = {
                    "description": _clean(row.get("description")),
                    "parameters": row.get("parameters") or [],
                }
    return out


def read_param_types(conn, query_name: str) -> list:
    meta = conn.getQueryMetadata(query_name) or {}
    return meta.get("input") or []


def _fingerprint(description: str, params: list[dict]) -> str:
    blob = json.dumps({"d": description, "p": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def evaluate_query(name: str, desc_row: Optional[dict], typed: Optional[list]) -> dict:
    """One query's state: ``{"name", "ok", "reason", "tool"}``."""
    if desc_row is None:
        return {"name": name, "ok": False, "reason": "no description", "tool": None}
    fields, _, problem = parse_tool_block(desc_row.get("description"))
    if fields is None:
        return {"name": name, "ok": False, "reason": problem, "tool": None}
    params = build_params(desc_row.get("parameters"), typed or [])
    problem = params_problem(params)
    if problem:
        return {"name": name, "ok": False, "reason": problem, "tool": None}
    tool = {
        "query": name,
        "fields": fields,
        "description": agent_description(fields),
        "params": params,
        "fingerprint": _fingerprint(desc_row.get("description") or "", params),
    }
    return {"name": name, "ok": True, "reason": None, "tool": tool}


def discover(conn, graphname: str, registered: dict[str, dict]) -> dict:
    """Installed queries, split into those with a valid tool block and the
    others with the reason they were skipped. Registered tools whose query is
    missing, or whose description or parameters changed, are marked."""
    from common.db.migrate import get_installed_query_names

    installed = sorted(get_installed_query_names(conn, graphname))
    rows = read_descriptions(conn, graphname, installed)
    matching, skipped = [], []
    for name in installed:
        row = rows.get(name)
        has_block = row is not None and BLOCK_START in (row.get("description") or "")
        typed = None
        if has_block:
            try:
                typed = read_param_types(conn, name)
            except Exception as exc:
                skipped.append({"name": name, "reason": f"its parameters could not be read: {exc}"})
                continue
        state = evaluate_query(name, row, typed)
        if not state["ok"]:
            if name in registered:
                state["registered"] = True
            skipped.append({k: v for k, v in state.items() if k != "tool"})
            continue
        tool = state["tool"]
        saved = registered.get(name)
        status = "not_registered"
        if saved:
            status = "registered" if saved.get("fingerprint") == tool["fingerprint"] else "changed"
        matching.append({**tool, "name": name, "status": status})
    missing = [{"name": n, "reason": "the query is not installed"}
               for n in sorted(registered) if n not in set(installed)]
    for s in skipped:
        if s.get("registered"):
            s["reason"] = f"registered, but {s['reason']}"
    return {"matching": matching, "skipped": skipped, "missing": missing}


# --------------------------------------------------------------------------
# Stage 2: the stored tool list
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def tools_path(graphname: str) -> str:
    from common.config import validate_graphname
    validate_graphname(graphname)
    return os.path.join("configs", "graph_configs", graphname, TOOLS_FILE)


def load_registered(graphname: str) -> dict[str, dict]:
    """The registered tools of a graph, by query name (empty when none)."""
    path = tools_path(graphname)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tools = {t["query"]: t for t in data.get("tools", []) if t.get("query")}
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    _cache[path] = (mtime, tools)
    return tools


def save_registered(graphname: str, tools: dict[str, dict]) -> None:
    path = tools_path(graphname)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tools": [tools[k] for k in sorted(tools)]}, f,
                      indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        _cache.pop(path, None)


def register(conn, graphname: str, names: list[str], user: Optional[str]) -> dict:
    """Read the named queries once and save those with a valid tool block."""
    from common.db.migrate import get_installed_query_names

    installed = get_installed_query_names(conn, graphname)
    rows = read_descriptions(conn, graphname, [n for n in names if n in installed])
    tools = dict(load_registered(graphname))
    done, failed = [], []
    for name in names:
        if name not in installed:
            failed.append({"name": name, "reason": "the query is not installed"})
            continue
        try:
            typed = read_param_types(conn, name)
        except Exception as exc:
            failed.append({"name": name, "reason": f"its parameters could not be read: {exc}"})
            continue
        state = evaluate_query(name, rows.get(name), typed)
        if not state["ok"]:
            failed.append({"name": name, "reason": state["reason"]})
            continue
        tools[name] = {**state["tool"], "registered_at": int(time.time()), "registered_by": user}
        done.append(name)
    if done:
        save_registered(graphname, tools)
    return {"registered": done, "failed": failed}


def unregister(graphname: str, names: list[str]) -> list[str]:
    tools = dict(load_registered(graphname))
    removed = [n for n in names if tools.pop(n, None) is not None]
    if removed:
        save_registered(graphname, tools)
    return removed


# --------------------------------------------------------------------------
# Agent tools from the stored list
# --------------------------------------------------------------------------

def _to_query_args(params: list[dict], kwargs: dict) -> dict:
    """Agent arguments as pyTigerGraph query parameters; an untyped VERTEX
    becomes ``(id, type)``."""
    types = {p["name"]: p["type"] for p in params}
    out = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        t = types.get(k, "")
        inner = _inner_type(t)
        if _vertex_type(t) == "" and isinstance(v, dict):
            v = (str(v.get("id")), str(v.get("type")))
        elif inner and _vertex_type(inner) == "" and isinstance(v, list):
            v = [(str(x.get("id")), str(x.get("type"))) if isinstance(x, dict) else x for x in v]
        out[k] = v
    return out


def _query_is_installed(conn, graphname: str, name: str) -> Optional[bool]:
    """Whether the query is installed; None when that cannot be checked.
    Called only after a tool failed."""
    from common.db.migrate import get_installed_query_names
    try:
        return name in get_installed_query_names(conn, graphname)
    except Exception as exc:
        logger.warning("Could not check whether %s is installed: %s", name, exc)
        return None


def _capped(result: Any) -> tuple[Any, bool]:
    """The result, or its JSON cut to ``MAX_RESULT_CHARS``, and whether it was cut."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return result, False
    return text[:MAX_RESULT_CHARS], True


def _make_runner(graphname: str, tool: dict):
    name = tool["query"]
    params = tool.get("params") or []

    def _fn(ctx, **kwargs):
        ctx.emit(f"Running registered query {name}")
        try:
            result = ctx.conn.runInstalledQuery(name, params=_to_query_args(params, kwargs))
        except Exception as exc:
            text = str(exc)
            if _query_is_installed(ctx.conn, graphname, name) is False:
                summary = (f"Registered query {name} is no longer installed on graph "
                           f"{graphname}. An admin needs to register it again or unregister it.")
            else:
                summary = (f"Registered query {name} failed: {text}. Check the arguments; if the "
                           "query changed in TigerGraph since it was registered, an admin needs "
                           "to register it again.")
            logger.warning("registered query %s failed: %s", name, text)
            return {"ok": False, "summary": summary, "context": None, "citations": []}
        if result is None or (isinstance(result, (list, dict, str)) and len(result) == 0):
            return {"ok": False, "summary": f"{name} returned no rows", "context": None, "citations": []}
        result, cut = _capped(result)
        summary = f"{name} returned results"
        if cut:
            summary += (f"; the result was cut to its first {MAX_RESULT_CHARS} characters. "
                        "Use parameters that return fewer rows for the rest")
        return {
            "ok": True,
            "summary": summary,
            "context": {"query_name": name, "result": result, "truncated": cut},
            "citations": [],
        }

    _fn.__name__ = f"run_{name}"
    return _fn


def registered_query_tools(graphname: str) -> dict[str, ToolSpec]:
    """Agent tools for the graph's registered queries, from the stored list.
    Makes no TigerGraph call."""
    out: dict[str, ToolSpec] = {}
    for name, tool in load_registered(graphname).items():
        spec_name = tool_name_for(name)
        out[spec_name] = ToolSpec(
            name=spec_name,
            description=tool.get("description") or f"Run installed query {name}.",
            args_schema_json=args_schema(tool.get("params") or []),
            fn=_make_runner(graphname, tool),
        )
    return out


# --------------------------------------------------------------------------
# Stage 1 writes (GSQL)
# --------------------------------------------------------------------------

def gsql_quote(text: str) -> str:
    escaped = ((text or "").replace("\\", "\\\\").replace('"', '\\"')
               .replace("\r\n", "\\n").replace("\n", "\\n"))
    return f'"{escaped}"'


def _gsql(conn, graphname: str, statement: str) -> str:
    from common.db.schema_utils import gsql_output_error
    out = conn.gsql(f"USE GRAPH {graphname}\n{statement}\n")
    text = out if isinstance(out, str) else str(out)
    err = gsql_output_error(text)
    if err:
        raise ValueError(err)
    return text


def write_description(conn, graphname: str, name: str, fields: dict,
                      param_descriptions: Optional[dict] = None) -> None:
    """Write the tool block into the query description (keeping the other
    text) and the given parameter descriptions. Does not register a tool."""
    rows = read_descriptions(conn, graphname, [name])
    current = rows.get(name, {})
    known = {str(p.get("paramName")): _clean(p.get("description"))
             for p in current.get("parameters", []) if isinstance(p, dict)}
    _gsql(conn, graphname,
          f"UPDATE DESCRIPTION OF QUERY {name} {gsql_quote(with_tool_block(current.get('description'), fields))}")
    for pname, text in (param_descriptions or {}).items():
        if pname not in known:
            raise ValueError(f"Query {name} has no parameter {pname}.")
        text = (text or "").strip()
        if text == known[pname]:
            continue
        if text:
            _gsql(conn, graphname, f"UPDATE DESCRIPTION OF QUERY_PARAM {name}.{pname} {gsql_quote(text)}")
        elif known[pname]:
            _gsql(conn, graphname, f"DROP DESCRIPTION OF QUERY_PARAM {name}.{pname}")


def remove_description_block(conn, graphname: str, name: str) -> None:
    """Remove only the tool block; other description text stays."""
    rows = read_descriptions(conn, graphname, [name])
    current = rows.get(name, {}).get("description") or ""
    if BLOCK_START not in current:
        return
    rest = without_tool_block(current)
    if rest:
        _gsql(conn, graphname, f"UPDATE DESCRIPTION OF QUERY {name} {gsql_quote(rest)}")
    else:
        _gsql(conn, graphname, f"DROP DESCRIPTION OF QUERY {name}")
