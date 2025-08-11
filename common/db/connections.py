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

import logging
import asyncio
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasicCredentials, HTTPAuthorizationCredentials
from pyTigerGraph import TigerGraphConnection, AsyncTigerGraphConnection
from pyTigerGraph.common.exception import TigerGraphException
from requests import HTTPError

from common.config import (
    db_config,
    security,
)
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)
consistency_checkers = {}


def get_db_connection_id_token(
    graphname: str,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    async_conn: bool = False
) -> TigerGraphConnectionProxy:
    if async_conn:
        conn = AsyncTigerGraphConnection(
            host=db_config["hostname"],
            graphname=graphname,
            apiToken=credentials,
            tgCloud=True,
            sslPort=14240,
        )
        asyncio.run(conn.customizeHeader(
            timeout=db_config["default_timeout"] * 1000, responseSize=5000000
        ))
    else:
        conn = TigerGraphConnection(
            host=db_config["hostname"],
            graphname=graphname,
            apiToken=credentials,
            tgCloud=True,
            sslPort=14240,
        )
        conn.customizeHeader(
            timeout=db_config["default_timeout"] * 1000, responseSize=5000000
        )

        conn = TigerGraphConnectionProxy(conn, auth_mode="token")

    try:
        if async_conn:
            asyncio.run(conn.gsql("USE GRAPH " + graphname))
        else:
            conn.gsql("USE GRAPH " + graphname)
    except HTTPError:
        LogWriter.error("Failed to connect to TigerGraph. Incorrect ID Token.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except TigerGraphException as e:
        LogWriter.error(f"Failed to get token: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to get token - is the database running?"
        )

    LogWriter.info("Connected to TigerGraph with ID Token")
    return conn


def get_db_connection_pwd(
    graphname, credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    async_conn: bool = False
) -> TigerGraphConnectionProxy:
    conn = elevate_db_connection_to_token(db_config["hostname"], credentials.username, credentials.password, graphname, async_conn)

    conn.customizeHeader(
        timeout=db_config["default_timeout"] * 1000, responseSize=5000000
    )
    conn = TigerGraphConnectionProxy(conn)
    LogWriter.info("Connected to TigerGraph with password")
    return conn



def get_db_connection_pwd_manual(
    graphname, username: str, password: str,
    async_conn: bool = False
) -> TigerGraphConnectionProxy:
    """
    Manual auth - pass in user/pass not from basic auth
    """
    conn = elevate_db_connection_to_token(
            db_config["hostname"], username, password, graphname, async_conn
        )

    conn.customizeHeader(
        timeout=db_config["default_timeout"] * 1000, responseSize=5000000
    )
    conn = TigerGraphConnectionProxy(conn)
    LogWriter.info("Connected to TigerGraph with password")
    return conn

def elevate_db_connection_to_token(host, username, password, graphname, async_conn: bool = False) -> TigerGraphConnectionProxy:
    conn = TigerGraphConnection(
        host=host,
        username=username,
        password=password,
        graphname=graphname,
        restppPort=db_config.get("restppPort", "9000"),
        gsPort=db_config.get("gsPort", "14240")
    )
    
    if db_config["getToken"]:
        try:
            apiToken = conn.getToken()[0]
        except HTTPError:
            LogWriter.error("Failed to get token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Basic"},
            )
        except TigerGraphException as e:
            LogWriter.error(f"Failed to get token: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to get token - is the database running?"
            )

        if async_conn:
            conn = AsyncTigerGraphConnection(
                host=host,
                username=username,
                password=password,
                graphname=graphname,
                apiToken=apiToken,
                restppPort=db_config.get("restppPort", "9000"),
                gsPort=db_config.get("gsPort", "14240")
            )
        else:
            conn = TigerGraphConnection(
                host=db_config["hostname"],
                username=username,
                password=password,
                graphname=graphname,
                apiToken=apiToken,
                restppPort=db_config.get("restppPort", "9000"),
                gsPort=db_config.get("gsPort", "14240")
            )
    else:
        if async_conn:
            conn = AsyncTigerGraphConnection(
                host=host,
                username=username,
                password=password,
                graphname=graphname,
                restppPort=db_config.get("restppPort", "9000"),
                gsPort=db_config.get("gsPort", "14240")
            )

            # temp fix for path
            if conn.restppPort == conn.gsPort and "/restpp" not in conn.restppUrl:
                conn.restppUrl = conn.restppUrl+"/restpp"

    return conn


def get_schema_ver(conn: TigerGraphConnectionProxy) -> int:
    """Retrieves the schema version of the graph by running an interpreted query.

    Returns:
        The schema version as an integer.
    """
    logger.info("entry: _get_schema_ver")

    # Create the interpreted query to get schema version
    query_text = f'INTERPRET QUERY () FOR GRAPH {conn.graphname} {{ PRINT "OK"; }}'

    try:
        # Run the interpreted query
        #result = self.conn.runInterpretedQuery(query_text)
        if conn._version_greater_than_4_0():
            ret = conn._post(conn.gsUrl + "/gsql/v1/queries/interpret",
                            params={}, data=query_text, authMode="pwd", resKey="version",
                            headers={'Content-Type': 'text/plain'})
        else:
            ret = conn._post(conn.gsUrl + "/gsqlserver/interpreted_query", data=query_text,
                            params={}, authMode="pwd", resKey="version")

        schema_version_int = None
        if isinstance(ret, dict) and "schema" in ret:
            schema_version = ret["schema"]
            try:
                schema_version_int = int(schema_version)
            except (ValueError, TypeError):
                logger.warning(f"Schema version '{schema_version}' could not be converted to integer")
        if schema_version_int is None:
            logger.warning("Schema version not found in query result")
        logger.info("exit: _get_schema_ver")
        return schema_version_int

    except Exception as e:
        logger.error(f"Error getting schema version: {str(e)}")
        raise Exception(f"Failed to get schema version: {str(e)}")
