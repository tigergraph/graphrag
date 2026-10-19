"""Ingest one Zendesk ticket and resolve links that were waiting for it."""

from __future__ import annotations

from connectors.cross_source import (
    TigerGraphLinkGraph,
    link_text,
    resolve_target,
)
from connectors.jira.mapper import VertexRecord
from connectors.jira.sync import _upsert_payload
import json


def ticket_vertex_id(subdomain: str, ticket_id: str) -> str:
    return f"zendesk:{subdomain.strip().lower()}:ticket:{ticket_id}"


def ingest_ticket(conn, subdomain: str, ticket: dict) -> str:
    """Upsert this ticket only, then resolve pending references to it.

    The payload does not include a Jira vertex. A reference to an existing
    Jira issue creates an edge without overwriting that issue.
    """
    ticket_id = str(ticket["id"])
    vertex_id = ticket_vertex_id(subdomain, ticket_id)
    attributes = {
        "ticket_id": ticket_id,
        "subject": str(ticket.get("subject") or ""),
        "status": str(ticket.get("status") or ""),
        "url": str(ticket.get("url") or ""),
    }
    payload = _upsert_payload(
        [VertexRecord("ZendeskTicket", vertex_id, attributes)],
        [],
    )
    conn.upsertData(json.dumps(payload))
    graph = TigerGraphLinkGraph(conn)
    resolve_target(graph, "ZendeskTicket", vertex_id)
    link_text(
        graph,
        "ZendeskTicket",
        vertex_id,
        "\n".join(
            str(ticket.get(field) or "")
            for field in ("subject", "description", "url")
        ),
    )
    return vertex_id
