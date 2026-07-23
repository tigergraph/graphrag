# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Batched, timeout-safe query installation.

pyTigerGraph's ``installQueries()`` submits a *synchronous* install (it omits
``async=true``), so a large query set whose compile exceeds TG's gsql gateway
limit (~390s) fails with a server disconnect regardless of the client timeout.
This utility submits the install as a background job (``async=true``) and polls
the install status to completion instead — the submit returns in ~0.1s and the
compile time no longer bounds any single request.

Shared by the ECC rebuild (async) and the Migration Assistant (sync). Both
install ONLY a given set of query names (with ``-force``), never
``INSTALL QUERY ALL`` — installing ``ALL`` recompiles every query on the graph.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_INSTALL_PATH = "/gsql/v1/queries/install"
DEFAULT_TIMEOUT_S = 1800
_POLL_S = 10


def _install_params(graphname: str, query_names: list[str], force: bool) -> dict:
    params = {
        "graph": graphname,
        "queries": ",".join(query_names),
        "async": "true",
    }
    if force:
        params["flag"] = "-force"
    return params


def _request_id(res) -> str:
    request_id = res.get("requestId") if isinstance(res, dict) else None
    if not request_id:
        raise Exception(f"Query install submit returned no requestId: {res}")
    return request_id


def _status_done(status) -> bool:
    """Return True on SUCCESS; raise on FAILED; False while still running."""
    msg = (status.get("message", "") if isinstance(status, dict) else str(status)) or ""
    if "SUCCESS" in msg.upper():
        return True
    if "FAIL" in msg.upper():
        raise Exception(f"Query installation failed: {status}")
    return False


# ---- sync (Migration Assistant) ------------------------------------------

def submit_query_install(conn, query_names: list[str], force: bool = True) -> str:
    """Submit a background install for ``query_names``; return its requestId."""
    res = conn._req(
        "GET", conn.gsUrl + _INSTALL_PATH,
        params=_install_params(conn.graphname, query_names, force),
        authMode="pwd", resKey=None,
    )
    return _request_id(res)


def poll_query_install(conn, request_id: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
    """Poll the install job until SUCCESS (return) / FAILED / timeout (raise)."""
    waited = 0
    while waited < timeout_s:
        time.sleep(_POLL_S)
        waited += _POLL_S
        if _status_done(conn.getQueryInstallationStatus(request_id)):
            return
    raise Exception(f"Query installation timed out after {timeout_s}s (requestId={request_id})")


def install_query_set(conn, query_names: list[str], force: bool = True,
                      timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
    """Install exactly ``query_names`` (submit + poll). No-op on empty list."""
    if not query_names:
        return
    logger.info(f"Installing {len(query_names)} query(ies): {', '.join(sorted(query_names))}")
    poll_query_install(conn, submit_query_install(conn, query_names, force), timeout_s)


# ---- async (ECC rebuild) --------------------------------------------------

async def submit_query_install_async(conn, query_names: list[str], force: bool = True) -> str:
    res = await conn._req(
        "GET", conn.gsUrl + _INSTALL_PATH,
        params=_install_params(conn.graphname, query_names, force),
        authMode="pwd", resKey=None,
    )
    return _request_id(res)


async def poll_query_install_async(conn, request_id: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
    waited = 0
    while waited < timeout_s:
        await asyncio.sleep(_POLL_S)
        waited += _POLL_S
        if _status_done(await conn.getQueryInstallationStatus(request_id)):
            return
    raise Exception(f"Query installation timed out after {timeout_s}s (requestId={request_id})")
