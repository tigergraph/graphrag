"""Data-integrity health checks for the Migration Assistant: embedding coverage
and community-summary completeness.

Deterministic, read-only — NO LLM calls and NO embedding-service init. They only
run count queries + a schema check over an existing connection, so they are safe
on a polled/triggered status check. The connection is the synchronous one the
status endpoint already holds.
"""

import logging

from common.utils.summary_placeholders import PLACEHOLDER_MARKERS

logger = logging.getLogger(__name__)

_VECTOR_ATTR = "embedding"


def _has_vector_attr(conn, v_type: str) -> bool:
    """True if *v_type* carries the embedding vector attribute, per the live
    schema. Uses the connection's schema API — no embedding-service init."""
    # Native vector attributes are exposed under ``EmbeddingAttributes`` in the
    # schema, not by getVertexAttrs (which omits them).
    try:
        for v in conn.getSchema().get("VertexTypes", []):
            if v.get("Name") == v_type:
                return any(
                    e.get("Name") == _VECTOR_ATTR
                    for e in (v.get("EmbeddingAttributes") or [])
                )
        return False
    except Exception:
        return False


def embeddable_types(conn) -> list[str]:
    """Vertex types that carry the embedding vector attribute. Schema-detected
    (not hardcoded) so it stays correct as the embedded set changes."""
    # Native vector attributes are NOT returned by getVertexAttrs/getVertexTypes;
    # they live under each vertex type's ``EmbeddingAttributes`` in the schema.
    try:
        schema = conn.getSchema()
    except Exception as e:
        logger.warning(f"embeddable_types: getSchema failed: {e}")
        return []
    out = []
    for v in schema.get("VertexTypes", []):
        if any(e.get("Name") == _VECTOR_ATTR for e in (v.get("EmbeddingAttributes") or [])):
            out.append(v.get("Name"))
    return out


def embedding_coverage(conn, v_type: str) -> dict | None:
    """``{"total": M, "missing": N}`` for *v_type*, or ``None`` when the type is
    not embeddable or the ``vertices_have_embedding`` query is unavailable."""
    try:
        if not _has_vector_attr(conn, v_type):
            return None
        res = conn.runInstalledQuery(
            "vertices_have_embedding", params={"vertex_type": v_type}
        )
        # PRINT order: [0] all_have_embedding, [1] size (missing), [2] total.
        missing = int(res[1]["size"])
        total = int(res[2]["total"])
        return {"total": total, "missing": missing}
    except Exception as e:
        logger.warning(f"embedding_coverage({v_type}) failed: {e}")
        return None


def community_summary_health(conn, markers=None) -> dict | None:
    """``{"total": M, "needs_resummarize": N}`` for Community vertices, or
    ``None`` when the ``communities_need_resummarize`` query is unavailable.
    ``needs_resummarize`` counts communities whose description is empty or a
    known placeholder (old or new)."""
    markers = markers if markers is not None else PLACEHOLDER_MARKERS
    try:
        res = conn.runInstalledQuery(
            "communities_need_resummarize", params={"markers": markers}
        )
        row = res[0]
        return {
            "total": int(row["total"]),
            "needs_resummarize": int(row["needs_resummarize"]),
        }
    except Exception as e:
        logger.warning(f"community_summary_health failed: {e}")
        return None
