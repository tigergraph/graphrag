"""Data-integrity health checks for the Migration Assistant: embedding coverage
and community-summary completeness.

Deterministic, read-only — NO LLM calls. Safe to run on a polled/triggered
status check. Shared by the graphrag app (status endpoint) and available to ECC.
"""

import logging

from common.utils.summary_placeholders import PLACEHOLDER_MARKERS

logger = logging.getLogger(__name__)


def embeddable_types(store) -> list[str]:
    """Vertex types that carry the embedding vector attribute, per the live
    schema. Schema-detected (not hardcoded) so it stays correct as the embedded
    set changes. Empty on any error."""
    try:
        types = store.conn.getVertexTypes()
    except Exception as e:
        logger.warning(f"embeddable_types: getVertexTypes failed: {e}")
        return []
    out = []
    for vt in types:
        try:
            if store.has_vector_attribute(vt, store.default_vector_attribute):
                out.append(vt)
        except Exception:
            continue
    return out


def embedding_coverage(store, v_type: str) -> dict | None:
    """``{"total": M, "missing": N}`` for *v_type*, or ``None`` when the type is
    not embeddable or the ``vertices_have_embedding`` query is unavailable."""
    try:
        if not store.has_vector_attribute(v_type, store.default_vector_attribute):
            return None
        res = store.conn.runInstalledQuery(
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
