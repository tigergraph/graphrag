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
import base64
import json
import logging
import re
import traceback

import httpx
from graphrag import reusable_channel, workers
from pyTigerGraph import AsyncTigerGraphConnection

from common.config import (
    graphrag_config,
    embedding_service,
    get_llm_service,
    get_completion_config,
    get_graphrag_config,
)
from common.db.schema_utils import (
    is_structural_type,
    read_existing_schema_async,
    read_type_metadata_async,
)
from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.extractors import GraphExtractor, LLMEntityRelationshipExtractor
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)

http_timeout = httpx.Timeout(15.0)

_default_concurrency = graphrag_config.get("default_concurrency", 10)
# Worker amplifier: processing workers (chunk, embed, extract, community) run at 2x
# the base concurrency since each worker is mostly waiting on I/O (LLM/embedding API calls).
_worker_concurrency = _default_concurrency * 2
tg_sem = asyncio.Semaphore(_default_concurrency)

COMMUNITY_QUERIES = [
    "common/gsql/graphrag/louvain/graphrag_louvain_init",
    "common/gsql/graphrag/louvain/graphrag_louvain_communities",
    "common/gsql/graphrag/louvain/modularity",
    "common/gsql/graphrag/louvain/stream_community",
    "common/gsql/graphrag/get_community_children",
    "common/gsql/graphrag/communities_have_desc",
    "common/gsql/graphrag/graphrag_delete_all_communities",
    "common/gsql/graphrag/graphrag_stream_entity_community_pairs",
    "common/gsql/graphrag/graphrag_stream_all_ids",
]

REQUIRED_QUERIES = [
    "common/gsql/graphrag/StreamIds",
    "common/gsql/graphrag/StreamDocContent",
    "common/gsql/graphrag/StreamChunkContent",
    "common/gsql/graphrag/SetEpochProcessing",
    "common/gsql/graphrag/get_vertices_or_remove",
]
load_q = reusable_channel.ReuseableChannel()

# will pause workers until the event is false
loading_event = asyncio.Event()
loading_event.set() # set the event to true to allow the workers to run

async def install_queries(
    requried_queries: list[str],
    conn: AsyncTigerGraphConnection,
):
    installed_queries = [q.split("/")[-1] for q in await conn.getEndpoints(dynamic=True) if f"/{conn.graphname}/" in q]

    required_names = set()
    for q in requried_queries:
        q_name = q.split("/")[-1]
        required_names.add(q_name)
        if q_name not in installed_queries:
            res = await workers.install_query(conn, q, False)
            if res["error"]:
                raise Exception(res["message"])
            logger.info(f"Successfully created query '{q_name}'.")

    if required_names.issubset(set(installed_queries)):
        logger.info("All required queries already installed, skipping INSTALL QUERY ALL.")
        return

    logger.info("Submitting INSTALL QUERY ALL ...")
    query = f"USE GRAPH {conn.graphname}\nINSTALL QUERY ALL\n"
    async with tg_sem:
        res = await conn.gsql(query)
        logger.info(f"INSTALL QUERY ALL returned: {str(res)[:200]}")
        res_lower = res.lower() if isinstance(res, str) else ""
        if "error" in res_lower or "does not exist" in res_lower or "failed" in res_lower:
            raise Exception(res)

    max_wait = 600  # seconds
    poll_interval = 10
    elapsed = 0
    while elapsed < max_wait:
        ready = [
            q.split("/")[-1]
            for q in await conn.getEndpoints(dynamic=True)
            if f"/{conn.graphname}/" in q
        ]
        missing = required_names - set(ready)
        if not missing:
            break
        logger.info(
            f"Waiting for query installation to finish "
            f"({len(missing)} remaining: {', '.join(sorted(missing))})"
        )
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    else:
        raise Exception(
            f"Query installation timed out after {max_wait}s. "
            f"Still missing: {', '.join(sorted(missing))}"
        )

    logger.info("All required queries installed and verified.")


async def init(
    conn: AsyncTigerGraphConnection,
) -> tuple[BaseExtractor, dict[str, EmbeddingStore]]:
    """Initialize extractors and embedding store.

    Returns:
        (extractor, embedding_store)
    """
    # install required queries
    logger.info("Installing queries needed for GraphRAG all together")
    await install_queries(REQUIRED_QUERIES, conn)

    # extractor
    graph_cfg = get_graphrag_config(conn.graphname)
    if graph_cfg.get("extractor") == "graphrag":
        extractor = GraphExtractor()
    elif graph_cfg.get("extractor") == "llm":
        # Read the live schema directly (without going through the
        # proposal-flow SchemaProposal type). This intentionally
        # supports graphs whose domain types were created outside of
        # the proposal flow — admin UI, prior releases,
        # external migration scripts — as long as the domain types
        # and the EntityType / RelationshipType metadata are on the
        # graph, ECC will use them.
        try:
            existing = await read_existing_schema_async(conn)
        except Exception as exc:
            logger.warning(f"Loading live schema for extractor failed: {exc}")
            from common.db.schema_utils import ExistingSchema
            existing = ExistingSchema()
        try:
            entity_descs, rel_defs = await read_type_metadata_async(conn)
        except Exception as exc:
            logger.warning(f"Loading type metadata for extractor failed: {exc}")
            entity_descs, rel_defs = {}, {}

        # Filter to domain types (drop GraphRAG structural types and
        # any pair whose endpoint touches a structural vertex).
        domain_vertex_types = sorted(
            v for v in existing.vertex_types if not is_structural_type(v)
        )
        domain_edge_endpoints: dict = {}
        for edge_name, pairs in existing.edge_pairs.items():
            if is_structural_type(edge_name):
                continue
            domain_pairs = [
                (s, t)
                for s, t in pairs
                if not is_structural_type(s) and not is_structural_type(t)
            ]
            if domain_pairs:
                domain_edge_endpoints[edge_name] = domain_pairs
        domain_edge_types = sorted(domain_edge_endpoints.keys())

        # Trim the descriptions to domain types only.
        domain_entity_defs = {
            vt: entity_descs[vt]
            for vt in domain_vertex_types
            if entity_descs.get(vt)
        }
        domain_rel_defs = {
            et: rel_defs[et]
            for et in domain_edge_types
            if rel_defs.get(et)
        }

        # Strict-mode comes from graphrag_config; default false (legacy
        # fallback to plain Entity vertices for non-domain extractions).
        strict_mode = bool(graph_cfg.get("strict_mode", False))

        extractor = LLMEntityRelationshipExtractor(
            get_llm_service(get_completion_config(conn.graphname)),
            allowed_entity_types=domain_vertex_types or None,
            allowed_relationship_types=domain_edge_types or None,
            strict_mode=strict_mode,
            entity_type_definitions=domain_entity_defs,
            relationship_type_definitions=domain_rel_defs,
            domain_edge_endpoints=domain_edge_endpoints,
        )
    else:
        raise ValueError("Invalid extractor type")

    embedding_store = TigerGraphEmbeddingStore(
        conn,
        embedding_service,
        support_ai_instance=True,
    )
    embedding_store.set_graphname(conn.graphname)

    return extractor, embedding_store


def make_headers(conn: AsyncTigerGraphConnection):
    if conn.apiToken is None or conn.apiToken == "":
        tkn = base64.b64encode(f"{conn.username}:{conn.password}".encode()).decode()
        headers = {"Authorization": f"Basic {tkn}"}
    else:
        headers = {"Authorization": f"Bearer {conn.apiToken}"}

    return headers


async def stream_ids(
    conn: AsyncTigerGraphConnection, v_type: str, current_batch: int, ttl_batches: int
) -> dict[str, str | list[str]]:
    try:
        async with tg_sem:
            res = await conn.runInstalledQuery(
                "StreamIds",
                params={
                    "current_batch": current_batch,
                    "ttl_batches": ttl_batches,
                    "v_type": v_type,
                }
            )
        ids = res[0]["@@ids"]
        logger.debug(f"Fetched ids: {ids}")
        return {"error": False, "ids": ids}

    except Exception as e:
        exc = traceback.format_exc()
        LogWriter.error(f"/{conn.graphname}/query/StreamIds\nException Trace:\n{exc}")

        return {"error": True, "message": str(e)}


def map_attrs(attributes: dict):
    # map attrs
    attrs = {}
    for k, v in attributes.items():
        if isinstance(v, tuple):
            attrs[k] = {"value": v[0], "op": v[1]}
        elif isinstance(v, dict):
            attrs[k] = {
                "value": {"keylist": list(v.keys()), "valuelist": list(v.values())}
            }
        else:
            attrs[k] = {"value": v}
    return attrs


def process_id(v_id: str):
    has_func = re.compile(r"(.*)\(").findall(v_id)
    if len(has_func) > 0:
        v_id = has_func[0]
    v_id = v_id.replace(" ", "-").lower().replace("/", "_").replace("(", "").replace(")", "")
    if v_id == "''" or v_id == '""':
        return ""

    return v_id


# Suffixes the LLM commonly tacks onto type labels without adding
# semantic distinction. Stripped during meta-layer normalization so
# ``Company_Type``, ``Company_Class``, ``Company_Entity`` collapse onto
# the same canonical name.
_TYPE_SUFFIXES = ("_type", "_class", "_entity", "_data", "_info", "_record")


def normalize_type_name(name: str) -> str:
    """Normalize an LLM-emitted vertex / edge type label so trivial
    variants collapse onto a single canonical id.

    Applies in order:

    1. ``process_id`` (lowercase, whitespace → ``-``, strip parens).
    2. Strip a single trailing semantic-suffix from
       :data:`_TYPE_SUFFIXES` (e.g. ``company_type`` → ``company``).
    3. Singularize trailing ``ies`` → ``y`` (``companies`` →
       ``company``); strip a single trailing ``s`` only when the
       preceding char is a consonant other than ``s``, ``i``, or ``u``
       (``reports`` → ``report``; preserves ``series``, ``status``,
       ``news``, ``business``).

    Used only for the EntityType / RelationshipType meta-layer in
    Case 1 (no domain types declared) — instance ids stay
    untouched. Synonym consolidation (``Company`` vs ``Corporation``)
    is out of scope for this deterministic pass.
    """
    base = process_id(name)
    if not base:
        return ""
    for suffix in _TYPE_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    # Singularize defensively. Length thresholds keep short words
    # whose final ``s`` / ``ies`` is part of the singular stem
    # (``News``, ``Series``, ``Bus``, ``Status``, ``Yes``).
    if base.endswith("ies") and len(base) > 6:
        base = base[:-3] + "y"
    elif (
        base.endswith("s")
        and len(base) > 4
        and base[-2] not in "siu"
        and not base[-2].isdigit()
    ):
        base = base[:-1]
    return base


async def upsert_vertex(
    conn: AsyncTigerGraphConnection,
    vertex_type: str,
    vertex_id: str,
    attributes: dict,
):
    logger.debug(f"Upsert vertex: {vertex_id} as {vertex_type}")
    vertex_id = vertex_id.replace(" ", "_")
    attrs = map_attrs(attributes)
    await load_q.put(("vertices", (vertex_type, vertex_id, attrs)))


async def upsert_batch(conn: AsyncTigerGraphConnection, data: str):
    async with tg_sem:
        try:
            res = await conn.upsertData(data)
            logger.info(f"Upsert res: {res}")
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"Upsert err with {data}:\n{err}")
            return {"error": True, "message": str(e)}


async def check_vertex_exists(conn, v_id: str):
    async with tg_sem:
        try:
            from urllib.parse import quote
            url = (conn.restppUrl + "/graph/" + conn.graphname
                   + "/vertices/Entity/" + quote(v_id, safe=""))
            res = await conn._req("GET", url, params={"select": "description"})

        except Exception as e:
            if "is not a valid vertex id" not in str(e):
                err = traceback.format_exc()
                logger.error(f"Check err:\n{err}")
            return {"error": True, "message": str(e)}

        return {"error": False, "resp": res}


async def upsert_edge(
    conn: AsyncTigerGraphConnection,
    src_v_type: str,
    src_v_id: str,
    edge_type: str,
    tgt_v_type: str,
    tgt_v_id: str,
    attributes: dict = None,
):
    if attributes is None:
        attrs = {}
    else:
        attrs = map_attrs(attributes)
    logger.debug(f"Upsert edge: {src_v_id} -[{edge_type}]-> {tgt_v_id}")
    src_v_id = src_v_id.replace(" ", "_")
    tgt_v_id = tgt_v_id.replace(" ", "_")
    await load_q.put(
        (
            "edges",
            (
                src_v_type,
                src_v_id,
                edge_type,
                tgt_v_type,
                tgt_v_id,
                attrs,
            ),
        )
    )


async def get_commuinty_children(conn, i: int, c: str):
    async with tg_sem:
        try:
            resp = await conn.runInstalledQuery(
                "get_community_children",
                params={"comm": c, "iter": i}
            )
        except:
            logger.error(f"Get Children err:\n{traceback.format_exc()}")

    descrs = []
    try:
        res = resp[0]["children"]
    except Exception as e:
        logger.error(f"Get Children err:\n{e}")
        res = []
    for d in res:
        desc = d["attributes"]["description"]
        # if it's the entity iteration
        if i == 1:
            # filter out empty strings
            desc = list(filter(lambda x: len(x) > 0, desc))
            # if there are no descriptions, make it the v_id
            if len(desc) == 0:
                desc.append(d["v_id"])
            descrs.extend(desc)
        else:
            descrs.append(desc)

    return descrs


async def check_vertex_has_desc(conn, i: int):
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "communities_have_desc",
                params={"iter": i},
            )
    except Exception as e:
        logger.error(f"Check Vert Desc err:\n{e}")

    res = resp[0]["all_have_desc"]
    logger.info(res)

    return res

async def check_embedding_rebuilt(conn, v_type: str):
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "vertices_have_embedding",
                params={
                    "vertex_type": v_type,
                }
            )
    except Exception as e:
        logger.error(f"Check embedding rebuilt err:\n{e}")

    res = resp[0]["all_have_embedding"]
    logger.info(resp)

    return res


async def graphrag_mirror_communities(
    conn: AsyncTigerGraphConnection,
    domain_vts: list[str],
) -> int:
    """Mirror Entity → Community memberships onto domain-VT instances
    that share the same id. Returns the number of mirror edges written.
    """
    if not domain_vts:
        return 0

    async with tg_sem:
        try:
            res = await conn.runInstalledQuery(
                "graphrag_stream_entity_community_pairs",
                params={},
                sizeLimit=1000000000,
            )
        except Exception as e:
            logger.error(f"stream entity-community pairs failed: {e}")
            return 0

    pairs = (res[0] if res else {}).get("pairs", []) or []
    if not pairs:
        return 0

    valid_ids_by_vt: dict[str, set[str]] = {}
    for vt in domain_vts:
        try:
            async with tg_sem:
                r = await conn.runInstalledQuery(
                    "graphrag_stream_all_ids",
                    params={"v_type": vt},
                    sizeLimit=1000000000,
                )
        except Exception as e:
            logger.warning(f"stream_all_ids({vt}) failed: {e}")
            valid_ids_by_vt[vt] = set()
            continue
        ids = set((r[0] if r else {}).get("@@ids", []) or [])
        valid_ids_by_vt[vt] = ids

    written = 0
    chunk_size = 5000
    for vt, valid_ids in valid_ids_by_vt.items():
        if not valid_ids:
            continue
        edges = [
            (p["entity_id"], p["community_id"])
            for p in pairs
            if isinstance(p, dict)
            and p.get("entity_id") in valid_ids
            and p.get("community_id")
        ]
        if not edges:
            continue
        for i in range(0, len(edges), chunk_size):
            chunk = edges[i:i + chunk_size]
            async with tg_sem:
                try:
                    await conn.upsertEdges(
                        sourceVertexType=vt,
                        edgeType="IN_COMMUNITY",
                        targetVertexType="Community",
                        edges=chunk,
                    )
                    written += len(chunk)
                except Exception as e:
                    logger.error(
                        f"upsertEdges IN_COMMUNITY for {vt} (chunk size "
                        f"{len(chunk)}) failed: {e}"
                    )

    logger.info(
        f"graphrag_mirror_communities: wrote {written} mirror "
        f"IN_COMMUNITY edges across {len(domain_vts)} domain VT(s)"
    )
    return written
