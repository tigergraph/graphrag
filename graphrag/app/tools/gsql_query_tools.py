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

"""Discover tagged GSQL queries as agent catalog tools.

Not a chat tool. Called in-process before plan/react so tagged queries
already appear in ``## Tools``. Each tagged query runs via
``conn.runInstalledQuery``.

User vs original is a keyword on the native GSQL description
(``UPDATE`` / ``SHOW`` / ``DROP DESCRIPTION OF QUERY``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tools.tool_registry import ToolSpec

logger = logging.getLogger(__name__)

TOOL_PREFIX = "graphrag__gsql__"
TOOL_MARKER = "[GRAPHRAG_TOOL]"

# TG SHOW DESCRIPTION OF QUERY * prints:
#   The description of queryName: text
# Older / other formats may use a leading dash.
_SHOW_LINE_RE = re.compile(
    r"^\s*(?:The description of\s+|-\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def tool_name_for(query_name: str) -> str:
    return f"{TOOL_PREFIX}{query_name}"


def _unescape_desc(text: str | None) -> str:
    t = (text or "").strip()
    if not t or t.lower() == "null":
        return ""
    return t.replace("\\n", "\n").replace('\\"', '"')


def strip_tool_marker(description: str | None) -> str:
    text = _unescape_desc(description)
    if text.startswith(TOOL_MARKER):
        text = text[len(TOOL_MARKER) :]
    else:
        text = text.replace(TOOL_MARKER, "")
    return text.lstrip("\n").strip()


def _parse_show_description(output: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    current_name: str | None = None
    current_desc: list[str] = []

    def _flush():
        nonlocal current_name, current_desc
        if current_name:
            rows.append((current_name, "\n".join(current_desc).strip()))
        current_name = None
        current_desc = []

    for raw in (output or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().lower().startswith("using graph"):
            continue
        m = _SHOW_LINE_RE.match(line)
        if m:
            _flush()
            current_name = m.group(1)
            current_desc = [m.group(2)]
            continue
        if current_name is not None:
            current_desc.append(line.strip())
    _flush()
    return [(name, _unescape_desc(desc)) for name, desc in rows]


def _rows_from_query_description(raw) -> list[tuple[str, str]]:
    if not raw:
        return []
    if isinstance(raw, str):
        return _parse_show_description(raw)
    if isinstance(raw, dict):
        if "queryName" in raw or "name" in raw:
            raw = [raw]
        else:
            out = []
            for k, v in raw.items():
                if isinstance(v, str):
                    out.append((str(k), v))
                elif isinstance(v, dict):
                    out.append((str(k), v.get("description") or ""))
            return out
    if isinstance(raw, list):
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("queryName") or item.get("name") or item.get("query")
            desc = item.get("description") or item.get("desc") or ""
            if name:
                out.append((str(name), str(desc)))
        return out
    return []


def _fetch_description_rows(
    conn, graphname: str, query_names: list[str] | None = None
) -> list[tuple[str, str]]:
    """Read query descriptions. ``getQueryDescription('*')`` is not valid on TG."""
    conn.graphname = graphname
    by_name: dict[str, str] = {}
    if query_names:
        try:
            for name, desc in _rows_from_query_description(
                conn.getQueryDescription(query_names)
            ):
                by_name[name] = _unescape_desc(desc)
        except Exception as exc:
            logger.debug("getQueryDescription(list) failed: %s", exc)
    from common.db.schema_utils import gsql_output_error
    out = conn.gsql(f"USE GRAPH {graphname}\nSHOW DESCRIPTION OF QUERY *\n")
    text = out if isinstance(out, str) else str(out)
    err = gsql_output_error(text)
    if err:
        logger.warning("SHOW DESCRIPTION OF QUERY * failed: %s", err)
    else:
        for name, desc in _parse_show_description(text):
            if name not in by_name or (TOOL_MARKER in desc and TOOL_MARKER not in by_name.get(name, "")):
                by_name[name] = desc
    return list(by_name.items())


def list_query_catalog(conn, graphname: str) -> dict[str, list[dict]]:
    """Installed queries split into original vs ``[GRAPHRAG_TOOL]``-tagged."""
    from common.db.migrate import get_installed_query_names

    try:
        installed_names = sorted(get_installed_query_names(conn, graphname))
    except Exception as exc:
        logger.warning("get_installed_query_names failed: %s", exc)
        installed_names = []
    desc_by_name = dict(_fetch_description_rows(conn, graphname, installed_names))
    if not installed_names:
        installed_names = sorted(desc_by_name.keys())

    registered: list[dict] = []
    original: list[dict] = []
    for name in installed_names:
        desc = desc_by_name.get(name, "") or ""
        if TOOL_MARKER in desc:
            registered.append({
                "function_header": name,
                "description": strip_tool_marker(desc),
            })
        else:
            original.append({
                "function_header": name,
                "description": desc.strip(),
            })
    logger.info(
        "query catalog graph=%s registered=%s original=%s",
        graphname,
        [r["function_header"] for r in registered],
        [o["function_header"] for o in original],
    )
    return {"registered": registered, "installed": original}


def list_tagged_queries(conn, graphname: str) -> list[dict]:
    """``SHOW DESCRIPTION OF QUERY *`` / ``getQueryDescription``, keep ``[GRAPHRAG_TOOL]``."""
    return list_query_catalog(conn, graphname)["registered"]


def _gsql_type_to_json(t: str) -> dict:
    u = (t or "STRING").upper()
    if u in ("INT", "UINT", "UINT64", "INT64"):
        return {"type": "integer"}
    if u in ("FLOAT", "DOUBLE"):
        return {"type": "number"}
    if u in ("BOOL", "BOOLEAN"):
        return {"type": "boolean"}
    if u.startswith("SET") or u.startswith("LIST") or u.startswith("BAG"):
        return {"type": "array", "items": {"type": "string"}}
    return {"type": "string"}


def _iter_params(raw_input) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if isinstance(raw_input, dict):
        for k, v in raw_input.items():
            if isinstance(v, dict):
                v = v.get("type") or v.get("paramType") or "STRING"
            items.append((str(k), str(v)))
        return items
    if isinstance(raw_input, list):
        for x in raw_input:
            if isinstance(x, dict) and x:
                k = list(x.keys())[0]
                v = x[k]
                if isinstance(v, dict):
                    v = v.get("type") or v.get("paramType") or "STRING"
                items.append((str(k), str(v)))
            elif isinstance(x, str):
                items.append((x, "STRING"))
    return items


def args_schema_for(conn, query_name: str) -> dict:
    try:
        meta = conn.getQueryMetadata(query_name) or {}
        raw_input = meta.get("input") or []
    except Exception as exc:
        logger.warning("getQueryMetadata(%s) failed: %s", query_name, exc)
        raw_input = []
    props = {}
    required = []
    for pname, ptype in _iter_params(raw_input):
        js = _gsql_type_to_json(str(ptype))
        js["description"] = f"Parameter {pname} ({ptype})"
        props[pname] = js
        required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _result_is_empty(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, (list, dict, str)) and len(result) == 0:
        return True
    return False


def _make_runner(query_name: str):
    def _fn(ctx, **kwargs):
        ctx.emit(f"Running registered query {query_name}")
        try:
            result = ctx.conn.runInstalledQuery(query_name, params=kwargs or {})
        except Exception as exc:
            logger.warning("registered query %s failed: %s", query_name, exc)
            return {
                "ok": False,
                "summary": f"{query_name} failed: {exc}",
                "context": None,
                "citations": [],
            }
        if _result_is_empty(result):
            return {
                "ok": False,
                "summary": f"{query_name} returned no rows",
                "context": None,
                "citations": [],
            }
        return {
            "ok": True,
            "summary": f"{query_name} returned results",
            "context": {
                "function_call": f"runInstalledQuery({query_name!r})",
                "query_name": query_name,
                "result": result,
            },
            "citations": [],
        }

    _fn.__name__ = f"run_{query_name}"
    return _fn


def discover_tagged_query_tools(conn, graphname: str) -> dict[str, ToolSpec]:
    """Build a ToolSpec per tagged GSQL query. Empty dict on failure."""
    try:
        tagged = list_tagged_queries(conn, graphname)
    except Exception as exc:
        logger.warning("SHOW DESCRIPTION tagged-query list failed: %s", exc)
        return {}
    out: dict[str, ToolSpec] = {}
    for row in tagged:
        name = row.get("function_header") or ""
        if not name:
            continue
        spec_name = tool_name_for(name)
        out[spec_name] = ToolSpec(
            name=spec_name,
            description=row.get("description") or f"Run installed query {name}.",
            args_schema_json=args_schema_for(conn, name),
            fn=_make_runner(name),
        )
    return out
