"""Confluence vertex types. A page is never stored as a Jira issue or a Zendesk ticket."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema
from connectors.schema_reuse import can_reuse_vertex
from connectors.schema_status import attribute_types, evaluate_proposal

AUTHORED = "authored"
EDITED = "edited"
OWNED = "owned"
MEMBER = "member"
LINKS_ISSUE = "links_issue"
LINKS_TICKET = "links_ticket"

_CONTAINS_TARGETS = ("ConfluencePage", "ConfluenceComment")


def confluence_schema_proposal(
    existing_vertices: set[str] | None = None,
) -> SchemaProposal:
    proposal = SchemaProposal(domain_label="Confluence")
    proposal.add_vertex(
        "ConfluencePage",
        "A Confluence page or blog post. It is not a Jira issue or a Zendesk ticket.",
        [
            ("page_id", "STRING"),
            ("content_type", "STRING"),
            ("title", "STRING"),
            ("body", "STRING"),
            ("status", "STRING"),
            ("space_key", "STRING"),
            ("space_name", "STRING"),
            ("parent_id", "STRING"),
            ("labels", "STRING"),
            ("version_number", "STRING"),
            ("url", "STRING"),
            ("created", "STRING"),
            ("updated", "STRING"),
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
        "A customer company. domain is the id when another connector stored one.",
        [
            ("name", "STRING"),
            ("domain", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ConfluenceComment",
        "One comment. page_id is the page. comment_type is footer or inline.",
        [
            ("comment_id", "STRING"),
            ("page_id", "STRING"),
            ("body", "STRING"),
            ("comment_type", "STRING"),
            ("resolution", "STRING"),
            ("created", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "Attachment",
        "A file on a page. parent_id is the page id. The bytes are not stored.",
        [
            ("file_name", "STRING"),
            ("content_url", "STRING"),
            ("parent_id", "STRING"),
        ],
    )
    proposal.add_edge_pair(
        AUTHORED,
        "ConfluencePage",
        "Person",
        "The person who created the page.",
    )
    proposal.add_edge_pair(
        AUTHORED,
        "ConfluenceComment",
        "Person",
        "The person who wrote the comment.",
    )
    proposal.add_edge_pair(
        EDITED,
        "ConfluencePage",
        "Person",
        "The person who authored the current version.",
    )
    proposal.add_edge_pair(
        OWNED,
        "ConfluencePage",
        "Person",
        "The current owner of the page.",
    )
    proposal.add_edge_pair(
        MEMBER,
        "Organization",
        "Person",
        "The customer organization this person belongs to.",
    )
    folded = {name.casefold() for name in (existing_vertices or set())}
    if "jiraissue" in folded:
        proposal.add_edge_pair(
            LINKS_ISSUE,
            "ConfluencePage",
            "JiraIssue",
            "This page names a Jira issue key.",
        )
    if "zendeskticket" in folded:
        proposal.add_edge_pair(
            LINKS_TICKET,
            "ConfluencePage",
            "ZendeskTicket",
            "This page names a Zendesk ticket URL.",
        )
    return proposal


def _contains_pairs() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        ("CONTAINS_ENTITY", container, vertex_name)
        for vertex_name in _CONTAINS_TARGETS
        for container in ("Document", "DocumentChunk")
    )


def confluence_schema_status(conn) -> dict[str, Any]:
    """Inspect whether the Confluence schema is installed on this graph."""
    existing = read_existing_schema(conn)
    proposal = confluence_schema_proposal(existing.vertex_types)
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
