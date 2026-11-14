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

"""Render and install retrieval queries against the live domain schema."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

from common.db.schema_utils import gsql_output_error

logger = logging.getLogger(__name__)


_RETRIEVER_DIR = "common/gsql/supportai/retrievers"


TEMPLATED_RETRIEVERS: tuple = (
    "GraphRAG_Hybrid_Search",
    "GraphRAG_Hybrid_Vector_Search",
    "GraphRAG_Community_Search",
    "GraphRAG_Community_Vector_Search",
    "Chunk_Sibling_Vector_Search",
    "Content_Similarity_Vector_Search",
)


def _with_path(path_expr: str, text_expr: str) -> str:
    """GSQL expression: the chunk text, after the heading path when the chunk
    has one."""
    return (
        f'(CASE WHEN {path_expr} == "" THEN {text_expr} '
        f'ELSE {path_expr} + "\\n\\n" + {text_expr} END)'
    )


# (needle, replacement) pairs that show a chunk's heading path before its
# text. Only chunk-level text is changed; Document has no heading path.
_CHUNK_CONTEXT_SUBSTITUTIONS = (
    # GraphRAG_Hybrid_Vector_Search / GraphRAG_Hybrid_Search, chunk branch
    ("ACCUM s.@context += t.text\n",
     f"ACCUM s.@context += {_with_path('s.heading_path', 't.text')}\n"),
    # Chunk_Sibling_Vector_Search
    ("ACCUM s.@content += t.text;",
     f"ACCUM s.@content += {_with_path('s.heading_path', 't.text')};"),
    # Content_Similarity_Vector_Search: s can be a Document or a
    # DocumentChunk, so the path is read with getAttr, which TigerGraph does
    # not allow inside CASE.
    ("@@final_retrieval += (s.id -> tgt.text)",
     'IF s.type == "DocumentChunk" AND s.getAttr("heading_path", "STRING") != "" THEN '
     '@@final_retrieval += (s.id -> s.getAttr("heading_path", "STRING") + "\\n\\n" + tgt.text) '
     'ELSE @@final_retrieval += (s.id -> tgt.text) END'),
)

# GraphRAG_Community_Vector_Search / GraphRAG_Community_Search: the chunk
# branch reaches Content from a DocumentChunk named d.
_COMMUNITY_CHUNK_BRANCH = "-(<HAS_CONTENT)- DocumentChunk:d"
_COMMUNITY_TEXT = "m.@context += c.text"


def _chunk_context_pattern(body: str) -> str:
    """Show each chunk's heading path before its text."""
    for needle, replacement in _CHUNK_CONTEXT_SUBSTITUTIONS:
        body = body.replace(needle, replacement)
    start = body.find(_COMMUNITY_CHUNK_BRANCH)
    if start >= 0:
        at = body.find(_COMMUNITY_TEXT, start)
        if at >= 0:
            body = (
                body[:at]
                + f"m.@context += {_with_path('d.heading_path', 'c.text')}"
                + body[at + len(_COMMUNITY_TEXT):]
            )
    return body


def _hop_edge_pattern(body: str, domain_edges: Iterable[str]) -> str:
    """Append directed domain edges to the hybrid-walk hop pattern."""
    edges = sorted(set(e for e in domain_edges if e))
    if not edges:
        return body
    needle = "IS_AFTER>):e"
    if needle not in body:
        return body
    addition = "|" + "|".join(f"{e}>" for e in edges)
    return body.replace(needle, f"IS_AFTER>{addition}):e")


def _community_member_pattern(
    body: str,
    domain_vts: Iterable[str],
    include_entity: bool,
) -> str:
    """Expand the community-walk start type to include domain VTs."""
    vts = sorted(set(v for v in domain_vts if v))
    if not vts:
        return body
    types = (["Entity"] + vts) if include_entity else vts
    member = types[0] if len(types) == 1 else "(" + "|".join(types) + ")"
    needle = "CONTAINS_ENTITY>)- Entity:v -(IN_COMMUNITY>"
    if needle not in body:
        return body
    return body.replace(
        needle,
        f"CONTAINS_ENTITY>)- {member}:v -(IN_COMMUNITY>",
    )


def resolve_include_entity(
    graphrag_config_get,
    has_domain_schema: bool,
) -> bool:
    """Resolve effective ``retrieval_include_entity``. Default: ``False``
    when a schema exists, ``True`` otherwise. Explicit config wins.
    """
    configured = graphrag_config_get("retrieval_include_entity")
    if configured is None:
        return not has_domain_schema
    return bool(configured)


def render_retriever_body(
    template_text: str,
    *,
    domain_vts: Iterable[str],
    domain_edges: Iterable[str],
    include_entity: bool,
    chunk_context: bool = False,
) -> str:
    """Apply every schema-aware substitution to one retriever body.
    ``chunk_context`` is true when DocumentChunk has the heading_path
    attribute."""
    body = template_text
    body = _hop_edge_pattern(body, domain_edges)
    body = _community_member_pattern(body, domain_vts, include_entity=include_entity)
    if chunk_context:
        body = _chunk_context_pattern(body)
    return body


def load_template(query_name: str, retriever_dir: str = _RETRIEVER_DIR) -> str:
    return (Path(retriever_dir) / f"{query_name}.gsql").read_text()


def render_retrievers(
    domain_vts: Iterable[str],
    domain_edges: Iterable[str],
    include_entity: bool,
    retriever_dir: str = _RETRIEVER_DIR,
    chunk_context: bool = False,
) -> dict:
    """Return ``{query_name: rendered_body}`` for every templated retriever."""
    rendered: dict = {}
    for q in TEMPLATED_RETRIEVERS:
        try:
            text = load_template(q, retriever_dir)
        except FileNotFoundError:
            logger.warning(f"render_retrievers: template not found for {q}, skipped")
            continue
        rendered[q] = render_retriever_body(
            text,
            domain_vts=domain_vts,
            domain_edges=domain_edges,
            include_entity=include_entity,
            chunk_context=chunk_context,
        )
    return rendered


def _install_block(graphname: str, query_name: str, body: str) -> str:
    return (
        f"USE GRAPH {graphname}\n"
        f"{body}\n"
        f"INSTALL QUERY {query_name}\n"
    )


def _summarize(out) -> str:
    s = str(out)
    s = s.replace("\n", " | ")
    return s[:200]


def install_retrievers(
    conn,
    graphname: str,
    domain_vts: Iterable[str],
    domain_edges: Iterable[str],
    include_entity: bool,
    retriever_dir: str = _RETRIEVER_DIR,
    progress: Optional["Callable[[str], None]"] = None,
    chunk_context: bool = False,
) -> dict:
    """Render and install every templated retriever (sync).

    *progress* is an optional callback invoked once per query with a
    short status message; lets the caller surface per-query progress
    in a UI (init dialog poll, etc.).
    """
    rendered = render_retrievers(
        domain_vts, domain_edges, include_entity, retriever_dir, chunk_context
    )
    logger.info(
        f"install_retrievers: graph={graphname} include_entity={include_entity} "
        f"vts={len(list(domain_vts))} edges={len(list(domain_edges))} "
        f"rendered={list(rendered.keys())}"
    )
    # Group the four templated retrievers into two user-facing
    # status messages — the text/vector variants of each family
    # install back-to-back and a per-query message flickers too
    # fast to be useful. The mapping is exhaustive over the
    # current ``TEMPLATED_RETRIEVERS`` set; new entries fall
    # through to a single "Installing retriever queries" message.
    _GROUP_MESSAGE = {
        "GraphRAG_Hybrid_Search": ("hybrid", "Installing hybrid retriever queries"),
        "GraphRAG_Hybrid_Vector_Search": ("hybrid", "Installing hybrid retriever queries"),
        "GraphRAG_Community_Search": ("community", "Installing community retriever queries"),
        "GraphRAG_Community_Vector_Search": ("community", "Installing community retriever queries"),
    }

    results: dict = {}
    emitted_groups: set = set()
    for query_name, body in rendered.items():
        if progress is not None:
            group_key, group_msg = _GROUP_MESSAGE.get(
                query_name, ("_other", "Installing retriever queries")
            )
            if group_key not in emitted_groups:
                try:
                    progress(group_msg)
                except Exception:
                    pass
                emitted_groups.add(group_key)
        block = _install_block(graphname, query_name, body)
        try:
            out = conn.gsql(block)
            results[query_name] = out
            err = gsql_output_error(out) if isinstance(out, str) else None
            if err:
                logger.warning(
                    f"install_retrievers: {query_name} install reported "
                    f"errors: {_summarize(out)}"
                )
            else:
                logger.info(
                    f"install_retrievers: {query_name} OK: {_summarize(out)}"
                )
        except Exception as e:
            logger.error(f"install_retrievers: {query_name} install raised: {e}")
            results[query_name] = f"ERROR: {e}"
    return results


async def install_retrievers_async(
    conn,
    graphname: str,
    domain_vts: Iterable[str],
    domain_edges: Iterable[str],
    include_entity: bool,
    retriever_dir: str = _RETRIEVER_DIR,
    sem: Optional["object"] = None,
    chunk_context: bool = False,
) -> dict:
    """Render and install every templated retriever (async)."""
    rendered = render_retrievers(
        domain_vts, domain_edges, include_entity, retriever_dir, chunk_context
    )
    results: dict = {}
    for query_name, body in rendered.items():
        block = _install_block(graphname, query_name, body)
        try:
            if sem is not None:
                async with sem:
                    out = await conn.gsql(block)
            else:
                out = await conn.gsql(block)
            results[query_name] = out
            err = gsql_output_error(out) if isinstance(out, str) else None
            if err:
                logger.warning(
                    f"install_retrievers_async: {query_name} install "
                    f"reported errors: {str(out)[:300]}"
                )
        except Exception as e:
            logger.error(
                f"install_retrievers_async: {query_name} install raised: {e}"
            )
            results[query_name] = f"ERROR: {e}"
    return results
