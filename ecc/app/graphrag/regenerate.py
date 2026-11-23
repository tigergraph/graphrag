"""Targeted regeneration actions for the Migration Assistant data-integrity
panel:

- ``regenerate_embeddings`` — re-embed vertices missing an embedding (GML-2175).
- ``regenerate_summaries``  — re-summarize communities whose description is a
  placeholder or empty, then re-embed the new summary (GML-2176).
- ``move_heading_paths``    — move each chunk's breadcrumb from its text to
  ``heading_path`` (GML-2369).
- ``regenerate_chunk_summaries`` — ask the model for the summary of chunks that
  have none, save it, and re-embed the chunk (GML-2369).

Both reuse the rebuild pipeline's building blocks (``get_commuinty_children``,
``CommunitySummarizer``, the embedding store) — no new embedding/summarization
logic. Vertices whose source text is empty/placeholder are skipped and counted;
they need a rebuild/re-ingest, not a re-embed.
"""

import json
import logging

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate

from common.chunkers.auto import auto_detect_kind
from common.config import (
    get_llm_service,
    get_completion_config,
    get_embedding_service,
)
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.py_schemas.tool_io_schemas import ChunkSummary
from common.utils.chunk_context import (
    HEADING_PATH_ATTR,
    SUMMARY_ATTR,
    chunk_parts,
    embedding_text,
    format_chunk_summary,
    schema_has_chunk_context,
)
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
    # Native vector attributes are absent from getVertexAttrs; they live under
    # each vertex type's ``EmbeddingAttributes`` in the schema.
    try:
        schema = await conn.getSchema()
    except Exception as e:
        logger.warning(f"regen: getSchema failed: {e}")
        return []
    out = []
    for v in schema.get("VertexTypes", []):
        if any(e.get("Name") == _VECTOR_ATTR for e in (v.get("EmbeddingAttributes") or [])):
            out.append(v.get("Name"))
    return out


def _row_id(r):
    """Vertex id from a printed vertex row, tolerant of the two shapes
    pyTigerGraph returns (``v_id`` vs an aliased ``id`` attribute)."""
    return r.get("v_id") or r.get("attributes", {}).get("id") or r.get("id")


async def _chunk_embedding_text(conn, chunk_id):
    """Embedding input of a chunk: its heading path and summary (when the graph
    stores them) and its text."""
    text = await _chunk_text(conn, chunk_id)
    if not text or conn.graphname not in util.chunk_context_graphs:
        return text
    attrs = await _chunk_attrs(conn, chunk_id)
    return embedding_text(attrs.get(HEADING_PATH_ATTR) or "", attrs.get(SUMMARY_ATTR) or "", text)


async def _chunk_attrs(conn, chunk_id) -> dict:
    try:
        rows = await conn.getVerticesById("DocumentChunk", chunk_id)
        return rows[0].get("attributes", {}) if rows else {}
    except Exception as e:
        logger.warning(f"regen: chunk attributes fetch failed for {chunk_id}: {e}")
        return {}


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
    await util.refresh_chunk_context(conn)
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
                await _chunk_embedding_text(conn, vid)
                if vt == "DocumentChunk"
                else await _description(conn, vt, vid)
            )
            if not text or is_placeholder_summary(text):
                skipped += 1
                continue
            try:
                # Only a returned result means the embedding was saved.
                written = await store.aadd_embeddings([(text, [])], [{"vertex_id": (vid, vt)}])
            except Exception as e:
                logger.warning(f"regen_embeddings: re-embed failed {vt} {vid}: {e}")
                written = None
            if written:
                regenerated += 1
            else:
                logger.warning(f"regen_embeddings: no embedding saved for {vt} {vid}")
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
            # Direct upsert (not util.upsert_vertex, which only enqueues to the
            # rebuild loader queue that isn't running in a standalone regenerate
            # action — the description write would be silently dropped).
            await conn.upsertVertex(
                "Community", pid,
                attributes={"description": summary, "iteration": i},
            )
            written = await store.aadd_embeddings(
                [(summary, [])], [{"vertex_id": (pid, "Community")}]
            )
        except Exception as e:
            logger.warning(f"regen_summaries: upsert/embed failed {cid}: {e}")
            skipped += 1
            continue
        if written:
            resummarized += 1
        else:
            # A summary saved without its embedding is not counted as done.
            logger.warning(f"regen_summaries: summary saved but no embedding for {cid}")
            skipped += 1
    logger.info(
        f"regenerate_summaries({graphname}): "
        f"resummarized={resummarized} skipped={skipped}"
    )
    return {"resummarized": resummarized, "skipped": skipped}


# --------------------------------------------------------------------------
# Chunk heading paths and summaries (GML-2369)
# --------------------------------------------------------------------------

# Chunker kinds that write a breadcrumb at the start of each chunk.
_PATH_CHUNKER_KINDS = {"structured", "markdown", "html"}


def _decode(text: str) -> str:
    # Same decoding as chunk_doc applies before chunking.
    return text.encode("raw_unicode_escape").decode("unicode_escape")


def _path_chunker(ctype: str, text: str, graphname: str):
    """The chunker that ingest used for this document, if it is one that
    writes breadcrumbs; ``None`` otherwise. Choosing it costs no model call."""
    import ecc_util
    from common.config import get_graphrag_config

    kind = (ctype or "").lower().strip() or get_graphrag_config(graphname).get("chunker", "auto")
    if kind == "auto":
        kind = auto_detect_kind(text)
    if kind not in _PATH_CHUNKER_KINDS:
        return None
    return ecc_util.get_chunker(kind, graphname=graphname)


async def _stored_chunk_texts(conn, chunk_ids) -> dict:
    """Stored text of each chunk, in one query when every chunk exists."""
    try:
        res = await conn.runInstalledQuery(
            "Selected_Set_Display",
            params={"json_list_vts": json.dumps([{"v": c, "t": "DocumentChunk"} for c in chunk_ids])},
            usePost=True,
        )
        return {
            r["v_id"]: "\n".join(r.get("attributes", {}).get("context") or [])
            for r in res[0].get("results", [])
        }
    except Exception:
        out = {}
        for c in chunk_ids:
            text = await _chunk_text(conn, c)
            if text:
                out[c] = text
        return out


async def move_heading_paths(graphname, conn):
    """Move each chunk's breadcrumb from the start of its text to
    ``heading_path``. The document is chunked again with the chunker that
    ingest used; a chunk changes only when its stored text equals the new
    chunk exactly. Returns ``{"moved": n, "unchanged": m, "not_matched": k}``:
    unchanged = no breadcrumb (or another chunker), not_matched = the stored
    text differs, for example after a change of chunker settings."""
    util.loading_event.set()
    if not await util.refresh_chunk_context(conn):
        return {"moved": 0, "unchanged": 0, "not_matched": 0,
                "error": "The graph schema has no chunk heading path attribute."}
    moved = unchanged = not_matched = 0
    docs = await conn.getVertices("Document", select="epoch_added")
    for d in docs:
        doc_id = d["v_id"].lower()
        rows = await conn.getVerticesById("Content", doc_id)
        if not rows:
            continue
        attrs = rows[0].get("attributes", {})
        text = _decode(attrs.get("text") or "")
        chunker = _path_chunker(attrs.get("ctype") or "", text, graphname) if text else None
        if chunker is None:
            continue
        try:
            chunks = chunker.chunk(text)
        except Exception as e:
            logger.warning(f"move_heading_paths: chunking {doc_id} failed: {e}")
            continue
        ids = [util.process_id(f"{doc_id}_chunk_{i}") for i in range(len(chunks))]
        stored = await _stored_chunk_texts(conn, ids)
        contents, paths = [], []
        for chunk_id, chunk in zip(ids, chunks):
            path, body = chunk_parts(chunk)
            current = stored.get(chunk_id)
            if current is None:
                continue
            if not path:
                unchanged += 1
            elif current == str(chunk):
                contents.append((chunk_id, {"text": body}))
                paths.append((chunk_id, {HEADING_PATH_ATTR: path}))
            else:
                not_matched += 1
        if contents:
            try:
                await conn.upsertVertices("DocumentChunk", paths)
                await conn.upsertVertices("Content", contents)
                moved += len(contents)
            except Exception as e:
                logger.warning(f"move_heading_paths: upsert failed for {doc_id}: {e}")
                not_matched += len(contents)
    logger.info(
        f"move_heading_paths({graphname}): moved={moved} "
        f"unchanged={unchanged} not_matched={not_matched}"
    )
    return {"moved": moved, "unchanged": unchanged, "not_matched": not_matched}


_CHUNK_SUMMARY_PROMPT = """Summarize one document chunk as metadata for search.

- ``topic`` — one short noun phrase (≤12 chars) naming what the chunk is
  primarily about, in the source language.
- ``section`` — the heading or section title the chunk falls under, copied
  verbatim from the section path or the text when present; empty otherwise.
- ``entities`` — proper nouns, categories and years explicitly named in the
  chunk. When the chunk contains a table, also include every column header
  and row label. Skip generic terms.

Only include items explicitly present in the text; never infer or guess.

## Section path
{heading_path}

## Chunk
{chunk}

## Output
{format_instructions}"""


async def regenerate_chunk_summaries(graphname, conn):
    """Ask the model for the summary of each chunk that has none, save it,
    and re-embed the chunk from its heading path, summary and text. Returns
    ``{"summarized": n, "skipped": m}``; skipped = no text, a model error or
    an embedding that was not saved."""
    util.loading_event.set()
    try:
        schema = await conn.getSchema(force=True)
    except Exception as e:
        # For example the catalog lock another schema change holds.
        return {"summarized": 0, "skipped": 0,
                "error": f"Could not read the graph schema ({e}). Try again when no "
                         "other graph operation is running."}
    if not schema_has_chunk_context(schema):
        return {"summarized": 0, "skipped": 0,
                "error": "The graph schema has no chunk summary attribute."}
    await util.refresh_chunk_context(conn)
    llm = get_llm_service(get_completion_config(graphname))
    parser = PydanticOutputParser(pydantic_object=ChunkSummary)
    prompt = PromptTemplate(
        template=_CHUNK_SUMMARY_PROMPT,
        input_variables=["heading_path", "chunk"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    store = _make_store(conn, graphname)
    rows = await conn.getVertices("DocumentChunk", select=f"{HEADING_PATH_ATTR},{SUMMARY_ATTR}")
    summarized = skipped = 0
    for r in rows:
        attrs = r.get("attributes", {})
        if attrs.get(SUMMARY_ATTR):
            continue
        chunk_id = r["v_id"]
        text = await _chunk_text(conn, chunk_id)
        if not text:
            skipped += 1
            continue
        path = attrs.get(HEADING_PATH_ATTR) or ""
        try:
            out = await llm.ainvoke_with_parser(
                prompt, parser, {"heading_path": path or "(none)", "chunk": text},
                caller_name="regenerate_chunk_summaries",
            )
            summary = format_chunk_summary(out.model_dump() if hasattr(out, "model_dump") else out)
        except Exception as e:
            logger.warning(f"regen_chunk_summaries: summary failed for {chunk_id}: {e}")
            skipped += 1
            continue
        if not summary:
            skipped += 1
            continue
        try:
            await conn.upsertVertex("DocumentChunk", chunk_id, attributes={SUMMARY_ATTR: summary})
            written = await store.aadd_embeddings(
                [(embedding_text(path, summary, text), [])],
                [{"vertex_id": (chunk_id, "DocumentChunk")}],
            )
        except Exception as e:
            logger.warning(f"regen_chunk_summaries: save failed for {chunk_id}: {e}")
            written = None
        if written:
            summarized += 1
        else:
            skipped += 1
    logger.info(
        f"regenerate_chunk_summaries({graphname}): "
        f"summarized={summarized} skipped={skipped}"
    )
    return {"summarized": summarized, "skipped": skipped}
