# Copyright (c) 2025 TigerGraph, Inc.
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
import logging
import re
import traceback
from glob import glob

import httpx
from graphrag import reusable_channel, workers
from pyTigerGraph import AsyncTigerGraphConnection

from common.config import (
    graphrag_config,
    embedding_service,
    get_llm_service,
    llm_config,
)
from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.extractors import GraphExtractor, LLMEntityRelationshipExtractor
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)

http_timeout = httpx.Timeout(15.0)

tg_sem = asyncio.Semaphore(2)
load_q = reusable_channel.ReuseableChannel()

# will pause workers until the event is false
loading_event = asyncio.Event()
loading_event.set() # set the event to true to allow the workers to run

async def install_queries(
    requried_queries: list[str],
    conn: AsyncTigerGraphConnection,
):
    # queries that are currently installed
    installed_queries = [q.split("/")[-1] for q in await conn.getEndpoints(dynamic=True) if f"/{conn.graphname}/" in q]

    # doesn't need to be parallel since tg only does it one at a time
    for q in requried_queries:
        # only install n queries at a time (n=n_workers)
        q_name = q.split("/")[-1]
        # if the query is not installed, install it
        if q_name not in installed_queries:
            res = await workers.install_query(conn, q, False)
            # stop system if a required query doesn't install
            if res["error"]:
                raise Exception(res["message"])
            logger.info(f"Successfully created query '{q_name}'.")
    query = f"""\
USE GRAPH {conn.graphname}
INSTALL QUERY ALL
"""
    async with tg_sem:
        res = await conn.gsql(query)
        if "error" in res:
            raise Exception(res)

    logger.info("Finished processing all required queries.")


async def init(
    conn: AsyncTigerGraphConnection,
) -> tuple[BaseExtractor, dict[str, EmbeddingStore]]:
    # install requried queries
    requried_queries = [
        "common/gsql/graphrag/StreamIds",
        "common/gsql/graphrag/StreamDocContent",
        "common/gsql/graphrag/SetEpochProcessing",
        "common/gsql/graphrag/ResolveRelationships",
        "common/gsql/graphrag/get_community_children",
        "common/gsql/graphrag/entities_have_resolution",
        "common/gsql/graphrag/communities_have_desc",
        "common/gsql/graphrag/get_vertices_or_remove",
        "common/gsql/graphrag/louvain/graphrag_louvain_init",
        "common/gsql/graphrag/louvain/graphrag_louvain_communities",
        "common/gsql/graphrag/louvain/modularity",
        "common/gsql/graphrag/louvain/stream_community",
        "common/gsql/supportai/create_entity_type_relationships"
    ]
    # add louvain to queries
    q = [x.split(".gsql")[0] for x in glob("common/gsql/graphrag/louvain/*")]
    requried_queries.extend(q)
    logger.info(f"Installing queries needed for GraphRAG all together")
    await install_queries(requried_queries, conn)

    # extractor
    if graphrag_config.get("extractor") == "graphrag":
        extractor = GraphExtractor()
    elif graphrag_config.get("extractor") == "llm":
        extractor = LLMEntityRelationshipExtractor(get_llm_service(llm_config))
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
    v_id = v_id.replace(" ", "_").replace("/", "").replace("%", "percent").lower()

    has_func = re.compile(r"(.*)\(").findall(v_id)
    if len(has_func) > 0:
        v_id = has_func[0]
    if v_id == "''" or v_id == '""':
        return ""
    v_id = v_id.replace("(", "").replace(")", "")

    return v_id


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
            res = await conn.getVerticesById("Entity", v_id)

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


async def check_all_ents_resolved(conn):
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "entities_have_resolution"
            )
    except Exception as e:
        logger.error(f"Check Vert Desc err:\n{e}")

    res = resp[0]["all_resolved"]
    logger.info(resp)

    return res

async def add_rels_between_types(conn):
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "create_entity_type_relationships"
            )
    except Exception as e:
        logger.error(f"Check Vert EntityType err:\n{e}")
        return {"error": True, "message": e}        
    return resp[0]

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
