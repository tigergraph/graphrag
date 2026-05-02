# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import logging
import time
import traceback
from collections import defaultdict

import httpx
from aiochannel import Channel, ChannelClosed
from graphrag import workers
from graphrag.util import (
    COMMUNITY_QUERIES,
    check_vertex_has_desc,
    http_timeout,
    init,
    install_queries,
    load_q,
    loading_event,
    make_headers,
    stream_ids,
    tg_sem,
    upsert_batch,
)
from pyTigerGraph import AsyncTigerGraphConnection

from common.config import embedding_service, entity_extraction_switch, community_detection_switch, doc_process_switch, get_graphrag_config
from common.embeddings.base_embedding_store import EmbeddingStore
from common.extractors.BaseExtractor import BaseExtractor

logger = logging.getLogger(__name__)

consistency_checkers = {}

async def stream_docs(
    conn: AsyncTigerGraphConnection,
    docs_chan: Channel,
    ttl_batches: int = 10,
):
    """
    Streams the document contents into the docs_chan
    """
    logger.info("streaming docs")
    for i in range(ttl_batches):
        doc_ids = await stream_ids(conn, "Document", i, ttl_batches)
        if doc_ids["error"]:
            # continue to the next batch.
            # These docs will not be marked as processed, so the ecc will process it eventually.
            continue

        for d in doc_ids["ids"]:
            try:
                async with tg_sem:
                    res = await conn.runInstalledQuery(
                        "StreamDocContent",
                        params={"doc": d},
                    )
                logger.info(f"stream_docs writes {d} to docs")
                await docs_chan.put(res[0]["DocContent"][0])
            except Exception as e:
                exc = traceback.format_exc()
                logger.error(f"Error retrieving doc: {d} --> {e}\n{exc}")
                continue  # try retrieving the next doc

    logger.info("stream_docs done")
    # close the docs chan -- this function is the only sender
    logger.info("closing docs chan")
    docs_chan.close()

async def stream_chunks(
    conn: AsyncTigerGraphConnection,
    extract_chan: Channel,
    embed_chan: Channel,
    ttl_batches: int = 10,
):
    """
    Streams the chunk contents into the extract_chan and embed_chan
    """
    logger.info("streaming chunks")
    for i in range(ttl_batches):
        chunk_ids = await stream_ids(conn, "DocumentChunk", i, ttl_batches)
        if chunk_ids["error"]:
            continue

        for c in chunk_ids["ids"]:
            try:
                async with tg_sem:
                    res = await conn.runInstalledQuery(
                        "StreamChunkContent",
                        params={"chunk": c},
                    )
                content = res[0]["ChunkContent"][0]["attributes"]["text"].encode('raw_unicode_escape').decode('unicode_escape')
                logger.info("chunk writes to extract_chan")
                await extract_chan.put((content, c))

                # send chunks to be embedded
                logger.info("chunk writes to embed_chan")
                await embed_chan.put((c, content, "DocumentChunk"))
            except Exception as e:
                exc = traceback.format_exc()
                logger.error(f"Error retrieving chunk: {c} --> {e}\n{exc}")
                continue  # try retrieving the next doc

    logger.info("stream_chunks done")
    logger.info("closing extract_chan")
    await extract_chan.put(None)


async def chunk_docs(
    conn: AsyncTigerGraphConnection,
    docs_chan: Channel,
    embed_chan: Channel,
    upsert_chan: Channel,
    extract_chan: Channel,
):
    """
    Creates and starts one worker for each document
    in the docs channel.
    """
    logger.info("Chunk Processing Start")
    doc_tasks = []
    async with asyncio.TaskGroup() as grp:
        while True:
            try:
                content = await docs_chan.get()
                task = grp.create_task(
                    workers.chunk_doc(conn, content, upsert_chan, embed_chan, extract_chan)
                )
                doc_tasks.append(task)
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("Chunk Processing End")

    logger.info("closing extract_chan")
    await extract_chan.put(None)


async def upsert(upsert_chan: Channel):
    """
    Creates and starts one worker for each upsert job
    chan expects:
    (func, args) <- q.get()
    """

    logger.info("Data Upserting Start")
    # consume task queue
    async with asyncio.TaskGroup() as grp:
        while True:
            try:
                (func, args) = await upsert_chan.get()
                logger.info(f"Upserting with {func.__name__}, {args[1:3]}")
                # execute the task
                grp.create_task(func(*args))
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("Data Upserting End")
    logger.info("closing load_q chan")
    load_q.close()


async def load(conn: AsyncTigerGraphConnection):
    logger.info("Data Loading Start")
    dd = lambda: defaultdict(dd)  # infinite default dict
    graph_cfg = get_graphrag_config(conn.graphname)
    batch_size = graph_cfg.get("load_batch_size", 500)
    upsert_delay = graph_cfg.get("upsert_delay", 0)
    # while the load q is still open or has contents
    while not load_q.closed() or not load_q.empty():
        if load_q.closed():
            logger.info(
                f"load queue closed. Flushing load_q (final load for this stage)"
            )
        # if there's `batch_size` entities in the channel, load it
        # or if the channel is closed, flush it
        if load_q.qsize() >= batch_size or load_q.closed() or load_q.should_flush():
            batch = {
                "vertices": defaultdict(dict[str, any]),
                "edges": dd(),
            }
            n_verts = 0
            n_edges = 0
            size = (
                load_q.qsize()
                if load_q.closed() or load_q.should_flush()
                else batch_size
            )
            for _ in range(size):
                t, elem = await load_q.get()
                if t == "FLUSH":
                    logger.debug(f"flush received: {t}")
                    load_q._should_flush = False
                    break
                match t:
                    case "vertices":
                        vt, v_id, attr = elem
                        batch[t][vt][v_id] = attr
                        n_verts += 1
                    case "edges":
                        src_v_type, src_v_id, edge_type, tgt_v_type, tgt_v_id, attrs = (
                            elem
                        )
                        batch[t][src_v_type][src_v_id][edge_type][tgt_v_type][
                            tgt_v_id
                        ] = attrs
                        n_edges += 1
                    case _:
                        logger.debug(f"Unexpected data {t} -> {elem} in load_q")

            data = json.dumps(batch)
            logger.info(
                f"Upserting batch size of {size}. ({n_verts} verts | {n_edges} edges. {len(data.encode())/1000:,} kb)"
            )

            if n_verts > 0 or n_edges > 0:
                loading_event.clear()
                await upsert_batch(conn, data)
                loading_event.set()
                if upsert_delay > 0:
                    await asyncio.sleep(upsert_delay)
        else:
            await asyncio.sleep(1)

    # TODO: flush q if it's not empty
    if not load_q.empty():
        raise Exception(f"load_q not empty: {load_q.qsize()}", flush=True)
    logger.info("Data Loading End")


async def embed(
    embed_chan: Channel, embedding_store: EmbeddingStore, graphname: str
):
    """
    Creates and starts one worker for each embed job
    chan expects:
    (v_id, content, index_name) <- q.get()
    """
    logger.info("Embedding Processing Start")
    async with asyncio.TaskGroup() as grp:
        # consume task queue
        while True:
            try:
                (v_id, content, index_name) = await embed_chan.get()
                v_id = (v_id, index_name)
                logger.info(f"Embed to {graphname}_{index_name}: {v_id}")
                if get_graphrag_config(graphname).get("reuse_embedding", True) and embedding_store.has_embeddings([v_id]):
                    logger.info(f"Embeddings for {v_id} already exists, skipping to save cost")
                    continue
                grp.create_task(
                    workers.embed(
                        embedding_service,
                        embedding_store,
                        v_id,
                        content,
                    )
                )
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("Embedding Processing End")


async def extract(
    extract_chan: Channel,
    upsert_chan: Channel,
    embed_chan: Channel,
    extractor: BaseExtractor,
    conn: AsyncTigerGraphConnection,
    num_senders: int,
):
    """
    Creates and starts one worker for each extract job
    chan expects:
    (chunk , chunk_id) <- q.get()
    """
    logger.info("Entity Extration Start")
    # consume task queue
    async with asyncio.TaskGroup() as grp:
        done_count = 0
        while True:
            try:
                item = await extract_chan.get()
                if item is None:  # sender finished
                    done_count += 1
                    if done_count == num_senders:
                        logger.info("All senders finished, exiting extract.")
                        break
                else:
                    if entity_extraction_switch:
                        grp.create_task(
                            workers.extract(upsert_chan, extractor, conn, *item)
                        )
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("Entity Extration End")

    logger.info("closing extract, upsert and embed chan")
    extract_chan.close()
    upsert_chan.close()
    embed_chan.close()


async def communities(conn: AsyncTigerGraphConnection, comm_process_chan: Channel):
    """
    Run louvain
    """
    # first pass: Group Entities into Communities
    logger.info("Initializing Communities (first louvain pass)")

    async with tg_sem:
        try:
            res = await conn.runInstalledQuery(
                "graphrag_louvain_init",
                params={"n_batches": 1}
            )
        except Exception as e:
            exc = traceback.format_exc()
            logger.error(f"Error running query: graphrag_louvain_init\n{exc}")

    # get the modularity
    async with tg_sem:
        try:
            res = await conn.runInstalledQuery(
                "modularity",
                params={"iteration": 1, "batch_num": 1}
            )
        except Exception as e:
            exc = traceback.format_exc()
            logger.error(f"Error running query: modularity\n{exc}")

    mod = res[0]["mod"]
    logger.info(f"****mod pass 1: {mod}")
    await stream_communities(conn, 1, comm_process_chan)

    # nth pass: Iterate on Communities until modularity stops increasing
    prev_mod = -10
    i = 0
    while abs(prev_mod - mod) > 0.0000001 and prev_mod != 0:
        prev_mod = mod
        i += 1
        logger.info(f"Running louvain on Communities (iteration: {i})")
        # louvain pass
        async with tg_sem:
            res = await conn.runInstalledQuery(
                "graphrag_louvain_communities",
                params={"n_batches": 1, "iteration": i},
            )

        # get the modularity
        async with tg_sem:
            res = await conn.runInstalledQuery(
                "modularity",
                params={"iteration": i + 1, "batch_num": 1},
            )
        mod = res[0]["mod"]
        logger.info(f"mod pass {i+1}: {mod} (diff= {abs(prev_mod - mod)})")
        # write iter to chan for layer to be processed
        await stream_communities(conn, i + 1, comm_process_chan)

        if mod == 0 or mod - prev_mod <= -0.05:
            break


    # TODO: erase last run since it's ∆q to the run before it will be small
    logger.info("closing communities chan")
    comm_process_chan.close()
    logger.info("communities done")


async def stream_communities(
    conn: AsyncTigerGraphConnection,
    i: int,
    comm_process_chan: Channel,
):
    """
    Streams Community IDs from the grpah for a given iteration (from the channel)
    """
    logger.info("streaming communities")

    headers = make_headers(conn)

    # async for i in community_chan:
    # get the community from that layer
    async with tg_sem:
        resp = await conn.runInstalledQuery(
            "stream_community",
            params={"iter": i}
        )
    comms = resp[0]["Comms"]

    for c in comms:
        await comm_process_chan.put((i, c["v_id"]))

    # Wait for all communities for layer i to be processed before doing next layer
    # all community descriptions must be populated before the next layer can be processed
    if len(comms) > 0:
        n_waits = 0
        while not await check_vertex_has_desc(conn, i):
            logger.info(f"Waiting for layer{i} to finish processing")
            await asyncio.sleep(5)
            n_waits += 1
            if n_waits > 3:
                logger.info("Flushing load_q")
                await load_q.flush(("FLUSH", None))
        await asyncio.sleep(3)
    logger.info("stream_communities done")


async def summarize_communities(
    conn: AsyncTigerGraphConnection,
    comm_process_chan: Channel,
    upsert_chan: Channel,
    embed_chan: Channel,
):
    async with asyncio.TaskGroup() as tg:
        while True:
            try:
                c = await comm_process_chan.get()
                tg.create_task(workers.process_community(conn, upsert_chan, embed_chan, *c))
                logger.debug(f"Added community to process: {c}")
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("closing upsert_chan")
    upsert_chan.close()
    embed_chan.close()
    logger.info("summarize_communities done")


async def run(graphname: str, conn: AsyncTigerGraphConnection):
    """
    Set up GraphRAG:
        - Install necessary queries.
        - Process the documents into:
            - chunks
            - embeddings
            - entities/relationships
            - upsert everything to the graph
        - Detect communities and summarize them
    """

    extractor, embedding_store = await init(conn)
    init_start = time.perf_counter()

    if doc_process_switch:
        logger.info("Doc Processing Start")
        docs_chan = Channel(1)
        embed_chan = Channel()
        upsert_chan = Channel()
        extract_chan = Channel()
        num_chunk_senders = 2

        async with asyncio.TaskGroup() as grp:
            # get docs
            grp.create_task(stream_docs(conn, docs_chan, 100))
            # process docs
            grp.create_task(
                chunk_docs(conn, docs_chan, embed_chan, upsert_chan, extract_chan)
            )
            # process existing chunks
            grp.create_task(stream_chunks(conn, extract_chan, embed_chan, 100))

            # upsert chunks
            grp.create_task(upsert(upsert_chan))
            grp.create_task(load(conn))
            # embed
            grp.create_task(embed(embed_chan, embedding_store, graphname))
            # extract entities
            grp.create_task(
                extract(extract_chan, upsert_chan, embed_chan, extractor, conn, num_chunk_senders)
            )
    logger.info("Join docs_chan")
    await docs_chan.join()
    logger.info("Join extract_chan")
    await extract_chan.join()
    logger.info("Join embed_chan")
    await embed_chan.join()
    logger.info("Join upsert_chan")
    await upsert_chan.join()
    init_end = time.perf_counter()
    logger.info("Doc Processing End")

    # Type Resolution — IS_HEAD_OF / HAS_TAIL writes happen inline in
    # the per-relationship extract step (workers.py); no post-processing
    # query needed.
    type_start = time.perf_counter()
    type_end = time.perf_counter()

    # Community Detection
    # Ensure community queries are installed.  Per TG docs, only DROP operations
    # invalidate queries (not ADD/ALTER), but partial init failures or manual
    # schema edits could still leave queries missing.
    community_start = time.perf_counter()
    if community_detection_switch:
        await install_queries(COMMUNITY_QUERIES, conn)
        logger.info("Community Processing Start")
        comm_process_chan = Channel()
        upsert_chan = Channel()
        embed_chan = Channel()
        load_q.reopen()
        async with asyncio.TaskGroup() as grp:
            # run louvain
            # get the communities
            grp.create_task(communities(conn, comm_process_chan))
            # summarize each community
            grp.create_task(
                summarize_communities(conn, comm_process_chan, upsert_chan, embed_chan)
            )
            grp.create_task(upsert(upsert_chan))
            grp.create_task(load(conn))
            grp.create_task(embed(embed_chan, embedding_store, graphname))
        logger.info("Join comm_process_chan")
        await comm_process_chan.join()
        logger.info("Join embed_chan")
        await embed_chan.join()
        logger.info("Join upsert_chan")
        await upsert_chan.join()
    community_end = time.perf_counter()
    logger.info("Community Processing End")

    end = time.perf_counter()
    logger.info(f"DONE. graphrag system initializer dT: {init_end-init_start}")
    logger.info(f"DONE. graphrag type creation dT: {type_end-type_start}")
    logger.info(
        f"DONE. graphrag community initializer dT: {community_end-community_start}"
    )
    logger.info(f"DONE. graphrag.run() total time elaplsed: {end-init_start}")

    # Verify all required queries and loading jobs are still intact after the
    # pipeline.  Per TG docs, only DROP invalidates queries, and ALTER/DROP
    # invalidates loading jobs.  Log any missing ones to catch unexpected side
    # effects from schema changes.
    from graphrag.util import REQUIRED_QUERIES
    installed = set(
        q.split("/")[-1]
        for q in await conn.getEndpoints(dynamic=True)
        if f"/{conn.graphname}/" in q
    )
    expected = {q.split("/")[-1] for q in REQUIRED_QUERIES}
    missing = expected - installed
    if missing:
        logger.error(
            f"Queries missing after ECC pipeline: {sorted(missing)}."
        )
    else:
        logger.info("Post-pipeline check: all required queries are installed.")

    current_schema = await conn.gsql(f"USE GRAPH {conn.graphname}\nls")
    expected_jobs = [
        "load_documents_content_json",
        "load_documents_content_json_with_images",
    ]
    missing_jobs = [
        j for j in expected_jobs
        if f"- CREATE LOADING JOB {j} {{" not in current_schema
    ]
    if missing_jobs:
        logger.error(
            f"Loading jobs missing after ECC pipeline: {missing_jobs}."
        )
    else:
        logger.info("Post-pipeline check: all loading jobs are intact.")
