"""Generic migration helpers for upgrading existing graphs to the
current release's GSQL queries and schema.

The release-cut workflow that motivates this module:

* On an existing graph (created against an older version), the customer
  upgrades graphrag. The new release may ship modified GSQL query
  bodies or expanded vertex/edge attributes. Without an automatic
  migration step the old, stale objects keep serving requests — leading
  to surprising behavior that's hard to attribute.

* ``check_and_reinstall_queries`` compares each shipped ``.gsql`` file
  against the body currently installed on TigerGraph and re-creates +
  re-installs only the ones whose body has actually drifted.

* ``check_schema_compatibility`` decides whether a graph can be repaired
  in place at all: it must already have the shipped base schema.

* ``check_and_apply_schema`` (still a stub — see TODO below) is the
  schema counterpart: detect missing attributes on existing vertex /
  edge types and emit ``ALTER VERTEX ... ADD ATTRIBUTE …`` statements.

Designed to be importable from both the graphrag FastAPI app (sync TG
connection via pyTigerGraph) and the ECC worker (async connection).
The sync entry points wrap the same comparison logic.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GSQL body normalization
# ---------------------------------------------------------------------------
#
# We compare the local ``.gsql`` text with TG's ``SHOW QUERY`` output.
# TG may canonicalize comments and whitespace differently from what was
# CREATE-d, so a literal byte-compare is noisy. Normalize both sides
# (strip comments, collapse whitespace) before hashing.
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_gsql(body: str) -> str:
    body = _BLOCK_COMMENT_RE.sub("", body)
    body = _LINE_COMMENT_RE.sub("", body)
    body = _WHITESPACE_RE.sub(" ", body).strip()
    return body


def _gsql_hash(body: str) -> str:
    return hashlib.sha256(_normalize_gsql(body).encode()).hexdigest()[:16]


# Pull the ``CREATE … QUERY <name>(…) { … }`` block out of TG's
# ``SHOW QUERY <name>`` output. The output also carries a status banner
# we don't want to fold into the hash.
_QUERY_BLOCK_RE = re.compile(
    r"(CREATE\s+(?:OR\s+REPLACE\s+)?(?:DISTRIBUTED\s+)?QUERY\s+\w+.*)",
    re.DOTALL,
)


def _extract_query_body(show_query_output: str) -> str:
    m = _QUERY_BLOCK_RE.search(show_query_output)
    return m.group(1) if m else ""


def get_installed_query_names(conn, graphname: str) -> set[str]:
    """Return the set of query names that are INSTALLED (have an active REST
    endpoint) on ``graphname`` — the authoritative install-state signal.

    A query can be *created* (its body exists in the catalog) yet not
    *installed*; only an installed query serves requests. Uses the pyTigerGraph
    query API (``getInstalledQueries`` → ``getEndpoints(dynamic=True)``); one
    call covers every query on the graph.
    """
    conn.graphname = graphname
    return set(conn.getInstalledQueries(fmt="list"))


def get_installed_query_body(conn, graphname: str, q_name: str) -> str | None:
    """Return the source of query ``q_name`` on ``graphname``, or ``None`` if the
    query does not exist (was never created).

    Uses the pyTigerGraph query API (``getQueryContent`` → ``GET /gsql/v1/
    queries/{name}``), which returns the clean source directly. GraphRAG requires
    TG >= 4.2, so this endpoint is always available. NOTE: this reflects the
    *created* body, not install state — pair it with ``get_installed_query_names``
    to decide whether a query needs installing.
    """
    conn.graphname = graphname
    try:
        res = conn.getQueryContent(q_name)
    except Exception as e:
        if "404" in str(e):
            return None  # query does not exist (never created)
        raise
    if isinstance(res, dict):
        if res.get("error"):
            return None
        return res.get("queryContent") or None
    return None


def _query_name_from_path(query_path: str) -> str:
    """``common/gsql/graphrag/StreamIds.gsql`` → ``StreamIds``."""
    base = os.path.basename(query_path)
    return base[:-5] if base.endswith(".gsql") else base


def _read_local_query(query_path: str) -> str | None:
    try:
        with open(query_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Local query file missing: {query_path}")
        return None


# ---------------------------------------------------------------------------
# Sync API (used by graphrag/app/supportai/supportai.py init_supportai)
# ---------------------------------------------------------------------------

def query_needs_update_sync(conn, graphname: str, query_path: str) -> bool:
    """Return True when the local ``.gsql`` body differs from what's
    installed on TG (or when the query is missing on TG).

    Wraps a synchronous ``conn.gsql()`` call. Errors fetching the
    installed body are treated as "needs update" so the caller falls
    back to re-installation rather than silently skipping.
    """
    local_body = _read_local_query(query_path)
    if local_body is None:
        return False  # nothing local to reinstall

    q_name = _query_name_from_path(query_path)
    local_hash = _gsql_hash(local_body)

    try:
        gc = conn.getQueryContent(q_name)
    except Exception as e:
        logger.warning(f"getQueryContent {q_name} failed ({e}); will reinstall.")
        return True

    # getQueryContent returns the clean installed body in ``queryContent`` —
    # no ``Using graph`` / ``# installed`` headers, so it normalizes to the same
    # body as the local .gsql (SHOW QUERY's header wrapping caused false drift).
    installed_body = gc.get("queryContent", "") if isinstance(gc, dict) and not gc.get("error") else ""
    if not installed_body:
        logger.info(f"Query '{q_name}' not installed yet; will install.")
        return True

    installed_hash = _gsql_hash(installed_body)
    drifted = local_hash != installed_hash
    if drifted:
        logger.info(
            f"Query '{q_name}' body has drifted from local ({installed_hash} != "
            f"{local_hash}); will reinstall."
        )
    return drifted


def filter_queries_needing_update_sync(
    conn,
    graphname: str,
    query_paths: Iterable[str],
) -> list[str]:
    """Return the subset of ``query_paths`` whose local body differs
    from TG's installed body. Use to skip unnecessary CREATE OR REPLACE
    + INSTALL QUERY ALL roundtrips on warm graphs.
    """
    return [p for p in query_paths if query_needs_update_sync(conn, graphname, p)]


# ---------------------------------------------------------------------------
# Async API (used by ecc/app/graphrag/util.py install_queries)
# ---------------------------------------------------------------------------

async def query_needs_update_async(conn, query_path: str) -> bool:
    """Async variant of :func:`query_needs_update_sync` for the ECC
    worker's ``AsyncTigerGraphConnection``. Reads ``conn.graphname``
    rather than taking it as a separate arg, matching how the rest of
    the ECC code threads the connection.
    """
    local_body = _read_local_query(query_path)
    if local_body is None:
        return False

    q_name = _query_name_from_path(query_path)
    local_hash = _gsql_hash(local_body)

    try:
        gc = await conn.getQueryContent(q_name)
    except Exception as e:
        logger.warning(f"getQueryContent {q_name} failed ({e}); will reinstall.")
        return True

    # getQueryContent returns the clean installed body in ``queryContent`` —
    # no header wrapping, so it normalizes to the same body as the local .gsql
    # (SHOW QUERY's headers caused false drift).
    installed_body = gc.get("queryContent", "") if isinstance(gc, dict) and not gc.get("error") else ""
    if not installed_body:
        logger.info(f"Query '{q_name}' not installed yet; will install.")
        return True

    installed_hash = _gsql_hash(installed_body)
    drifted = local_hash != installed_hash
    if drifted:
        logger.info(
            f"Query '{q_name}' body has drifted from local ({installed_hash} != "
            f"{local_hash}); will reinstall."
        )
    return drifted


async def filter_queries_needing_update_async(
    conn,
    query_paths: Iterable[str],
) -> list[str]:
    out: list[str] = []
    for p in query_paths:
        if await query_needs_update_async(conn, p):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------
#
# Query repair reinstalls the shipped queries in place, which only works when
# the graph already has the base schema those queries are written against.
# That base schema has been unchanged since 1.4.0; graphs created earlier
# differ (1.3.x: IS_HEAD_OF / HAS_TAIL attach to Entity rather than
# EntityType; 1.1-1.2: different types altogether) and are not repairable in
# place. Rather than infer a version, compare the live schema with the shipped
# base schema file: every base vertex type, edge type, edge endpoint pair and
# attribute must exist. Extra types (domain types, images) are allowed. Vector
# attributes live in separate files and depend on the deployment's vector
# setup, so they are not part of the check.

BASE_SCHEMA_PATH = "common/gsql/supportai/SupportAI_Schema.gsql"

_ADD_VERTEX_RE = re.compile(
    r"ADD\s+VERTEX\s+(\w+)\s*\((.*?)\)\s*WITH", re.IGNORECASE | re.DOTALL
)
_ADD_EDGE_RE = re.compile(
    r"ADD\s+(?:UN)?DIRECTED\s+EDGE\s+(\w+)\s*\((.*?)\)\s*(?:WITH|;)",
    re.IGNORECASE | re.DOTALL,
)
_EDGE_PAIR_RE = re.compile(r"FROM\s+(\w+)\s*,\s*TO\s+(\w+)", re.IGNORECASE)


@dataclass
class SchemaCompatibility:
    compatible: bool
    differences: list = field(default_factory=list)


def _attr_names(segment: str) -> set:
    """Attribute names from a comma-separated ``name TYPE`` list."""
    names = set()
    for part in segment.split(","):
        words = part.split()
        if words and words[0].upper() != "PRIMARY_ID":
            names.add(words[0])
    return names


def parse_base_schema(text: str) -> tuple[dict, dict]:
    """Expected schema from the shipped base schema GSQL.

    Returns ``(vertices, edges)``: ``{vertex: {attr, ...}}`` and
    ``{edge: {"pairs": {(from, to), ...}, "attrs": {attr, ...}}}``.
    """
    vertices = {m.group(1): _attr_names(m.group(2)) for m in _ADD_VERTEX_RE.finditer(text)}
    edges = {}
    for m in _ADD_EDGE_RE.finditer(text):
        body = m.group(2)
        pairs = {(f, t) for f, t in _EDGE_PAIR_RE.findall(body)}
        edges[m.group(1)] = {
            "pairs": pairs,
            "attrs": _attr_names(_EDGE_PAIR_RE.sub("", body).replace("|", ",")),
        }
    return vertices, edges


def _live_attr_names(meta: dict) -> set:
    return {a.get("AttributeName") for a in (meta or {}).get("Attributes", []) or []}


def _edge_connects(meta: dict, from_vt: str, to_vt: str) -> bool:
    """Whether the live edge accepts ``from_vt -> to_vt``.

    Multi-pair edges list their pairs under ``EdgePairs``. An edge whose
    endpoints are reported as ``*`` with no pair list accepts any type at
    that end; TigerGraph reports ``IN_COMMUNITY`` this way once domain types
    have been added to it.
    """
    pairs = [(ep.get("From"), ep.get("To")) for ep in meta.get("EdgePairs", []) or []]
    if pairs:
        return (from_vt, to_vt) in pairs
    f, t = meta.get("FromVertexTypeName"), meta.get("ToVertexTypeName")
    return f in ("*", from_vt) and t in ("*", to_vt)


def check_schema_compatibility(conn, base_schema_path: str = BASE_SCHEMA_PATH) -> SchemaCompatibility:
    """Whether the graph has every element of the shipped base schema.

    Raises when the live schema cannot be read, so callers can tell "not
    compatible" from "could not check".
    """
    with open(base_schema_path, encoding="utf-8") as f:
        exp_vertices, exp_edges = parse_base_schema(f.read())

    live_vertices = set(conn.getVertexTypes() or [])
    live_edges = set(conn.getEdgeTypes() or [])
    differences = []

    for vt, attrs in exp_vertices.items():
        if vt not in live_vertices:
            differences.append(f"missing vertex type {vt}")
            continue
        missing = attrs - _live_attr_names(conn.getVertexType(vt))
        if missing:
            differences.append(f"vertex type {vt} is missing attribute(s) {', '.join(sorted(missing))}")

    for et, spec in exp_edges.items():
        if et not in live_edges:
            differences.append(f"missing edge type {et}")
            continue
        meta = conn.getEdgeType(et) or {}
        missing_pairs = {(f, t) for f, t in spec["pairs"] if not _edge_connects(meta, f, t)}
        if missing_pairs:
            pairs = ", ".join(f"{a}->{b}" for a, b in sorted(missing_pairs))
            differences.append(f"edge type {et} does not connect {pairs}")
        missing = spec["attrs"] - _live_attr_names(meta)
        if missing:
            differences.append(f"edge type {et} is missing attribute(s) {', '.join(sorted(missing))}")

    return SchemaCompatibility(compatible=not differences, differences=differences)


# ---------------------------------------------------------------------------
# Schema migration — TODO
# ---------------------------------------------------------------------------
#
# Goal: detect attributes that exist in the shipped schema ``.gsql``
# files but are missing on the live graph, and emit
# ``ALTER VERTEX <T> ADD ATTRIBUTE <name> <type> [DEFAULT …]``
# statements wrapped in a ``CREATE SCHEMA_CHANGE JOB`` so the operator
# never has to run them by hand.
#
# Outline (deferred implementation):
#   1. Parse each shipped ``SupportAI_Schema*.gsql`` (and any other
#      schema-relevant .gsql) to build the expected
#      ``{vertex_type: {attr: tg_type}}`` map.
#   2. Query live schema via ``conn.getSchema()`` (or parse ``ls``
#      output) to build the same map for the running graph.
#   3. For each declared type, compute ``expected - current``.
#   4. Emit one ``CREATE SCHEMA_CHANGE JOB`` that ADDs the missing
#      attributes with their declared defaults.
#   5. ``RUN SCHEMA_CHANGE JOB`` and drop it.
#
# v1.4.2 doesn't add any new attributes on existing vertex types
# (the ``Document.name`` / ``Image.name`` from v2.0 A3 were
# intentionally skipped), so this is a no-op for the current release.
# Stub kept here so future migrations have a place to land.

def check_and_apply_schema(conn, graphname: str) -> dict:
    """Compare expected vertex/edge attributes against the live schema
    and apply any missing additions. Returns a summary dict.

    Stubbed for v1.4.2 — no attribute additions ship in this release.
    """
    return {"applied": [], "skipped_reason": "no schema deltas in this release"}
