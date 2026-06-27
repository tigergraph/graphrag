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
