import os

os.environ["ECC"] = "true"
import json
import time
import logging
from contextlib import asynccontextmanager
from threading import Thread
from typing import Callable

import asyncio
import graphrag
import supportai
from eventual_consistency_checker import EventualConsistencyChecker
from fastapi import BackgroundTasks, Depends, FastAPI, Request, Response, status, HTTPException
from fastapi.security.http import HTTPBasicCredentials, HTTPAuthorizationCredentials
from base64 import b64decode

from common.config import (
    db_config,
    graphrag_config,
    embedding_service,
    get_llm_service,
    llm_config,
)
from common.db.connections import elevate_db_connection_to_token, get_db_connection_id_token
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
            embedding_store = TigerGraphEmbeddingStore(
                conn,
                embedding_service,
                support_ai_instance=False,
            )
        index_names = graphrag_config.get(
            "indexes",
            ["Document", "DocumentChunk", "Entity", "Relationship", "Concept"],
        )

        if graphrag_config.get("extractor") == "llm":
            from common.extractors import LLMEntityRelationshipExtractor

            extractor = LLMEntityRelationshipExtractor(get_llm_service(llm_config))
        else:
            raise ValueError("Invalid extractor type")

        checker = EventualConsistencyChecker(
            graphrag_config.get("process_interval_seconds", 300),
            graphrag_config.get("cleanup_interval_seconds", 300),
            graphname,
            embedding_service,
            embedding_store,
            index_names,
            conn,
            extractor,
            graphrag_config.get("batch_size", 100),
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

def auth_credentials(
    request: Request,
):
    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")

    scheme, credentials = auth.split(" ")
    if scheme == "Bearer":
        credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)
        return credentials

    elif scheme == "Basic":
        username, password = b64decode(credentials).decode().split(":")
        credentials = HTTPBasicCredentials(username=username, password=password)
        return credentials
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unsupported auth scheme")


@app.get("/")
def root():
    LogWriter.info(f"Healthcheck")
    return {"status": "ok"}


@app.get("/{graphname}/{ecc_method}/consistency_status")
def consistency_status(
    graphname: str,
    ecc_method: str,
    background: BackgroundTasks,
    response: Response,
    credentials = Depends(auth_credentials),
):
    if isinstance(credentials, HTTPBasicCredentials):
        conn = elevate_db_connection_to_token(
            db_config.get("hostname"),
            credentials.username,
            credentials.password,
            graphname,
            async_conn=True
        )
    elif isinstance(credentials, HTTPAuthorizationCredentials):
        conn = get_db_connection_id_token(
            graphname,
            credentials.credentials,
            async_conn=True
        )
    else:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")

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
