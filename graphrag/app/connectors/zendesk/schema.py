"""Zendesk vertex types. A ticket is never stored as a Jira issue."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema
from connectors.schema_reuse import can_reuse_vertex
from connectors.schema_status import attribute_types, evaluate_proposal

ZENDESK_REQUESTED_BY = "ZENDESK_REQUESTED_BY"
ZENDESK_COMMENT_ON = "ZENDESK_COMMENT_ON"
ZENDESK_LINKS_TO_JIRA = "ZENDESK_LINKS_TO_JIRA"

_CONTAINS_TARGETS = ("ZendeskTicket", "ZendeskUser", "ZendeskComment")


def zendesk_schema_proposal(
    existing_vertices: set[str] | None = None,
) -> SchemaProposal:
    proposal = SchemaProposal(domain_label="Zendesk")
    proposal.add_vertex(
        "ZendeskTicket",
        "A Zendesk support ticket. It is not a Jira issue.",
        [
            ("ticket_id", "STRING"),
            ("subject", "STRING"),
            ("status", "STRING"),
            ("url", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskUser",
        "A Zendesk requester or comment author.",
        [
            ("user_id", "STRING"),
            ("display_name", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskComment",
        "One Zendesk comment body.",
        [
            ("comment_id", "STRING"),
            ("body", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_edge_pair(
        ZENDESK_REQUESTED_BY,
        "ZendeskTicket",
        "ZendeskUser",
        "The user who requested the ticket.",
    )
    proposal.add_edge_pair(
        ZENDESK_COMMENT_ON,
        "ZendeskComment",
        "ZendeskTicket",
        "The ticket that owns the comment.",
    )
    folded = {name.casefold() for name in (existing_vertices or set())}
    if "jiraissue" in folded:
        proposal.add_edge_pair(
            ZENDESK_LINKS_TO_JIRA,
            "ZendeskTicket",
            "JiraIssue",
            "This ticket is named by a Zendesk Jira-integration link row.",
        )
    return proposal


def _contains_pairs() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        ("CONTAINS_ENTITY", container, vertex_name)
        for vertex_name in _CONTAINS_TARGETS
        for container in ("Document", "DocumentChunk")
    )


def zendesk_schema_status(conn) -> dict[str, Any]:
    """Inspect whether the Zendesk schema is installed on this graph."""
    existing = read_existing_schema(conn)
    proposal = zendesk_schema_proposal(existing.vertex_types)
    return evaluate_proposal(conn, proposal, _contains_pairs())


def drop_reused_vertices(proposal: SchemaProposal, conn) -> list[str]:
    """Remove proposed vertices that safely match an installed type."""
    reused: list[str] = []
    kept = []
    for vertex in proposal.vertices:
        attributes = {attribute.name: attribute.type for attribute in vertex.attributes}
        match = None
        for name in conn.getVertexTypes() or []:
            metadata = conn.getVertexType(name) or {}
            if can_reuse_vertex(
                vertex.name,
                attributes,
                str(name),
                attribute_types(metadata),
            ):
                match = name
                break
        if match is None:
            kept.append(vertex)
        else:
            reused.append(vertex.name)
    proposal.vertices = kept
    return reused
