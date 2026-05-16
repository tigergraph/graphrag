# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

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
)


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
) -> str:
    """Apply every schema-aware substitution to one retriever body."""
    body = template_text
    body = _hop_edge_pattern(body, domain_edges)
    body = _community_member_pattern(body, domain_vts, include_entity=include_entity)
    return body


def load_template(query_name: str, retriever_dir: str = _RETRIEVER_DIR) -> str:
    return (Path(retriever_dir) / f"{query_name}.gsql").read_text()


def render_retrievers(
    domain_vts: Iterable[str],
    domain_edges: Iterable[str],
    include_entity: bool,
    retriever_dir: str = _RETRIEVER_DIR,
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
) -> dict:
    """Render and install every templated retriever (sync).

    *progress* is an optional callback invoked once per query with a
    short status message; lets the caller surface per-query progress
    in a UI (init dialog poll, etc.).
    """
    rendered = render_retrievers(
        domain_vts, domain_edges, include_entity, retriever_dir
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
) -> dict:
    """Render and install every templated retriever (async)."""
    rendered = render_retrievers(
        domain_vts, domain_edges, include_entity, retriever_dir
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
