"""Targeted regeneration actions for the Migration Assistant data-integrity
panel:

- ``regenerate_embeddings`` — re-embed vertices missing an embedding (GML-2175).
- ``regenerate_summaries``  — re-summarize communities whose description is a
  placeholder or empty, then re-embed the new summary (GML-2176).

Both reuse the rebuild pipeline's building blocks (``get_commuinty_children``,
``CommunitySummarizer``, the embedding store) — no new embedding/summarization
logic. Vertices whose source text is empty/placeholder are skipped and counted;
they need a rebuild/re-ingest, not a re-embed.
"""

import logging

from common.config import (
    get_llm_service,
    get_completion_config,
    get_embedding_service,
)
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.utils.summary_placeholders import PLACEHOLDER_MARKERS, is_placeholder_summary
from graphrag import util, community_summarizer

logger = logging.getLogger(__name__)

_VECTOR_ATTR = "embedding"


def _make_store(conn, graphname):
    store = TigerGraphEmbeddingStore(
        conn, get_embedding_service(), support_ai_instance=True
    )
    store.set_graphname(graphname)
    return store


async def _embeddable_types(conn) -> list[str]:
    """Vertex types carrying the embedding attribute (async connection)."""
    try:
        types = await conn.getVertexTypes()
    except Exception as e:
        logger.warning(f"regen: getVertexTypes failed: {e}")
        return []
    out = []
    for vt in types:
        try:
            attrs = await conn.getVertexAttrs(vt)
            names = [a[0] if isinstance(a, (list, tuple)) else a for a in attrs]
            if _VECTOR_ATTR in names:
                out.append(vt)
        except Exception:
            continue
    return out


def _row_id(r):
    """Vertex id from a printed vertex row, tolerant of the two shapes
    pyTigerGraph returns (``v_id`` vs an aliased ``id`` attribute)."""
    return r.get("v_id") or r.get("attributes", {}).get("id") or r.get("id")


async def _chunk_text(conn, chunk_id):
    try:
        res = await conn.runInstalledQuery(
            "StreamChunkContent", params={"chunk": (chunk_id,)}
        )
        rows = res[0].get("ChunkContent") if res else None
        if rows:
            return rows[0].get("attributes", {}).get("text", "") or ""
    except Exception as e:
        logger.warning(f"regen: chunk text fetch failed for {chunk_id}: {e}")
    return ""


async def _description(conn, vtype, vid):
    try:
        v = await conn.getVerticesById(vtype, vid)
        if v:
            desc = v[0].get("attributes", {}).get("description", "")
            # Entity descriptions can be a list; join for embedding.
            if isinstance(desc, list):
                desc = " ".join(str(x) for x in desc if x)
            return desc or ""
    except Exception as e:
        logger.warning(f"regen: description fetch failed for {vtype} {vid}: {e}")
    return ""


async def regenerate_embeddings(graphname, conn):
    """Re-embed vertices missing an embedding, per embeddable type. Returns
    ``{"regenerated": n, "skipped": m}``; skipped = empty/placeholder source
    (needs a rebuild/re-summarize) or an embed error."""
    util.loading_event.set()
    store = _make_store(conn, graphname)
    regenerated = 0
    skipped = 0
    for vt in await _embeddable_types(conn):
        try:
            res = await conn.runInstalledQuery(
                "vertices_have_embedding",
                params={"vertex_type": vt, "verbose": True},
            )
            results = next(
                (r["results"] for r in res if isinstance(r, dict) and "results" in r),
                [],
            )
            ids = [i for i in (_row_id(r) for r in results) if i]
        except Exception as e:
            logger.warning(f"regen_embeddings: list missing for {vt} failed: {e}")
            continue
        for vid in ids:
            text = (
                await _chunk_text(conn, vid)
                if vt == "DocumentChunk"
                else await _description(conn, vt, vid)
            )
            if not text or is_placeholder_summary(text):
                skipped += 1
                continue
            try:
                await store.aadd_embeddings([(text, [])], [{"vertex_id": (vid, vt)}])
                regenerated += 1
            except Exception as e:
                logger.warning(f"regen_embeddings: re-embed failed {vt} {vid}: {e}")
                skipped += 1
    logger.info(
        f"regenerate_embeddings({graphname}): "
        f"regenerated={regenerated} skipped={skipped}"
    )
    return {"regenerated": regenerated, "skipped": skipped}


async def regenerate_summaries(graphname, conn):
    """Re-summarize communities with placeholder/empty descriptions, then
    re-embed. Returns ``{"resummarized": n, "skipped": m}``; skipped = no usable
    child text or a summarization failure (needs a rebuild/re-ingest)."""
    util.loading_event.set()
    store = _make_store(conn, graphname)
    llm = get_llm_service(get_completion_config(graphname))
    summarizer = community_summarizer.CommunitySummarizer(llm)
    resummarized = 0
    skipped = 0
    try:
        res = await conn.runInstalledQuery(
            "communities_need_resummarize",
            params={"markers": PLACEHOLDER_MARKERS, "p": True},
        )
        targets = next(
            (r["results"] for r in res if isinstance(r, dict) and "results" in r), []
        )
    except Exception as e:
        logger.error(f"regen_summaries: target list failed: {e}")
        return {"resummarized": 0, "skipped": 0, "error": str(e)}

    for t in targets:
        cid = _row_id(t)
        try:
            i = int(t.get("attributes", {}).get("iteration", 0))
        except (TypeError, ValueError):
            i = 0
        if not cid:
            skipped += 1
            continue
        try:
            children = await util.get_commuinty_children(conn, i, cid)
        except Exception as e:
            logger.warning(f"regen_summaries: children fetch failed {cid}: {e}")
            skipped += 1
            continue
        if not children:
            skipped += 1
            continue
        if len(children) == 1:
            summary = children[0]
        else:
            r = await summarizer.summarize(cid, children)
            if r.get("error"):
                logger.warning(
                    f"regen_summaries: summarize failed {cid}: {r.get('message')}"
                )
                skipped += 1
                continue
            summary = r["summary"]
        if not summary or is_placeholder_summary(summary):
            skipped += 1
            continue
        pid = util.process_id(cid)
        try:
            await util.upsert_vertex(
                conn, "Community", pid, {"description": summary, "iteration": i}
            )
            await store.aadd_embeddings(
                [(summary, [])], [{"vertex_id": (pid, "Community")}]
            )
            resummarized += 1
        except Exception as e:
            logger.warning(f"regen_summaries: upsert/embed failed {cid}: {e}")
            skipped += 1
    logger.info(
        f"regenerate_summaries({graphname}): "
        f"resummarized={resummarized} skipped={skipped}"
    )
    return {"resummarized": resummarized, "skipped": skipped}
