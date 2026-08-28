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

import json
import logging
import time
import uuid
from base64 import b64decode
from datetime import datetime

import routers
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from starlette.middleware.cors import CORSMiddleware

from common.config import PATH_PREFIX, PRODUCTION
from common.db.connections import (get_db_connection_id_token,
                                   get_db_connection_pwd)
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics as pmetrics

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    # Install source tarballs referenced by configured stdio MCP servers
    # (global + per-graph) each boot, so their console-script commands persist
    # across container recreation.
    try:
        from common.mcp_config import install_configured_libraries
        install_configured_libraries()
    except Exception as e:
        logging.getLogger(__name__).warning(f"mcp library install failed: {e}")

    # Agentic mode is on by default and confirmed by a runtime probe on first
    # use. Only warn when the configured chat model is a known-legacy model we
    # are sure can't tool-call, so Agentic will run as the classic engine.
    try:
        from common.config import get_chat_config
        from common.llm_services.capabilities import _known_no_tool_calling
        cfg = get_chat_config()
        if _known_no_tool_calling(cfg):
            logging.getLogger(__name__).warning(
                "Chat model llm_service=%r llm_model=%r is a legacy model without "
                "tool-calling; Agentic mode is unavailable and requests will use "
                "the classic engine. Configure a newer model to enable Agentic mode.",
                (cfg or {}).get("llm_service"), (cfg or {}).get("llm_model"),
            )
    except Exception as e:
        logging.getLogger(__name__).warning(f"agentic capability check skipped: {e}")

    yield

    # --- shutdown ---
    # Close every cached external-MCP client and stop the dedicated event loop
    # so stdio subprocesses don't outlive the worker.
    try:
        from mcp_addons import shutdown_all, stop_loop, run_async
        await run_async(shutdown_all())
        stop_loop()
    except Exception as e:
        logging.getLogger(__name__).warning(f"mcp_addons shutdown failed: {e}")


if PRODUCTION:
    app = FastAPI(
        title="TigerGraph GraphRAG", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
    )
else:
    app = FastAPI(title="TigerGraph GraphRAG", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routers.root_router, prefix=PATH_PREFIX)
app.include_router(routers.inquiryai_router, prefix=PATH_PREFIX)
app.include_router(routers.supportai_router, prefix=PATH_PREFIX)
app.include_router(routers.queryai_router, prefix=PATH_PREFIX)
app.include_router(routers.ui_router, prefix=PATH_PREFIX)
app.include_router(routers.mcp_servers_router, prefix=PATH_PREFIX)


excluded_metrics_paths = ("/docs", "/openapi.json", "/metrics")

logger = logging.getLogger(__name__)

logger.info("In main.py")


async def get_basic_auth_credentials(request: Request):
    auth_header = request.headers.get("Authorization")

    if auth_header is None:
        return ""

    try:
        auth_type, encoded_credentials = auth_header.split(" ", 1)
    except ValueError:
        return ""

    if auth_type.lower() != "basic":
        return ""

    try:
        decoded_credentials = b64decode(encoded_credentials).decode("utf-8")
        username, _ = decoded_credentials.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return username


@app.middleware("http")
async def log_requests(request: Request, call_next):
    req_id = str(uuid.uuid4())
    LogWriter.info(f"{request.url.path} ENTRY request_id={req_id}")
    req_id_cv.set(req_id)
    start_time = time.time()
    response = await call_next(request)

    user_name = await get_basic_auth_credentials(request)
    client_host = request.client.host
    user_agent = request.headers.get("user-agent", "Unknown")
    action_name = request.url.path
    status = "SUCCESS"

    if response.status_code != 200:
        status = "FAILURE"

    # set up the audit log entry structure and write it with the LogWriter
    if not any(request.url.path.endswith(path) for path in excluded_metrics_paths):
        audit_log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "userName": user_name,
            "clientHost": f"{client_host}:{request.url.port}",
            "userAgent": user_agent,
            "endpoint": request.url.path,
            "actionName": action_name,
            "status": status,
            "requestId": req_id,
        }
        LogWriter.audit_log(json.dumps(audit_log_entry), mask_pii=False)
        update_metrics(start_time=start_time, label=request.url.path)

    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    graphname = request.url.components.path.split("/")[1]
    if (
        graphname == ""
        or graphname == "docs"
        or graphname == "openapi.json"
        or graphname == "metrics"
        or graphname == "health"
        # allow the UI endpoints to authenticate without knowing graph name
        or graphname == "ui"
    ):
        return await call_next(request)

    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, credentials = authorization.split()
        if scheme.lower() == "basic":
            LogWriter.info("Authenticating with basic auth")
            username, password = b64decode(credentials).decode().split(":", 1)
            credentials = HTTPBasicCredentials(username=username, password=password)
            try:
                conn = get_db_connection_pwd(graphname, credentials)
            except HTTPException as e:
                LogWriter.error(
                    "Failed to connect to TigerGraph. Incorrect username or password."
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "message": "Failed to connect to TigerGraph. Incorrect username or password."
                    },
                )
        else:
            LogWriter.info("Authenticating with id token")
            try:
                conn = get_db_connection_id_token(graphname, credentials)
            except HTTPException as e:
                LogWriter.error("Failed to connect to TigerGraph. Incorrect ID Token.")
                return JSONResponse(
                    status_code=401,
                    content={
                        "message": "Failed to connect to TigerGraph. Incorrect ID Token."
                    },
                )
        request.state.conn = conn
    response = await call_next(request)
    return response


def update_metrics(start_time, label):
    duration = time.time() - start_time
    pmetrics.graphrag_endpoint_duration_seconds.labels(label).observe(duration)
    pmetrics.graphrag_endpoint_total.labels(label).inc()
