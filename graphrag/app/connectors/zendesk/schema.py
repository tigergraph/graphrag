"""Zendesk vertex types. A ticket is never stored as a Jira issue."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema
from connectors.schema_reuse import can_reuse_vertex
from connectors.schema_status import attribute_types, evaluate_proposal

REQUESTED = "requested"
MEMBER = "member"
AUTHORED = "authored"
CHANGED = "changed"
LINKS_ISSUE = "links_issue"

_CONTAINS_TARGETS = ("ZendeskTicket", "ZendeskComment")


def zendesk_schema_proposal(
    existing_vertices: set[str] | None = None,
) -> SchemaProposal:
    proposal = SchemaProposal(domain_label="Zendesk")
    proposal.add_vertex(
        "ZendeskTicket",
        "A Zendesk support ticket. It is not a Jira issue.",
        [
            ("ticket_id", "STRING"),
            ("ticket_type", "STRING"),
            ("subject", "STRING"),
            ("description", "STRING"),
            ("status", "STRING"),
            ("priority", "STRING"),
            ("tags", "STRING"),
            ("custom_fields", "STRING"),
            ("problem_id", "STRING"),
            ("group_name", "STRING"),
            ("url", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "Person",
        "A person shared by connectors. email is the id when the API returns it.",
        [
            ("email", "STRING"),
            ("display_name", "STRING"),
        ],
    )
    proposal.add_vertex(
        "Organization",
        "A customer company. domain is the id when Zendesk returns one.",
        [
            ("name", "STRING"),
            ("domain", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskComment",
        "One comment. ticket_id is the ticket. visibility is public or internal.",
        [
            ("comment_id", "STRING"),
            ("ticket_id", "STRING"),
            ("body", "STRING"),
            ("visibility", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskChange",
        "One status, priority, or assignee change. ticket_id is the ticket.",
        [
            ("change_id", "STRING"),
            ("ticket_id", "STRING"),
            ("field", "STRING"),
            ("from_value", "STRING"),
            ("to_value", "STRING"),
            ("created", "STRING"),
        ],
    )
    proposal.add_vertex(
        "Attachment",
        "A file on a ticket. parent_id is the ticket id. The bytes are not stored.",
        [
            ("file_name", "STRING"),
            ("content_url", "STRING"),
            ("parent_id", "STRING"),
        ],
    )
    proposal.add_edge_pair(
        REQUESTED,
        "Person",
        "ZendeskTicket",
        "The person who requested the ticket.",
    )
    proposal.add_edge_pair(
        MEMBER,
        "Organization",
        "Person",
        "The customer organization this person belongs to.",
    )
    proposal.add_edge_pair(
        AUTHORED,
        "ZendeskComment",
        "Person",
        "The person who wrote the comment.",
    )
    proposal.add_edge_pair(
        CHANGED,
        "ZendeskChange",
        "Person",
        "The person who made the change.",
    )
    folded = {name.casefold() for name in (existing_vertices or set())}
    if "jiraissue" in folded:
        proposal.add_edge_pair(
            LINKS_ISSUE,
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
