"""Router-facing Zendesk operations.

The data-sources router calls these four objects and does not import the
Zendesk client.
"""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import apply_proposal, read_existing_schema

from .client import ZendeskAPIError, ZendeskClient
from .config import ZendeskDataSource
from .schema import drop_reused_vertices, zendesk_schema_proposal, zendesk_schema_status
from .state import ZendeskSourceStore
from .sync import ZendeskSyncService


class ConnectorCallError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _http_error(exc: ZendeskAPIError) -> ConnectorCallError:
    if exc.status_code == 401:
        detail = (
            "Zendesk rejected the credentials. Use the agent email and an API token."
        )
    elif exc.status_code == 403:
        detail = "Zendesk accepted the credentials but denied access."
    elif exc.status_code == 404:
        detail = (
            "Zendesk was not found for this subdomain. "
            "Use the account subdomain, for example acme."
        )
    else:
        detail = str(exc)
    return ConnectorCallError(exc.status_code, detail)


def test_connection(source: ZendeskDataSource) -> dict[str, Any]:
    """Call users/me and try the Jira link inventory."""
    try:
        with ZendeskClient(source) as client:
            account = client.me()
            link_error = None
            try:
                next(client.iter_jira_link_pages(), None)
            except ZendeskAPIError as exc:
                link_error = str(exc)
    except ZendeskAPIError as exc:
        raise _http_error(exc) from exc
    return {
        "account": {
            "id": account.get("id"),
            "name": account.get("name"),
            "email": account.get("email"),
        },
        "link_error": link_error,
    }


def count_tickets(source: ZendeskDataSource) -> int:
    try:
        with ZendeskClient(source) as client:
            return client.count_tickets()
    except ZendeskAPIError as exc:
        raise _http_error(exc) from exc


def install_schema(conn, graphname: str) -> dict[str, Any]:
    existing = read_existing_schema(conn)
    proposal = zendesk_schema_proposal(existing.vertex_types)
    drop_reused_vertices(proposal, conn)
    return apply_proposal(conn, graphname, proposal)


def schema_status(conn) -> dict[str, Any]:
    return zendesk_schema_status(conn)


def sync(graphname: str, source: ZendeskDataSource, conn, store=None) -> dict[str, Any]:
    return ZendeskSyncService(
        graphname,
        source,
        conn,
        store=store or ZendeskSourceStore(),
    ).run()
