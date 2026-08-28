import asyncio
import base64
import json
import logging
import traceback
from glob import glob
from typing import Callable

import httpx
from supportai import workers
from pyTigerGraph import TigerGraphConnection

from common.config import (
    get_embedding_service,
    graphrag_config,
    get_llm_service,
    get_completion_config,
    get_graphrag_config,
)
from common.db.schema_utils import gsql_output_error
from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.extractors import GraphExtractor, LLMEntityRelationshipExtractor
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)
http_timeout = httpx.Timeout(15.0)

_default_concurrency = graphrag_config.get("default_concurrency", 10)
tg_sem = asyncio.Semaphore(_default_concurrency * 2)

async def install_queries(
    requried_queries: list[str],
    conn: TigerGraphConnection,
):
    installed_queries = [q.split("/")[-1] for q in await conn.getEndpoints(dynamic=True) if f"/{conn.graphname}/" in q]

    required_names = set()
    for q in requried_queries:
        q_name = q.split("/")[-1]
        required_names.add(q_name)
        if q_name not in installed_queries:
            logger.info(f"Query '{q_name}' not found in installed queries. Attempting to create...")
            try:
                res = await workers.install_query(conn, q, False)
                if res["error"]:
                    logger.error(f"Failed to create query '{q_name}'. Error: {res['message']}")
                    raise Exception(f"Creation of query '{q_name}' failed with message: {res['message']}")
                else:
                    logger.info(f"Successfully created query '{q_name}'.")
            except Exception as e:
                logger.critical(f"Critical error during creation of query '{q_name}': {e}")
                raise e
        else:
            logger.info(f"Query '{q_name}' is already installed.")

    if required_names.issubset(set(installed_queries)):
        logger.info("All required queries already installed, skipping INSTALL QUERY ALL.")
        return

    logger.info("Submitting INSTALL QUERY ALL ...")
    query = f"USE GRAPH {conn.graphname}\nINSTALL QUERY ALL\n"
    async with tg_sem:
        res = await conn.gsql(query)
        logger.info(f"INSTALL QUERY ALL returned: {str(res)[:200]}")
        err = gsql_output_error(res) if isinstance(res, str) else None
        if err:
            raise Exception(res)

    max_wait = 300  # seconds
    poll_interval = 5
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
    conn: TigerGraphConnection,
) -> tuple[BaseExtractor, dict[str, EmbeddingStore]]:
    # install requried queries
    requried_queries = [
        "common/gsql/supportai/Scan_For_Updates",
        "common/gsql/supportai/Update_Vertices_Processing_Status",
        "common/gsql/supportai/ECC_Status",
        "common/gsql/supportai/Check_Nonexistent_Vertices",
        "common/gsql/graphRAG/StreamIds",
        "common/gsql/graphRAG/StreamDocContent"
        "common/gsql/graphRAG/StreamChunkContent"
    ]
    await install_queries(requried_queries, conn)

    # extractor
    graph_cfg = get_graphrag_config(conn.graphname)
    if graph_cfg.get("extractor") == "graphrag":
        extractor = GraphExtractor()
    elif graph_cfg.get("extractor") == "llm":
        extractor = LLMEntityRelationshipExtractor(get_llm_service(get_completion_config()))
    else:
        raise ValueError("Invalid extractor type")

    embedding_store = TigerGraphEmbeddingStore(
        conn,
        get_embedding_service(),
        support_ai_instance=True,
    )
    embedding_store.set_graphname(conn.graphname)

    return extractor, embedding_store


def make_headers(conn: TigerGraphConnection):
    if conn.apiToken is None or conn.apiToken == "":
        tkn = base64.b64encode(f"{conn.username}:{conn.password}".encode()).decode()
        headers = {"Authorization": f"Basic {tkn}"}
    else:
        headers = {"Authorization": f"Bearer {conn.apiToken}"}

    return headers


async def stream_ids(
    conn: TigerGraphConnection, v_type: str, current_batch: int, ttl_batches: int
) -> dict[str, str | list[str]]:
    headers = make_headers(conn)

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
    # Strip parentheses in place — do NOT truncate at "(". Truncating dropped
    # the "_chunk_{i}" suffix for ids containing parentheses, corrupting chunk
    # ids. The replace() below removes the parens without dropping the rest.
    # (GML-2173)
    v_id = v_id.replace(" ", "_").lower().replace("/", "_").replace("(", "").replace(")", "")
    if v_id == "''" or v_id == '""':
        return ""

    return v_id


async def upsert_vertex(
    conn: TigerGraphConnection,
    vertex_type: str,
    vertex_id: str,
    attributes: dict,
):
    logger.info(f"Upsert vertex: {vertex_type} {vertex_id}")
    attrs = map_attrs(attributes)
    data = json.dumps({"vertices": {vertex_type: {vertex_id: attrs}}})
    headers = make_headers(conn)
    async with tg_sem:
        try:
            res = await conn.upsertData(data)

            logger.info(f"Upsert res: {res}")
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"Upsert err:\n{err}")
            return {"error": True, "message": str(e)}


async def check_vertex_exists(conn, v_id: str):
    async with tg_sem:
        try:
            from urllib.parse import quote
            url = (conn.restppUrl + "/graph/" + conn.graphname
                   + "/vertices/Entity/" + quote(v_id, safe=""))
            res = await conn._req("GET", url, params={"select": "description"})

        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"Check err:\n{err}")
            return {"error": True, "message": str(e)}

        return {"error": False, "resp": res}



async def upsert_edge(
    conn: TigerGraphConnection,
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
    data = json.dumps(
        {
            "edges": {
                src_v_type: {
                    src_v_id: {
                        edge_type: {
                            tgt_v_type: {
                                tgt_v_id: attrs,
                            }
                        }
                    },
                }
            }
        }
    )
    async with tg_sem:
        try:
            res = await conn.upsertData(data)

            logger.info(f"Upsert res: {res}")
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"Upsert err:\n{err}")
            return {"error": True, "message": str(e)}
