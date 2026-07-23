# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

import base64
import time
import logging
import httpx
from urllib.parse import quote_plus

import ecc_util

from aiochannel import Channel
from supportai import util
from pyTigerGraph import TigerGraphConnection
from langchain_community.graphs.graph_document import GraphDocument, Node
from common.embeddings.embedding_services import EmbeddingModel
from common.embeddings.base_embedding_store import EmbeddingStore
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


async def install_query(
    conn: TigerGraphConnection, query_path: str, install: bool = True
) -> dict[str, httpx.Response | str | None]:
    LogWriter.info(f"Installing query {query_path}")
    with open(f"{query_path}.gsql", "r") as f:
        query = f.read()

    query_name = query_path.split("/")[-1]
    query = f"""\
USE GRAPH {conn.graphname}
{query}
"""
    if install:
       query += f"""
INSTALL QUERY {query_name}
"""

    async with util.tg_sem:
        res = await conn.gsql(query)

    res_lower = res.lower() if isinstance(res, str) else ""
    if "error" in res_lower or "does not exist" in res_lower or "failed" in res_lower:
        LogWriter.error(res)
        return {
            "result": None,
            "error": True,
            "message": f"Failed to install query {query_name}",
        }

    return {"result": res, "error": False}



async def chunk_doc(
    conn: TigerGraphConnection,
    doc: dict[str, str],
    upsert_chan: Channel,
    embed_chan: Channel,
    extract_chan: Channel,
):
    """
    Chunks a document.
    Places the resulting chunks into the upsert channel (to be upserted to TG)
    and the embed channel (to be embedded and written to the vector store)
    """
    if "ctype" in doc["attributes"]:
        chunker_type = doc["attributes"]["ctype"].lower().strip()
    else:
        chunker_type = ""
    
    v_id = doc["v_id"].lower()
    
    # Use markdown chunker for all documents
    # Image descriptions wrapped in headers will naturally become single chunks
    chunker = ecc_util.get_chunker(chunker_type, graphname=conn.graphname)
    chunks = chunker.chunk(doc["attributes"]["text"])
    
    # v_id / chunk_id derive from user document content; demote
    # to DEBUG so the steady-state log doesn't carry data identifiers.
    logger.debug(f"Chunking {v_id} into {len(chunks)} chunk(s)")
    for i, chunk in enumerate(chunks):
        chunk_id = util.process_id(f"{v_id}_chunk_{i}")
        # send chunks to be upserted (func, args)
        logger.debug("chunk writes to upsert_chan")
        await upsert_chan.put((upsert_chunk, (conn, v_id, chunk_id, chunk)))

        # send chunks to be embedded
        logger.debug("chunk writes to embed_chan")
        await embed_chan.put((chunk_id, chunk, "DocumentChunk"))

        # send chunks to have entities extracted
        logger.debug("chunk writes to extract_chan")
        await extract_chan.put((chunk, chunk_id))

    return doc["v_id"]


async def upsert_chunk(conn: TigerGraphConnection, doc_id, chunk_id, chunk):
    logger.debug(f"Upserting chunk {chunk_id}")
    date_added = int(time.time())
    await util.upsert_vertex(
        conn,
        "DocumentChunk",
        chunk_id,
        attributes={"epoch_added": date_added, "idx": int(chunk_id.split("_")[-1])},
    )
    await util.upsert_vertex(
        conn,
        "Content",
        chunk_id,
        attributes={"text": chunk, "epoch_added": date_added},
    )
    await util.upsert_edge(
        conn, "DocumentChunk", chunk_id, "HAS_CONTENT", "Content", chunk_id
    )
    await util.upsert_edge(
        conn, "Document", doc_id, "HAS_CHILD", "DocumentChunk", chunk_id
    )
    idx = int(chunk_id.split("_")[-1])
    if idx > 0:
        await util.upsert_edge(
            conn,
            "DocumentChunk",
            chunk_id,
            "IS_AFTER",
            "DocumentChunk",
            util.process_id(f"{doc_id}_chunk_{idx - 1}"),
        )
        

async def embed(
    embed_svc: EmbeddingModel,
    embed_store: EmbeddingStore,
    v_id: str,
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
    logger.debug(f"Embedding {v_id}")

    await embed_store.aadd_embeddings([(content, [])], [{"vertex_id": v_id}])


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


async def extract(
    upsert_chan: Channel,
    extractor: BaseExtractor,
    conn: TigerGraphConnection,
    chunk: str,
    chunk_id: str,
):
    logger.debug(f"Extracting chunk: {chunk_id}")
    extracted: list[GraphDocument] = await extractor.aextract(chunk)
    # upsert nodes and edges to the graph
    for doc in extracted:
        for node in doc.nodes:
            logger.debug(f"extract writes entity vert to upsert\nNode: {node.id}")
            v_id = util.process_id(str(node.id))
            if len(v_id) == 0:
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

        for edge in doc.relationships:
            # upsert verts first to make sure their ID becomes an attr
            v_id = edge.type
            if len(v_id) == 0:
                continue

            await upsert_chan.put(
                (
                    util.upsert_vertex,  # func to call
                    (
                        conn,
                        "RelationshipType",  # v_type
                        v_id,
                        {  # attrs
                            "epoch_added": int(time.time()),
                        },
                    ),
                )
            )
            v_id = util.process_id(edge.source.id) # source id
            if len(v_id) == 0:
                continue
            desc = await get_vert_desc(conn, v_id, edge.source)
            await upsert_chan.put(
                (
                    util.upsert_vertex,  # func to call
                    (
                        conn,
                        "Entity",  # v_type
                        v_id,
                        {  # attrs
                            "description": desc,
                            "epoch_added": int(time.time()),
                        },
                    ),
                )
            )
            v_id = util.process_id(edge.target.id) # target id
            if len(v_id) == 0:
                continue
            desc = await get_vert_desc(conn, v_id, edge.target) 
            await upsert_chan.put(
                (
                    util.upsert_vertex,  # func to call
                    (
                        conn,
                        "Entity",  # v_type
                        v_id,  # src_id
                        {  # attrs
                            "description": desc,
                            "epoch_added": int(time.time()),
                        },
                    ),
                )
            )

            # IS_HEAD_OF / HAS_TAIL are meta-schema edges between
            # EntityType ↔ RelationshipType — not written here per
            # Entity instance. Legacy supportai ECC paths without
            # per-instance entity_type info skip the meta-layer.
            # link the relationship to the chunk it came from
            logger.debug("extract writes mentions edge to upsert")
            await upsert_chan.put(
                (
                    util.upsert_edge,
                    (
                        conn,
                        "DocumentChunk",  # src_type
                        chunk_id,  # src_id
                        "MENTIONS_RELATIONSHIP",  # edge_type
                        "RelationshipType",  # tgt_type
                        edge.type,  # tgt_id
                    ),
                )
            )
