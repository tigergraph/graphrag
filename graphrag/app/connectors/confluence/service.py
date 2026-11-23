"""Router-facing Confluence operations.

The data-sources router calls these objects and does not import the
Confluence client from the route handlers beyond this module.
"""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import apply_proposal, read_existing_schema

from .client import ConfluenceAPIError, ConfluenceClient
from .config import ConfluenceDataSource
from .schema import (
    confluence_schema_proposal,
    confluence_schema_status,
    drop_reused_vertices,
)


class ConnectorCallError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _http_error(exc: ConfluenceAPIError) -> ConnectorCallError:
    if exc.status_code == 401:
        detail = (
            "Confluence rejected the credentials. "
            "Use the Atlassian account email and an API token."
        )
    elif exc.status_code == 403:
        detail = "Confluence accepted the credentials but denied access."
    elif exc.status_code == 404:
        detail = (
            "Confluence was not found for this site. "
            "Use the site host, for example acme.atlassian.net."
        )
    else:
        detail = str(exc)
    return ConnectorCallError(exc.status_code, detail)


def test_connection(source: ConfluenceDataSource) -> dict[str, Any]:
    """Call the current-user endpoint."""
    try:
        with ConfluenceClient(source) as client:
            account = client.current_user()
    except ConfluenceAPIError as exc:
        raise _http_error(exc) from exc
    return {
        "account": {
            "account_id": account.get("accountId"),
            "display_name": account.get("displayName"),
            "email": account.get("email"),
        }
    }


def list_spaces(source: ConfluenceDataSource) -> list[dict[str, Any]]:
    try:
        with ConfluenceClient(source) as client:
            rows = client.spaces()
    except ConfluenceAPIError as exc:
        raise _http_error(exc) from exc
    spaces = []
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        spaces.append(
            {
                "id": str(row.get("id") or ""),
                "key": str(key),
                "name": str(row.get("name") or key),
            }
        )
    return spaces


def install_schema(conn, graphname: str) -> dict[str, Any]:
    existing = read_existing_schema(conn)
    proposal = confluence_schema_proposal(existing.vertex_types)
    drop_reused_vertices(proposal, conn)
    return apply_proposal(conn, graphname, proposal)


def schema_status(conn) -> dict[str, Any]:
    return confluence_schema_status(conn)
