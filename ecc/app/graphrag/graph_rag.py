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
from collections import Counter, defaultdict

import httpx
from aiochannel import Channel, ChannelClosed
from graphrag import workers
from graphrag.util import (
    COMMUNITY_QUERIES,
    community_desc_progress,
    http_timeout,
    init,
    install_queries,
    load_q,
    loading_event,
    make_headers,
    graphrag_mirror_communities,
    stream_ids,
    tg_sem,
    upsert_batch,
)
from common.db.schema_utils import is_structural_type, read_existing_schema_async
from pyTigerGraph import AsyncTigerGraphConnection

from common.config import embedding_service, entity_extraction_switch, community_detection_switch, doc_process_switch, get_graphrag_config
from common.embeddings.base_embedding_store import EmbeddingStore
from common.extractors.BaseExtractor import BaseExtractor

logger = logging.getLogger(__name__)

consistency_checkers = {}


class _DocProgress:
    """Tracks documents through the merged chunk+extract phase so the UI can
    show a single 'Processing documents' bar that advances at extraction pace
    (the slow, LLM-bound part) against a fixed document denominator — no
    moving-denominator jump, and no misleading jump to 100% when chunking
    finishes early.

    A document is complete once it has been chunked and all of its chunks have
    been extracted. When extraction is disabled, a document is complete as soon
    as it is chunked.

    Every method is defensive: a progress-tracking error must never break the
    ingestion pipeline.
    """

    def __init__(self, report, extraction_on: bool):
        self._report = report
        self._extraction_on = extraction_on
        self._total = 0
        self._completed = 0
        self._chunk_to_doc: dict = {}
        self._doc_remaining: dict = {}

    def set_total(self, total: int) -> None:
        try:
            self._total = int(total or 0)
            self._emit()
        except Exception:
            pass

    def register(self, doc_key: str, chunk_ids: list) -> None:
        """Called once per document after chunking, with the ids of its chunks."""
        try:
            if not self._extraction_on or not chunk_ids:
                self._completed += 1
                self._emit()
                return
            self._doc_remaining[doc_key] = len(chunk_ids)
            for cid in chunk_ids:
                self._chunk_to_doc[cid] = doc_key
        except Exception:
            pass

    def chunk_done(self, chunk_id: str) -> None:
        """Called after a chunk finishes extraction. Ticks the doc's counter and
        advances the bar when the document's last chunk clears. Unknown chunk ids
        (e.g. residual orphans streamed outside the new-doc pipeline) are ignored."""
        try:
            doc = self._chunk_to_doc.pop(chunk_id, None)
            if doc is None:
                return
            remaining = self._doc_remaining.get(doc, 0) - 1
            if remaining <= 0:
                self._doc_remaining.pop(doc, None)
                self._completed += 1
                self._emit()
            else:
                self._doc_remaining[doc] = remaining
        except Exception:
            pass

    def _emit(self) -> None:
        if self._report is None or self._total <= 0:
            return
        completed = min(self._completed, self._total)
        pct = min(100, int(100 * completed / self._total))
        try:
            self._report(
                f"Processing documents ({completed}/{self._total} — {pct}%)",
                current=completed,
                total=self._total,
            )
        except Exception:
            pass

async def stream_docs(
    conn: AsyncTigerGraphConnection,
    docs_chan: Channel,
    ttl_batches: int = 10,
    progress=None,
):
    """
    Streams the document contents into the docs_chan, over ``ttl_batches``
    partitions (each StreamIds call claims and returns one partition).

    *progress* is accepted for API compatibility with callers but is
    not used here — the UI stage advances after chunk_docs finishes,
    not when streaming completes.
    """
    logger.info(f"streaming docs ({ttl_batches} batches)")
    n_docs = 0
    for i in range(ttl_batches):
        doc_ids = await stream_ids(conn, "Document", i, ttl_batches)
        if doc_ids["error"]:
            continue
        for d in doc_ids["ids"]:
            try:
                async with tg_sem:
                    res = await conn.runInstalledQuery(
                        "StreamDocContent",
                        params={"doc": (d,)},
                    )
                logger.debug(f"stream_docs writes '{d}' to docs")
                await docs_chan.put(res[0]["DocContent"][0])
                n_docs += 1
            except Exception as e:
                exc = traceback.format_exc()
                logger.error(f"Error retrieving doc: '{d}' --> {e}\n{exc}")
                continue

    logger.info(f"stream_docs done: {n_docs} document(s) streamed")
    # close the docs chan -- this function is the only sender
    logger.info("closing docs chan")
    docs_chan.close()
    # Do NOT advance the UI stage here. Streaming finishing does not
    # mean chunking is done; advancing here used to clear the chunking
    # progress bar before chunk_docs finished (and before the UI polled).

async def stream_chunks(
    conn: AsyncTigerGraphConnection,
    extract_chan: Channel,
    embed_chan: Channel,
    ttl_batches: int = 10,
):
    """
    Stream residual unprocessed DocumentChunks into extract_chan + embed_chan,
    over ``ttl_batches`` partitions (each StreamIds call claims and returns one
    partition via ``getvid % ttl_batches``). Under the current ordering
    stream_chunks runs before chunk_docs writes new chunks, so it normally only
    sees residual orphans from a prior crashed ECC run.
    """
    logger.info(f"streaming chunks ({ttl_batches} batches)")
    n_chunks = 0
    for i in range(ttl_batches):
        chunk_ids = await stream_ids(conn, "DocumentChunk", i, ttl_batches)
        if chunk_ids["error"]:
            continue
        for c in chunk_ids["ids"]:
            try:
                # stream_chunks runs before chunk_docs writes new chunks, so an
                # empty ChunkContent here means a genuine orphan from a crashed
                # prior run (DocumentChunk exists but no Content). Retry briefly
                # in case of a transient read.
                chunk_rows = []
                for attempt in range(3):
                    async with tg_sem:
                        res = await conn.runInstalledQuery(
                            "StreamChunkContent",
                            params={"chunk": (c,)},
                        )
                    chunk_rows = (res[0] if res else {}).get("ChunkContent") or []
                    if chunk_rows:
                        break
                    await asyncio.sleep(2 * (attempt + 1))
                if not chunk_rows:
                    logger.warning(
                        f"No content row for chunk {c} after retries; skipping"
                    )
                    continue
                content = chunk_rows[0]["attributes"]["text"].encode(
                    'raw_unicode_escape'
                ).decode('unicode_escape')
                logger.debug("chunk writes to extract_chan")
                await extract_chan.put((content, c))
                # With extraction on, the extract worker pushes the
                # summary-augmented embed; only embed raw here when it's off.
                if not entity_extraction_switch:
                    logger.debug("chunk writes to embed_chan")
                    await embed_chan.put((c, content, "DocumentChunk"))
                n_chunks += 1
                if n_chunks % 100 == 0:
                    logger.info(f"streaming chunks: {n_chunks} streamed")
            except Exception as e:
                exc = traceback.format_exc()
                logger.error(f"Error retrieving chunk: {c} --> {e}\n{exc}")
                continue

    logger.info(f"stream_chunks done: {n_chunks} chunk(s) streamed")
    logger.info("closing extract_chan")
    await extract_chan.put(None)


async def chunk_docs(
    conn: AsyncTigerGraphConnection,
    docs_chan: Channel,
    embed_chan: Channel,
    upsert_chan: Channel,
    extract_chan: Channel,
    progress=None,
    total_docs: int = 0,
    tracker: "_DocProgress | None" = None,
):
    """
    Creates and starts one worker for each document
    in the docs channel.

    Document-level progress for the merged 'Processing documents' stage is
    driven by *tracker*, which advances the bar as each document finishes
    extraction (see :class:`_DocProgress`). This function no longer reports its
    own chunking percentage — the tracker is the single source of truth.
    """
    logger.info("Chunk Processing Start")
    n_docs = 0

    async def _chunk_one(content):
        nonlocal n_docs
        await workers.chunk_doc(
            conn, content, upsert_chan, embed_chan, extract_chan, tracker=tracker
        )
        n_docs += 1

    async with asyncio.TaskGroup() as grp:
        while True:
            try:
                content = await docs_chan.get()
                grp.create_task(_chunk_one(content))
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info(f"Chunk Processing End: {n_docs} document(s) processed")

    logger.info("closing extract_chan")
    await extract_chan.put(None)


async def upsert(upsert_chan: Channel):
    """
    Creates and starts one worker for each upsert job
    chan expects:
    (func, args) <- q.get()
    """

    logger.info("Data Upserting Start")
    n_upserts = 0
    # consume task queue
    async with asyncio.TaskGroup() as grp:
        while True:
            try:
                (func, args) = await upsert_chan.get()
                # Demoted from INFO — ``args`` carries vertex IDs and
                # payloads derived from user documents.
                logger.debug(f"Upserting with {func.__name__}, {args[1:3]}")
                # execute the task
                grp.create_task(func(*args))
                n_upserts += 1
                # Heartbeat every 200 upserts so a long stage doesn't
                # look stalled in the INFO log without exposing the
                # underlying data.
                if n_upserts % 200 == 0:
                    logger.info(f"Data Upserting: {n_upserts} dispatched")
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info(f"Data Upserting End: {n_upserts} dispatched")
    logger.info("closing load_q chan")
    load_q.close()


async def load(conn: AsyncTigerGraphConnection):
    logger.info("Data Loading Start")
    dd = lambda: defaultdict(dd)  # infinite default dict
    graph_cfg = get_graphrag_config(conn.graphname)
    batch_size = graph_cfg.get("load_batch_size", 500)
    upsert_delay = graph_cfg.get("upsert_delay", 0)
    batch_seq = 0
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
            # Cap every batch at batch_size — even on close / flush. Extraction
            # can flood the queue faster than TG drains it; sending the whole
            # backlog as one upsert produces a multi-GB request that RESTPP
            # rejects with 413 (losing the batch). The enclosing loop drains
            # the remainder in subsequent passes.
            size = min(load_q.qsize(), batch_size)
            for _ in range(size):
                t, elem = await load_q.get()
                if t == "FLUSH":
                    logger.debug(f"flush received: {t}")
                    load_q._should_flush = False
                    break
                match t:
                    case "vertices":
                        vt, v_id, attr = elem
                        batch["vertices"][vt][v_id] = attr
                    case "edges":
                        src_v_type, src_v_id, edge_type, tgt_v_type, tgt_v_id, attrs = (
                            elem
                        )
                        batch["edges"][src_v_type][src_v_id][edge_type][tgt_v_type][
                            tgt_v_id
                        ] = attrs
                    case "group":
                        # Atomic multi-vertex + multi-edge bundle from
                        # ``upsert_group``. Producers enqueue all related
                        # ops as one item so cancellation can't split
                        # them; unpack each into the same batch dict so
                        # they reach TG in one upsertData call.
                        for vt, v_id, attr in elem.get("vertices", []):
                            batch["vertices"][vt][v_id] = attr
                        for (src_v_type, src_v_id, edge_type, tgt_v_type, tgt_v_id, attrs) in elem.get("edges", []):
                            batch["edges"][src_v_type][src_v_id][edge_type][tgt_v_type][tgt_v_id] = attrs
                    case _:
                        logger.debug(f"Unexpected data {t} -> {elem} in load_q")

            # Count DISTINCT vertices/edges actually in the batch dict, not raw
            # drained items. Repeated primary ids / edge tuples collapse onto the
            # same key (last write wins) before the send, so the drained count
            # overstates what reaches TG. Reporting the distinct counts makes the
            # upsert-response GAP reflect genuine TG rejections, not in-batch dedup.
            vt_counts: Counter = Counter(
                {vt: len(ids) for vt, ids in batch["vertices"].items()}
            )
            et_counts: Counter = Counter()
            for srcs in batch["edges"].values():
                for etypes in srcs.values():
                    for edge_type, tgts in etypes.items():
                        for tgt_ids in tgts.values():
                            et_counts[edge_type] += len(tgt_ids)
            n_verts = sum(vt_counts.values())
            n_edges = sum(et_counts.values())

            batch_seq += 1
            if n_verts > 0 or n_edges > 0:
                data = json.dumps(batch)
                vt_brief = ", ".join(f"{k}={n}" for k, n in vt_counts.most_common()) or "-"
                et_brief = ", ".join(f"{k}={n}" for k, n in et_counts.most_common()) or "-"
                logger.info(
                    f"Upserting batch #{batch_seq}: size {size}. "
                    f"({n_verts} verts [{vt_brief}] | {n_edges} edges [{et_brief}]. "
                    f"{len(data.encode())/1000:,} kb)"
                )
                loading_event.clear()
                await upsert_batch(
                    conn, data,
                    expected_vertices=n_verts, expected_edges=n_edges,
                    batch_seq=batch_seq,
                )
                loading_event.set()
                if upsert_delay > 0:
                    await asyncio.sleep(upsert_delay)
            else:
                logger.info(
                    f"Skipping empty batch #{batch_seq}: size {size} "
                    f"(no vertices or edges to send)"
                )
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
    n_embed = 0
    n_reused = 0
    async with asyncio.TaskGroup() as grp:
        # consume task queue
        while True:
            try:
                (v_id, content, index_name) = await embed_chan.get()
                v_id = (v_id, index_name)
                # v_id is a per-vertex identifier derived from user content.
                logger.debug(f"Embed to {graphname}_{index_name}: {v_id}")
                if get_graphrag_config(graphname).get("reuse_embedding", True) and embedding_store.has_embeddings([v_id]):
                    logger.debug(f"Embeddings for {v_id} already exists, skipping to save cost")
                    n_reused += 1
                    continue
                grp.create_task(
                    workers.embed(
                        embedding_service,
                        embedding_store,
                        v_id,
                        content,
                    )
                )
                n_embed += 1
                if n_embed % 100 == 0:
                    logger.info(f"Embedding Processing: {n_embed} embedded so far")
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info(
        f"Embedding Processing End: {n_embed} embedded, {n_reused} reused"
    )


async def extract(
    extract_chan: Channel,
    upsert_chan: Channel,
    embed_chan: Channel,
    extractor: BaseExtractor,
    conn: AsyncTigerGraphConnection,
    num_senders: int,
    tracker: "_DocProgress | None" = None,
):
    """
    Creates and starts one worker for each extract job
    chan expects:
    (chunk , chunk_id) <- q.get()
    """
    logger.info("Entity Extration Start")

    async def _extract_one(itm):
        # itm == (chunk, chunk_id); tick the document tracker once this chunk's
        # extraction is done (even on error) so the 'Processing documents' bar
        # advances at extraction pace.
        try:
            await workers.extract(upsert_chan, embed_chan, extractor, conn, *itm)
        finally:
            if tracker is not None:
                tracker.chunk_done(itm[1])

    # consume task queue
    n_chunks = 0
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
                        grp.create_task(_extract_one(item))
                        n_chunks += 1
                        if n_chunks % 50 == 0:
                            logger.info(
                                f"Entity Extraction: {n_chunks} chunks dispatched"
                            )
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info(f"Entity Extration End: {n_chunks} chunks extracted")

    logger.info("closing extract, upsert and embed chan")
    extract_chan.close()
    upsert_chan.close()
    embed_chan.close()


async def communities(conn: AsyncTigerGraphConnection, comm_process_chan: Channel, report=None):
    """
    Run louvain
    """
    def _level(n):
        if report is not None:
            try:
                report(f"Detecting communities — level {n}", clear_progress=True)
            except Exception:
                pass

    # first pass: Group Entities into Communities
    logger.info("Initializing Communities (first louvain pass)")
    _level(1)

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
    await stream_communities(conn, 1, comm_process_chan, report=report)

    # nth pass: Iterate on Communities until modularity stops increasing
    prev_mod = -10
    i = 0
    while abs(prev_mod - mod) > 0.0000001 and prev_mod != 0:
        prev_mod = mod
        i += 1
        logger.info(f"Running louvain on Communities (iteration: {i})")
        _level(i + 1)
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
        await stream_communities(conn, i + 1, comm_process_chan, report=report)

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
    report=None,
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
        wait_interval = 5
        # Give up waiting once no new community has been described for longer
        # than one summarization timeout (+ margin). With the fast-fail timeout
        # and circuit breaker upstream, failures write a placeholder quickly, so
        # this only trips if summarization is truly stuck — never an infinite loop.
        stall_limit_s = workers._SUMMARY_TIMEOUT_S + 60
        last_described = -1
        stalled_for = 0
        n_waits = 0
        while True:
            progress = await community_desc_progress(conn, i)
            if progress is None:
                # progress query failed; treat as no forward progress
                all_have, described, total = False, last_described, None
            else:
                all_have, described, total = progress
            if report is not None and total:
                try:
                    report(
                        f"Summarizing communities — level {i} ({described}/{total})",
                        current=described,
                        total=total,
                    )
                except Exception:
                    pass
            if all_have:
                break
            if described > last_described:
                last_described = described
                stalled_for = 0
            else:
                stalled_for += wait_interval
            if stalled_for >= stall_limit_s:
                logger.error(
                    f"Layer {i} summarization stalled at {described}/{total} "
                    f"for {stalled_for}s; abandoning wait and moving on."
                )
                break
            logger.info(
                f"Waiting for layer{i} to finish processing ({described}/{total})"
            )
            await asyncio.sleep(wait_interval)
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
    breaker: "workers.CommunitySummaryBreaker",
):
    logger.info("Community summarization started")
    n_comm = 0
    async with asyncio.TaskGroup() as tg:
        while True:
            try:
                c = await comm_process_chan.get()
                tg.create_task(workers.process_community(conn, upsert_chan, embed_chan, breaker, *c))
                logger.debug(f"Added community to process: {c}")
                n_comm += 1
                # Per-community summarization can take 30s; emit a
                # heartbeat every 20 so a long run doesn't go silent.
                if n_comm % 20 == 0:
                    logger.info(
                        f"Community summarization: {n_comm} dispatched"
                    )
            except ChannelClosed:
                break
            except Exception:
                raise

    logger.info("closing upsert_chan")
    upsert_chan.close()
    embed_chan.close()
    logger.info(
        f"Community summarization done: {n_comm} communities dispatched"
    )
    if breaker.incomplete:
        logger.warning(
            f"{breaker.incomplete} community summaries could not be generated "
            f"(LLM provider errors) and were left as placeholders; regenerate "
            f"them once connectivity is restored."
        )


async def run(graphname: str, conn: AsyncTigerGraphConnection, progress=None):
    """
    Set up GraphRAG:
        - Install necessary queries.
        - Process the documents into:
            - chunks
            - embeddings
            - entities/relationships
            - upsert everything to the graph
        - Detect communities and summarize them

    *progress* is an optional ``Callable[[str], None]`` invoked at
    each user-visible sub-phase; ECC's ``run_with_tracking`` wires it
    to the task's ``stage`` field so the UI rebuild dialog can show
    where the job is.
    """

    def _report(msg: str, current=None, total=None, clear_progress=False) -> None:
        if progress is None:
            return
        try:
            progress(
                msg,
                current=current,
                total=total,
                clear_progress=clear_progress,
            )
        except TypeError:
            try:
                progress(msg)
            except Exception:
                pass
        except Exception:
            pass

    # Summary bubbled up to run_with_tracking for the completion status /
    # partial-success message.
    run_summary = {"communities_incomplete": 0, "warnings": []}

    _report("Preparing rebuild")
    extractor, embedding_store = await init(conn)
    init_start = time.perf_counter()

    if doc_process_switch:
        _report("Processing documents")
        logger.info("Doc Processing Start")
        docs_chan = Channel(1)
        embed_chan = Channel()
        upsert_chan = Channel()
        extract_chan = Channel()
        num_chunk_senders = 2
        # One bar for the merged chunk+extract phase; it advances as each
        # document finishes extraction (see _DocProgress).
        doc_tracker = _DocProgress(_report, entity_extraction_switch)

        async with asyncio.TaskGroup() as grp:
            # PHASE 1 — residual chunk sweep
            # stream_chunks is the gatekeeper for chunks that exist in TG
            # but weren't fully processed (e.g. crashed prior ECC run, or
            # DocumentChunk/Content vertices loaded by external means).
            # It MUST complete before stream_docs/chunk_docs run, otherwise
            # the concurrent upsertData visibility window (vertex visible
            # in StreamIds query before its HAS_CONTENT edge commits) makes
            # stream_chunks claim freshly-being-written chunks and emit
            # spurious "No content row for chunk" warnings.
            sc_task = grp.create_task(stream_chunks(conn, extract_chan, embed_chan, 100))

            # PHASE 2 — new-doc pipeline (stream_docs + chunk_docs)
            # Deferred behind stream_chunks completion. chunk_docs feeds
            # extract_chan with chunk text directly from Python memory, so
            # the extract worker doesn't need any TG round-trip for new
            # chunks. With the residual sweep already done, there's no
            # remaining producer racing chunk_docs' load_q writes.
            async def new_doc_pipeline():
                await sc_task
                # Count unprocessed Documents before StreamIds claims them,
                # so chunking can report document-level % progress.
                total_docs = 0
                try:
                    count_result = conn.getVertexCount(
                        "Document", where="epoch_processed=0"
                    )
                    if asyncio.iscoroutine(count_result):
                        count_result = await count_result
                    total_docs = int(count_result or 0)
                except Exception as e:
                    logger.warning(
                        f"Could not count unprocessed Documents for progress: {e}"
                    )
                if total_docs > 0:
                    doc_tracker.set_total(total_docs)
                    logger.info(
                        f"Processing-documents denominator: {total_docs} unprocessed Document(s)"
                    )
                async with asyncio.TaskGroup() as inner:
                    inner.create_task(
                        stream_docs(conn, docs_chan, 100, progress=progress)
                    )
                    inner.create_task(
                        chunk_docs(
                            conn,
                            docs_chan,
                            embed_chan,
                            upsert_chan,
                            extract_chan,
                            progress=progress,
                            total_docs=total_docs,
                            tracker=doc_tracker,
                        )
                    )
            grp.create_task(new_doc_pipeline())

            grp.create_task(upsert(upsert_chan))
            grp.create_task(load(conn))
            grp.create_task(embed(embed_chan, embedding_store, graphname))
            grp.create_task(
                extract(extract_chan, upsert_chan, embed_chan, extractor, conn, num_chunk_senders, tracker=doc_tracker)
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
        _report("Detecting communities", clear_progress=True)
        await install_queries(COMMUNITY_QUERIES, conn)
        logger.info("Community Processing Start")

        # Clear pre-existing communities so re-detection is idempotent.
        try:
            async with tg_sem:
                res = await conn.runInstalledQuery(
                    "graphrag_delete_all_communities"
                )
            deleted = (res[0] if res else {}).get("deleted", 0)
            if deleted:
                logger.info(f"Cleared {deleted} pre-existing Community vertex(es)")
        except Exception as e:
            logger.warning(f"graphrag_delete_all_communities failed: {e}")

        comm_process_chan = Channel()
        upsert_chan = Channel()
        embed_chan = Channel()
        load_q.reopen()
        # Breaker is created here (not inside summarize_communities) so its
        # incomplete count can bubble up into the rebuild's completion summary.
        comm_breaker = workers.CommunitySummaryBreaker()
        async with asyncio.TaskGroup() as grp:
            # run louvain
            # get the communities
            grp.create_task(communities(conn, comm_process_chan, report=_report))
            # summarize each community
            grp.create_task(
                summarize_communities(conn, comm_process_chan, upsert_chan, embed_chan, comm_breaker)
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

        # Record community summaries left incomplete (LLM provider errors) so
        # the rebuild can end with a partial-success warning instead of a flat
        # green success.
        if comm_breaker.incomplete:
            run_summary["communities_incomplete"] = comm_breaker.incomplete
            run_summary["warnings"].append(
                f"{comm_breaker.incomplete} community "
                f"{'summary' if comm_breaker.incomplete == 1 else 'summaries'} "
                f"could not be generated (language model unavailable); "
                f"regenerate them from the graph admin page."
            )

        # Mirror Entity → Community memberships onto domain-VT instances
        # that share the same id.
        try:
            existing_schema = await read_existing_schema_async(conn)
            domain_vts = sorted(
                v for v in existing_schema.vertex_types if not is_structural_type(v)
            )
        except Exception as e:
            logger.warning(f"read live schema for community mirror failed: {e}")
            domain_vts = []
        if domain_vts:
            mirrorable = [
                vt for vt in domain_vts
                if existing_schema.has_edge_pair("IN_COMMUNITY", vt, "Community")
            ]
            skipped = sorted(set(domain_vts) - set(mirrorable))
            if skipped:
                logger.warning(
                    f"skipping community mirror for {skipped}: "
                    f"IN_COMMUNITY pair missing on schema"
                )
            if mirrorable:
                _report("Updating domain types", clear_progress=True)
                await graphrag_mirror_communities(conn, mirrorable)
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

    return run_summary
