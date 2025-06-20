import os

os.environ["ECC"] = "true"
import json
import time
import logging
from contextlib import asynccontextmanager
from threading import Thread
from typing import Annotated, Callable

import asyncio
import graphrag
import supportai
from eventual_consistency_checker import EventualConsistencyChecker
from fastapi import BackgroundTasks, Depends, FastAPI, Response, status
from fastapi.security.http import HTTPBase

from common.config import (
    db_config,
    graphrag_config,
    embedding_service,
    get_llm_service,
    llm_config,
    security,
)
from common.db.connections import elevate_db_connection_to_token
from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.logs.logwriter import LogWriter
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.py_schemas.schemas import SupportAIMethod

logger = logging.getLogger(__name__)
consistency_checkers = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not graphrag_config.get("enable_consistency_checker", False):
        LogWriter.info("Eventual Consistency Checker not run on startup")

    else:
        startup_checkers = graphrag_config.get("graph_names", [])
        for graphname in startup_checkers:
            conn = elevate_db_connection_to_token(
                db_config.get("hostname"),
                db_config.get("username"),
                db_config.get("password"),
                graphname,
                async_conn=True
            )
            start_ecc_in_thread(graphname, conn)
    yield
    LogWriter.info("ECC Shutdown")


app = FastAPI(lifespan=lifespan)


def start_ecc_in_thread(graphname: str, conn: TigerGraphConnectionProxy):
    thread = Thread(
        target=initialize_eventual_consistency_checker,
        args=(graphname, conn),
        daemon=True,
    )
    thread.start()
    LogWriter.info(f"Eventual consistency checker started for graph {graphname}")


def initialize_eventual_consistency_checker(
    graphname: str, conn: TigerGraphConnectionProxy
):
    if graphname in consistency_checkers:
        return consistency_checkers[graphname]

    try:
        maj, minor, patch = conn.getVer().split(".")
        if  maj >= "4" and minor >= "2":
            # TigerGraph native vector support
            index_stores = {}
            index_stores["tigergraph"] = TigerGraphEmbeddingStore(
                conn,
                embedding_service,
                support_ai_instance=False,
            )

        if graphrag_config.get("extractor") == "llm":
            from common.extractors import LLMEntityRelationshipExtractor

            extractor = LLMEntityRelationshipExtractor(get_llm_service(llm_config))
        else:
            raise ValueError("Invalid extractor type")

        checker = EventualConsistencyChecker(
            process_interval_seconds,
            cleanup_interval_seconds,
            graphname,
            embedding_service,
            index_names,
            index_stores,
            conn,
            extractor,
            batch_size,
        )
        consistency_checkers[graphname] = checker

        # start the main ECC process that searches for new vertices that need to be processed
        checker.initialize()

        return checker
    except Exception as e:
        LogWriter.error(
            f"Failed to start eventual consistency checker for graph {graphname}: {e}"
        )


def start_func_in_thread(f: Callable, *args, **kwargs):
    thread = Thread(
        target=f,
        args=args,
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()
    LogWriter.info(f'Thread started for function: "{f.__name__}"')


@app.get("/")
def root():
    LogWriter.info(f"Healthcheck")
    return {"status": "ok"}


@app.get("/{graphname}/{ecc_method}/consistency_status")
def consistency_status(
    graphname: str,
    ecc_method: str,
    background: BackgroundTasks,
    credentials: Annotated[HTTPBase, Depends(security)],
    response: Response,
):
    conn = elevate_db_connection_to_token(
        db_config.get("hostname"),
        credentials.username,
        credentials.password,
        graphname,
        async_conn=True
    )

    asyncio.run(conn.customizeHeader(
        timeout=db_config.get("default_timeout", 300) * 1000, responseSize=5000000
    ))

    logger.info(f"Connection timeout set is {conn.responseConfigHeader}")
    
    match ecc_method:
        case SupportAIMethod.SUPPORTAI:
            background.add_task(supportai.run, graphname, conn)

            ecc_status = f"SupportAI initialization on {graphname} {time.ctime()}"       
        case SupportAIMethod.GRAPHRAG:
            background.add_task(graphrag.run, graphname, conn)

            ecc_status = f"GraphRAG initialization on {conn.graphname} {time.ctime()}"
        case _:
            response.status_code = status.HTTP_404_NOT_FOUND
            return f"Method unsupported, must be {SupportAIMethod.SUPPORTAI}, {SupportAIMethod.GRAPHRAG}"

    return ecc_status
