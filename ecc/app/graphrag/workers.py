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

import asyncio
import base64
import logging
import time
import json
from urllib.parse import quote_plus
from typing import Iterable, List, Optional, Tuple

import ecc_util
import httpx
from aiochannel import Channel
from graphrag import community_summarizer, util
from langchain_community.graphs.graph_document import GraphDocument, Node
from pyTigerGraph import AsyncTigerGraphConnection

from common.db.schema_utils import gsql_output_error
from common.embeddings.embedding_services import EmbeddingModel
from common.embeddings.base_embedding_store import EmbeddingStore
from common.extractors import BaseExtractor, LLMEntityRelationshipExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)

# Community summarization is a small single-shot call; cap it well below the
# provider's ~600s default so an unreachable provider fails fast instead of
# hanging the whole community layer.
_SUMMARY_TIMEOUT_S = 120
# After this many consecutive connectivity failures, stop calling the provider
# for the rest of this run — the remaining communities fall back immediately
# rather than each burning a full timeout.
_SUMMARY_CONN_FAILURE_THRESHOLD = 3


class CommunitySummaryBreaker:
    """Per-rebuild circuit breaker for community summarization. Once the LLM
    provider looks unreachable, `open` flips true so remaining communities skip
    the call and take the placeholder immediately."""

    def __init__(self, threshold: int = _SUMMARY_CONN_FAILURE_THRESHOLD):
        self.threshold = threshold
        self._consecutive = 0
        self.open = False
        self.incomplete = 0  # communities left with a placeholder summary

    def record_success(self):
        self._consecutive = 0

    def record_conn_failure(self):
        self._consecutive += 1
        if self._consecutive >= self.threshold and not self.open:
            self.open = True
            logger.error(
                "Community summarization circuit breaker OPEN: LLM provider "
                "appears unreachable; remaining communities will be left with "
                "placeholder summaries for later regeneration."
            )


async def _summarize_once(summarizer, comm_id, children) -> dict:
    """Run one summarization attempt with an explicit timeout so a hung
    provider fails in _SUMMARY_TIMEOUT_S instead of the ~600s library default."""
    try:
        return await asyncio.wait_for(
            summarizer.summarize(comm_id, children),
            timeout=_SUMMARY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {
            "error": True,
            "summary": "",
            "message": f"summarization timed out after {_SUMMARY_TIMEOUT_S}s",
            "category": "connectivity",
        }


async def install_query(
    conn: AsyncTigerGraphConnection, query_path: str, install: bool = True
) -> dict[str, httpx.Response | str | None]:
    LogWriter.info(f"Installing query {query_path}")
    with open(f"{query_path}.gsql", "r") as f:
        query_text = f.read()
    query_name = query_path.split("/")[-1]

    # CREATE/REPLACE the query body. Prefer the REST endpoint
    # (POST /gsql/v1/queries via createQuery); fall back to a GSQL CREATE
    # statement only if the REST call errors.
    async with util.tg_sem:
        try:
            await conn.createQuery(query_text)
        except Exception as rest_err:
            LogWriter.info(f"createQuery REST failed for {query_name}; gsql fallback: {rest_err}")
            res = await conn.gsql(f"USE GRAPH {conn.graphname}\n{query_text}\n")
            if gsql_output_error(res):
                LogWriter.error(res)
                return {"result": None, "error": True,
                        "message": f"Failed to create query {query_name}"}

    if install:
        async with util.tg_sem:
            try:
                await conn.installQueries([query_name], flag="-force", wait=True)
            except Exception as inst_err:
                LogWriter.info(f"installQueries REST failed for {query_name}; gsql fallback: {inst_err}")
                res = await conn.gsql(f"USE GRAPH {conn.graphname}\nINSTALL QUERY {query_name}\n")
                if gsql_output_error(res):
                    LogWriter.error(res)
                    return {"result": None, "error": True,
                            "message": f"Failed to install query {query_name}"}

    return {"result": "ok", "error": False}


chunk_sem = asyncio.Semaphore(util._worker_concurrency)


async def chunk_doc(
    conn: AsyncTigerGraphConnection,
    doc: dict[str, str],
    upsert_chan: Channel,
    embed_chan: Channel,
    extract_chan: Channel,
    tracker=None,
):
    """
    Chunks a document.
    Places the resulting chunks into the upsert channel (to be upserted to TG)
    and the embed channel (to be embedded and written to the vector store)
    """

    # if loader is running, wait until it's done
    if not util.loading_event.is_set():
        logger.info("Chunk worker waiting for loading event to finish")
        await util.loading_event.wait()

    async with chunk_sem:
        if "ctype" in doc["attributes"]:
            chunker_type = doc["attributes"]["ctype"].lower().strip()
        else:
            chunker_type = ""

        v_id = doc["v_id"].lower()

        # Use get_chunker for all types (including images)
        # For images, get_chunker returns SingleChunker which preserves markdown image references
        chunker = ecc_util.get_chunker(chunker_type, graphname=conn.graphname)
        # decode the text return from tigergraph as it was encoded when written into jsonl file for uploading
        chunks = chunker.chunk(doc["attributes"]["text"].encode('raw_unicode_escape').decode('unicode_escape'))

        # v_id / chunk_id derive from user document content.
        logger.debug(f"Chunking {v_id} into {len(chunks)} chunk(s)")
        chunk_ids = [util.process_id(f"{v_id}_chunk_{i}") for i in range(len(chunks))]
        # Register the document's chunks before dispatching any, so the extract
        # worker can't complete a chunk before the tracker knows to expect it.
        if tracker is not None:
            tracker.register(v_id, chunk_ids)
        for i, chunk in enumerate(chunks):
            chunk_id = chunk_ids[i]
            logger.debug(f"Processing chunk {chunk_id}")

            # send chunks to be upserted (func, args)
            logger.debug("chunk writes to upsert_chan")
            await upsert_chan.put((upsert_chunk, (conn, v_id, chunk_id, chunk, i)))

            # send chunks to have entities extracted
            logger.debug("chunk writes to extract_chan")
            await extract_chan.put((chunk, chunk_id))

            # When extraction is enabled the extract worker pushes the
            # summary-augmented embed message itself (Contextual Retrieval),
            # so only embed the raw chunk here when extraction is off.
            from common.config import entity_extraction_switch
            if not entity_extraction_switch:
                logger.debug("chunk writes to embed_chan (no extraction)")
                await embed_chan.put((chunk_id, chunk, "DocumentChunk"))

    return v_id


async def upsert_doc(conn: AsyncTigerGraphConnection, doc_id, ctype, content_text):
    date_added = int(time.time())
    await util.upsert_vertex(
        conn,
        "Document",
        doc_id,
        attributes={"epoch_added": date_added, "epoch_processed": date_added},
    )
    await util.upsert_vertex(
        conn,
        "Content",
        doc_id,
        attributes={"ctype": ctype, "text": content_text, "epoch_added": date_added},
    )
    await util.upsert_edge(
        conn, "Document", doc_id, "HAS_CONTENT", "Content", doc_id
    )

async def upsert_chunk(conn: AsyncTigerGraphConnection, doc_id, chunk_id, chunk, idx):
    logger.debug(f"Upserting chunk {chunk_id}")
    date_added = int(time.time())
    # Build the chunk's full vertex + edge bundle and enqueue atomically.
    # Three separate ``await util.upsert_vertex/edge`` calls would let
    # asyncio cancellation split DocumentChunk from its sibling Content,
    # producing the "chunk exists but Content missing" pattern that
    # surfaces as repeated "No content row for chunk" warnings.
    vertices = [
        ("DocumentChunk", chunk_id, {
            "epoch_added": date_added,
            "epoch_processed": date_added,
            "idx": idx,
        }),
        ("Content", chunk_id, {"text": chunk, "epoch_added": date_added}),
    ]
    edges = [
        ("DocumentChunk", chunk_id, "HAS_CONTENT", "Content", chunk_id, None),
        ("Document", doc_id, "HAS_CHILD", "DocumentChunk", chunk_id, None),
    ]
    if idx > 0:
        edges.append((
            "DocumentChunk", chunk_id, "IS_AFTER",
            "DocumentChunk", util.process_id(f"{doc_id}_chunk_{idx - 1}"), None,
        ))
    await util.upsert_group(conn, vertices, edges)


embed_sem = asyncio.Semaphore(util._worker_concurrency)


async def embed(
    embed_svc: EmbeddingModel,
    embed_store: EmbeddingStore,
    v_id: str | Tuple[str, str],
    content: str,
):
    """
    Args:
        graphname: str
            the name of the graph the documents are in
        embed_svc: EmbeddingModel
            The class used to vectorize text
        embed_store:
            The class used to store the vectore to a vector DB
        v_id: str
            the vertex id that will be embedded
        content: str
            the content of the document/chunk
        index_name: str
            the vertex index to write to
    """
    async with embed_sem:
        logger.debug(f"Embedding {v_id}")

        # if loader is running, wait until it's done
        if not util.loading_event.is_set():
            logger.debug("Embed worker waiting for loading event to finish")
            await util.loading_event.wait()
        try:
            await embed_store.aadd_embeddings([(content, [])], [{"vertex_id": v_id}])
        except Exception as e:
            logger.error(f"Failed to add embeddings for {v_id}: {e}")


def _is_near_duplicate(new_desc, existing_descs, threshold=0.85):
    from difflib import SequenceMatcher
    new_lower = new_desc.lower()
    new_len = len(new_lower)
    sm = SequenceMatcher(None, new_lower)
    for existing in existing_descs:
        ex_lower = existing.lower()
        ex_len = len(ex_lower)
        if not (new_len + ex_len) or 2 * min(new_len, ex_len) / (new_len + ex_len) < threshold:
            continue
        sm.set_seq2(ex_lower)
        if sm.quick_ratio() >= threshold and sm.ratio() >= threshold:
            return True
    return False


async def get_vert_desc(conn, v_id, node: Node):
    new_desc = node.properties.get("description", "")
    exists = await util.check_vertex_exists(conn, v_id)
    if not exists.get("error", False):
        resp = exists.get("resp")
        if resp and len(resp) > 0 and "attributes" in resp[0]:
            existing_descs = resp[0]["attributes"].get("description", [])
            if not new_desc or _is_near_duplicate(new_desc, existing_descs):
                return existing_descs if existing_descs else [new_desc]
            return existing_descs + [new_desc]
    return [new_desc]


extract_sem = asyncio.Semaphore(util._worker_concurrency)


async def extract(
    upsert_chan: Channel,
    embed_chan: Channel,
    extractor: BaseExtractor,
    conn: AsyncTigerGraphConnection,
    chunk: str,
    chunk_id: str,
):
    # if loader is running, wait until it's done
    if not util.loading_event.is_set():
        logger.info("Extract worker waiting for loading event to finish")
        await util.loading_event.wait()

    async with extract_sem:
        try:
            extracted: list[GraphDocument] = await extractor.aextract(chunk)
            # chunk_id is user-content-derived; demote.
            logger.debug(
                f"Extracting chunk: {chunk_id} ({len(extracted)} graph docs extracted)"
            )
        except Exception as e:
            logger.error(f"Failed to extract chunk {chunk_id}: {e}")
            extracted = []

        # Contextual Retrieval: the extractor's LLM call also produces a
        # compact ``chunk_summary`` (carried on ``source.metadata`` of the
        # first GraphDocument). Embed ``summary + raw chunk`` so dense
        # vectors carry the chunk's topic / entities explicitly — improves
        # retrieval on table-heavy and numeric content where raw text embeds
        # poorly. When extraction is enabled the chunk/residual workers skip
        # their own embed push, so this is the sole embed for the chunk;
        # an empty summary falls back to embedding the raw chunk.
        chunk_summary = ""
        if extracted:
            md = getattr(extracted[0].source, "metadata", None) or {}
            chunk_summary = (md.get("chunk_summary") or "").strip()
        embed_input = (chunk_summary + "\n\n" + str(chunk)) if chunk_summary else str(chunk)
        await embed_chan.put((chunk_id, embed_input, "DocumentChunk"))

        # Schema-aware ingest helpers — derive case-insensitive
        # lookups from the extractor once per chunk so the loops below
        # can map LLM-emitted type strings back to canonical schema names.
        domain_vt_canonical: dict = {}
        domain_edge_canonical: dict = {}
        edge_endpoint_pairs: dict = {}
        strict_mode = False
        if isinstance(extractor, LLMEntityRelationshipExtractor):
            domain_vt_canonical = {
                v.casefold(): v for v in (extractor.allowed_vertex_types or [])
            }
            domain_edge_canonical = {
                e.casefold(): e for e in (extractor.allowed_edge_types or [])
            }
            edge_endpoint_pairs = {
                name.casefold(): {(f.casefold(), t.casefold()) for f, t in pairs}
                for name, pairs in (extractor.domain_edge_endpoints or {}).items()
            }
            strict_mode = bool(extractor.strict_mode)

        # ``has_domain_types`` distinguishes the two meta-layer cases:
        #   Case 1: no domain types on the graph — the EntityType /
        #     RelationshipType layer becomes a free-text catalog of
        #     whatever the LLM emitted.
        #   Case 2: at least one domain type exists — the meta-layer
        #     is restricted to declared / matched domain types only.
        #     Non-matched extractions still write to the parallel
        #     Entity / RELATIONSHIP layer but do not pollute the
        #     meta layer.
        has_domain_types = bool(domain_vt_canonical) or bool(domain_edge_canonical)

        # upsert nodes and edges to the graph
        for doc in extracted:
            # Build a node_id → node_type lookup so the relationship
            # loop below knows the source/target types (the parser
            # currently doesn't carry endpoint types per relationship).
            node_type_by_id: dict = {}
            for n in doc.nodes:
                if not n.id or not n.type:
                    continue
                pid = util.process_id(str(n.id))
                if pid:
                    node_type_by_id[pid] = n.type

            for i, node in enumerate(doc.nodes):
                logger.debug(f"extract writes entity vert to upsert\nNode: {node.id}")
                v_id = util.process_id(str(node.id))
                if len(v_id) == 0:
                    continue
                node_type_lower = (node.type or "").casefold()
                domain_vt = domain_vt_canonical.get(node_type_lower)

                # Strict mode: drop nodes whose type isn't in the
                # schema. When strict_mode is off, non-matched nodes
                # fall through to the parallel Entity layer.
                if strict_mode and domain_vt is None:
                    continue

                desc = await get_vert_desc(conn, v_id, node)

                if len(desc[0]) == 0:
                    desc[0] = str(node.id)

                await upsert_chan.put(
                    (
                        util.upsert_vertex,  # func to call
                        (
                            conn,
                            "Entity",  # v_type
                            v_id,  # v_id
                            {  # attrs
                                "description": desc,
                                "epoch_added": int(time.time()),
                            },
                        ),
                    )
                )
                # Meta-layer (EntityType / ENTITY_HAS_TYPE) population:
                #   Case 1 (no domain types): write for every extracted
                #     node using a normalized form of the LLM-emitted
                #     type label so trivial variants
                #     (``Company`` / ``Companies`` / ``company_type``)
                #     collapse onto one EntityType row.
                #   Case 2 (domain types exist): write only when the
                #     node matches a declared domain VT, using the
                #     canonical domain-VT name as the EntityType id.
                meta_type_id = ""
                if isinstance(extractor, LLMEntityRelationshipExtractor):
                    if not has_domain_types:
                        meta_type_id = util.normalize_type_name(node.type)
                    elif domain_vt is not None:
                        # Preserve the canonical schema casing
                        # (``InvestmentFund``) so the EntityType id
                        # matches what ``upsert_type_metadata`` writes
                        # at schema-apply time. Lowercasing here would
                        # produce a duplicate row keyed
                        # ``investmentfund``.
                        meta_type_id = domain_vt
                if meta_type_id:
                    logger.debug("extract writes type vert to upsert")
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (
                                conn,
                                "EntityType",
                                meta_type_id,
                                {"epoch_added": int(time.time())},
                            ),
                        )
                    )
                    logger.debug("extract writes entity_has_type edge to upsert")
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                "Entity",
                                v_id,
                                "ENTITY_HAS_TYPE",
                                "EntityType",
                                meta_type_id,
                                None,
                            ),
                        )
                    )

                # link the entity to the chunk it came from
                logger.debug("extract writes contains edge to upsert")
                await upsert_chan.put(
                    (
                        util.upsert_edge,
                        (
                            conn,
                            "DocumentChunk",  # src_type
                            chunk_id,  # src_id
                            "CONTAINS_ENTITY",  # edge_type
                            "Entity",  # tgt_type
                            v_id,  # tgt_id
                            None,  # attributes
                        ),
                    )
                )

                # Schema-aware: when the node's type matches a domain
                # vertex type from the live schema, ALSO upsert the
                # vertex as that domain type and link it back to the
                # chunk via the multi-pair CONTAINS_ENTITY pair we
                # added at init time.
                if domain_vt is not None:
                    logger.debug(
                        f"extract writes domain {domain_vt} vert + CONTAINS_ENTITY pair"
                    )
                    # Coerce + filter LLM-emitted properties against
                    # the domain VT's attribute schema before upsert.
                    # The ``description`` key is for the Entity row and
                    # never belongs on the domain VT row, so strip it
                    # before coercion. Domain VTs don't carry the ECC
                    # bookkeeping ``epoch_added`` attribute either —
                    # sending it makes TG reject the whole batch.
                    raw_props = {
                        k: v for k, v in (node.properties or {}).items()
                        if k != "description"
                    }
                    attr_schema = (
                        extractor.entity_type_attributes.get(domain_vt)
                        if isinstance(extractor, LLMEntityRelationshipExtractor)
                        else {}
                    )
                    domain_attrs = util.coerce_attrs_for_schema(
                        raw_props, attr_schema or {}
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (
                                conn,
                                domain_vt,
                                v_id,
                                domain_attrs,
                            ),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                "DocumentChunk",
                                chunk_id,
                                "CONTAINS_ENTITY",
                                domain_vt,
                                v_id,
                                None,
                            ),
                        )
                    )
                for node2 in doc.nodes[i + 1:]:
                    v_id2 = util.process_id(str(node2.id))
                    if len(v_id2) == 0:
                        continue
                    await upsert_chan.put(
                    (
                        util.upsert_edge,
                        (
                            conn,
                            "Entity",  # src_type
                            v_id,  # src_id
                            "RELATIONSHIP",  # edgeType
                            "Entity",  # tgt_type
                            v_id2,  # tgt_id
                            {"relation_type": "DOC_CHUNK_COOCCURRENCE"},  # attributes
                        ),
                    )
                )

            for edge in doc.relationships:
                # Edge content includes entity names + relationship
                # types pulled from user documents.
                logger.debug(
                    f"extract writes relates edge to upsert:{edge.source.id} -({edge.type})->  {edge.target.id}"
                )
                src_id = util.process_id(edge.source.id)
                tgt_id = util.process_id(edge.target.id)
                if len(src_id) == 0 or len(tgt_id) == 0:
                    continue

                # Look up the source / target types from the per-doc
                # node lookup (the parser doesn't currently carry
                # endpoint types per relationship).
                src_type = node_type_by_id.get(src_id, "")
                tgt_type = node_type_by_id.get(tgt_id, "")

                rel_type_lower = (edge.type or "").casefold()
                canonical_rel = domain_edge_canonical.get(rel_type_lower)
                # Use the canonical-resolved name as the key for the
                # endpoint-pair lookup so the check stays correct even
                # if ``domain_edge_canonical`` later admits alias →
                # canonical mappings.
                canonical_rel_key = canonical_rel.casefold() if canonical_rel else ""
                valid_pair = (
                    canonical_rel is not None
                    and (src_type.casefold(), tgt_type.casefold())
                    in edge_endpoint_pairs.get(canonical_rel_key, set())
                )

                # Strict mode: only write the typed pattern. Legacy
                # Entity → RELATIONSHIP → Entity fallback applies only
                # when strict_mode is off.
                if strict_mode and not valid_pair:
                    continue

                # ---- Legacy raw layer: Entity src + Entity tgt + RELATIONSHIP edge ----
                src_desc = await get_vert_desc(conn, src_id, edge.source)
                if len(src_desc[0]) == 0:
                    src_desc[0] = edge.source.id
                await upsert_chan.put(
                    (
                        util.upsert_vertex,
                        (
                            conn,
                            "Entity",
                            src_id,
                            {
                                "description": src_desc,
                                "epoch_added": int(time.time()),
                            },
                        ),
                    )
                )
                tgt_desc = await get_vert_desc(conn, tgt_id, edge.target)
                if len(tgt_desc[0]) == 0:
                    tgt_desc[0] = edge.target.id
                await upsert_chan.put(
                    (
                        util.upsert_vertex,
                        (
                            conn,
                            "Entity",
                            tgt_id,
                            {
                                "description": tgt_desc,
                                "epoch_added": int(time.time()),
                            },
                        ),
                    )
                )
                await upsert_chan.put(
                    (
                        util.upsert_edge,
                        (
                            conn,
                            "Entity",
                            src_id,
                            "RELATIONSHIP",
                            "Entity",
                            tgt_id,
                            {"relation_type": edge.type},
                        ),
                    )
                )

                # ---- Meta-schema typed-relationship layer ----
                # Two cases:
                #   Case 1 (no domain types): every extracted
                #     relationship contributes RelationshipType (via
                #     LLM-emitted edge.type) and IS_HEAD_OF / HAS_TAIL
                #     edges between the corresponding EntityType
                #     vertices (via LLM-emitted src_type / tgt_type).
                #   Case 2 (domain types exist) with valid_pair:
                #     same writes but using canonical (declared) names.
                #   Case 2 without valid_pair: skip the meta-layer
                #     entirely. The Entity / RELATIONSHIP write
                #     above is the only persistence for unmatched
                #     extractions.
                #
                # IS_HEAD_OF / HAS_TAIL connect EntityType ↔
                # RelationshipType (meta layer), NOT individual domain
                # vertex instances. Per-instance domain edges (e.g.
                # ``Company → PUBLISHES → Report``) are written
                # separately when valid_pair holds.
                meta_rel_id = ""
                meta_src_et_id = ""
                meta_tgt_et_id = ""
                if valid_pair:
                    meta_rel_id = canonical_rel
                    # Preserve canonical schema casing so the EntityType
                    # id matches the entity-side write and the row that
                    # ``upsert_type_metadata`` lays down at schema-apply
                    # time (``InvestmentFund``, not
                    # ``investmentfund``).
                    meta_src_et_id = domain_vt_canonical.get(
                        src_type.casefold(), ""
                    )
                    meta_tgt_et_id = domain_vt_canonical.get(
                        tgt_type.casefold(), ""
                    )
                elif not has_domain_types:
                    # Case 1: dedup variants via normalize_type_name so
                    # the meta-layer doesn't overflow with near-duplicate
                    # labels (``Company``/``Companies``,
                    # ``WORKS_FOR``/``works_for_type``, etc.).
                    meta_rel_id = util.normalize_type_name(edge.type)
                    meta_src_et_id = util.normalize_type_name(src_type)
                    meta_tgt_et_id = util.normalize_type_name(tgt_type)

                if meta_rel_id and meta_src_et_id and meta_tgt_et_id:
                    now = int(time.time())
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (conn, "RelationshipType", meta_rel_id, {"epoch_added": now}),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (conn, "EntityType", meta_src_et_id, {"epoch_added": now}),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (conn, "EntityType", meta_tgt_et_id, {"epoch_added": now}),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                "EntityType",
                                meta_src_et_id,
                                "IS_HEAD_OF",
                                "RelationshipType",
                                meta_rel_id,
                                None,
                            ),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                "RelationshipType",
                                meta_rel_id,
                                "HAS_TAIL",
                                "EntityType",
                                meta_tgt_et_id,
                                None,
                            ),
                        )
                    )
                    # Chunk → RelationshipType — fires whenever any
                    # meta-layer write fires (Case 1 always, Case 2 on
                    # valid_pair).
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                "DocumentChunk",
                                chunk_id,
                                "MENTIONS_RELATIONSHIP",
                                "RelationshipType",
                                meta_rel_id,
                                None,
                            ),
                        )
                    )

                if valid_pair:
                    # Schema-aware: also write the canonical domain VT
                    # instances and the per-instance domain edge (the
                    # schema-declared edge name like ``PUBLISHES``).
                    # Domain VTs don't carry the ECC bookkeeping
                    # ``epoch_added`` attribute — sending it makes TG
                    # reject the whole batch with ``Unknown vertex
                    # attribute or vector name: epoch_added``.
                    canonical_src_vt = domain_vt_canonical.get(src_type.casefold())
                    canonical_tgt_vt = domain_vt_canonical.get(tgt_type.casefold())
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (conn, canonical_src_vt, src_id, {}),
                        )
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_vertex,
                            (conn, canonical_tgt_vt, tgt_id, {}),
                        )
                    )
                    # Coerce + filter LLM-emitted edge properties
                    # against the edge's attribute schema. ``description``
                    # is the Entity-side payload and never belongs on
                    # the domain edge row.
                    edge_raw_props = {
                        k: v for k, v in (edge.properties or {}).items()
                        if k != "description"
                    }
                    edge_attr_schema = (
                        extractor.relationship_type_attributes.get(canonical_rel)
                        if isinstance(extractor, LLMEntityRelationshipExtractor)
                        else {}
                    )
                    domain_edge_attrs = util.coerce_edge_attrs_for_schema(
                        edge_raw_props, edge_attr_schema or {}
                    )
                    await upsert_chan.put(
                        (
                            util.upsert_edge,
                            (
                                conn,
                                canonical_src_vt,
                                src_id,
                                canonical_rel,
                                canonical_tgt_vt,
                                tgt_id,
                                domain_edge_attrs or None,
                            ),
                        )
                    )


comm_sem = asyncio.Semaphore(util._worker_concurrency)


async def process_community(
    conn: AsyncTigerGraphConnection,
    upsert_chan: Channel,
    embed_chan: Channel,
    breaker: CommunitySummaryBreaker,
    i: int,
    comm_id: str,
):
    """
    https://github.com/microsoft/graphrag/blob/main/graphrag/prompt_tune/template/community_report_summarization.py

    Get children verts (Entity for layer-1 Communities, Community otherwise)
    if the commuinty only has one child, use its description -- no need to summarize

    embed summaries
    """
    # if loader is running, wait until it's done
    if not util.loading_event.is_set():
        logger.info("Process Community worker waiting for loading event to finish")
        await util.loading_event.wait()

    async with comm_sem:
        logger.info(f"Processing community at layer {i}")
        logger.debug(f"Processing Community: {comm_id}")
        # get the children of the community
        children = await util.get_commuinty_children(conn, i, comm_id)
        comm_id = util.process_id(comm_id)
        err = False

        # if the community only has one child, use its description
        if len(children) == 1:
            summary = children[0]
        elif breaker.open:
            # provider already confirmed unreachable this run: skip the call
            summary = util.COMMUNITY_SUMMARY_PLACEHOLDER
            breaker.incomplete += 1
        else:
            from common.config import get_llm_service, get_completion_config
            llm = get_llm_service(get_completion_config(conn.graphname))
            summarizer = community_summarizer.CommunitySummarizer(llm)
            result = await _summarize_once(summarizer, comm_id, children)
            if not result["error"]:
                breaker.record_success()
                summary = result["summary"]
            elif result.get("category") == "connectivity":
                # connectivity failure: record and DON'T retry — a second call
                # would just burn another timeout against a down provider
                breaker.record_conn_failure()
                logger.error(
                    f"Failed to summarize community {comm_id} (connectivity): "
                    f"{result['message']}"
                )
                summary = util.COMMUNITY_SUMMARY_PLACEHOLDER
                breaker.incomplete += 1
            else:
                # content/transient error: one retry may recover
                result = await _summarize_once(summarizer, comm_id, children)
                if not result["error"]:
                    breaker.record_success()
                    summary = result["summary"]
                else:
                    logger.error(
                        f"Failed to summarize community {comm_id}: {result['message']}"
                    )
                    summary = util.COMMUNITY_SUMMARY_PLACEHOLDER
                    breaker.incomplete += 1

        if not err:
            logger.debug(f"Community {comm_id}: {children}, {summary}")
            await upsert_chan.put(
                (
                    util.upsert_vertex,  # func to call
                    (
                        conn,
                        "Community",  # v_type
                        comm_id,  # v_id
                        {  # attrs
                            "description": summary,
                            "iteration": i,
                        },
                    ),
                )
            )

            # (v_id, content, index_name)
            # Don't embed placeholder summaries — they'd pollute the Community
            # vector index. They get an embedding when regenerated later.
            if summary != util.COMMUNITY_SUMMARY_PLACEHOLDER:
                await embed_chan.put((comm_id, summary, "Community"))
