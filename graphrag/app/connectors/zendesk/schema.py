"""Zendesk vertex types. Jira issues are never reused as Zendesk tickets."""

from __future__ import annotations

from common.db.schema_utils import SchemaProposal

from connectors.cross_source import CROSS_SOURCE_EDGE, add_pending_reference_schema
from connectors.schema_reuse import can_reuse_vertex


def zendesk_schema_proposal(existing_vertices: set[str] | None = None) -> SchemaProposal:
    proposal = SchemaProposal(domain_label="Zendesk")
    proposal.add_vertex(
        "ZendeskTicket",
        "A Zendesk ticket. Its id is not a Jira issue key.",
        [
            ("ticket_id", "STRING"),
            ("subject", "STRING"),
            ("status", "STRING"),
            ("url", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskUser",
        "A Zendesk user. Reuse a person vertex only when the primary key matches.",
        [
            ("user_id", "STRING"),
            ("display_name", "STRING"),
        ],
    )
    proposal.add_vertex(
        "ZendeskComment",
        "A Zendesk comment.",
        [
            ("comment_id", "STRING"),
            ("body", "STRING"),
        ],
    )
    proposal.add_edge_pair(
        "ZENDESK_REQUESTED_BY",
        "ZendeskTicket",
        "ZendeskUser",
        "The user who requested the ticket.",
    )
    proposal.add_edge_pair(
        "ZENDESK_COMMENT_ON",
        "ZendeskComment",
        "ZendeskTicket",
        "The ticket that owns the comment.",
    )
    add_pending_reference_schema(proposal, "ZendeskTicket")
    folded = {name.casefold() for name in (existing_vertices or set())}
    if "jiraissue" in folded:
        proposal.add_edge_pair(
            CROSS_SOURCE_EDGE,
            "JiraIssue",
            "ZendeskTicket",
            "A source reference or shared identity resolved during ingestion.",
        )
        proposal.add_edge_pair(
            CROSS_SOURCE_EDGE,
            "ZendeskTicket",
            "JiraIssue",
            "A source reference or shared identity resolved during ingestion.",
        )
    return proposal


def zendesk_schema_status(conn) -> dict:
    """Inspect whether the Zendesk schema is installed on this graph."""
    from connectors.jira.schema import _attribute_types, _edge_pairs
    from common.db.schema_utils import read_existing_schema

    existing = read_existing_schema(conn)
    proposal = zendesk_schema_proposal(existing.vertex_types)
    conflicts: list[str] = []
    missing_vertices: list[str] = []
    missing_edges: list[str] = []
    missing_pairs: list[str] = []

    required_core = ("Document", "DocumentChunk", "Content")
    missing_core = [name for name in required_core if not existing.has_vertex(name)]
    required_core_edges = ("CONTAINS_ENTITY", "HAS_CHILD", "HAS_CONTENT")
    missing_core_edges = [
        name for name in required_core_edges if not existing.has_edge(name)
    ]
    if missing_core or missing_core_edges:
        return {
            "status": "not_initialized",
            "missing": {
                "core_vertices": missing_core,
                "core_edges": missing_core_edges,
            },
            "conflicts": [],
        }

    for vertex in proposal.vertices:
        if not existing.has_vertex(vertex.name):
            missing_vertices.append(vertex.name)
            continue
        metadata = conn.getVertexType(vertex.name) or {}
        actual = _attribute_types(metadata)
        for attribute in vertex.attributes:
            actual_type = actual.get(attribute.name.casefold())
            if actual_type is None:
                conflicts.append(f"{vertex.name}.{attribute.name} is missing")
            elif actual_type != attribute.type.upper():
                conflicts.append(
                    f"{vertex.name}.{attribute.name} must be "
                    f"{attribute.type.upper()}, found {actual_type}"
                )

    for edge in proposal.edges:
        if not existing.has_edge(edge.name):
            missing_edges.append(edge.name)
            continue
        metadata = conn.getEdgeType(edge.name) or {}
        if bool(metadata.get("IsDirected")) != edge.directed:
            expected = "directed" if edge.directed else "undirected"
            conflicts.append(f"{edge.name} must be {expected}")
        actual_attributes = _attribute_types(metadata)
        for attribute in edge.attributes:
            actual_type = actual_attributes.get(attribute.name.casefold())
            if actual_type is None:
                conflicts.append(f"{edge.name}.{attribute.name} is missing")
            elif actual_type != attribute.type.upper():
                conflicts.append(
                    f"{edge.name}.{attribute.name} must be "
                    f"{attribute.type.upper()}, found {actual_type}"
                )
        actual_pairs = _edge_pairs(metadata)
        for source, target in edge.pairs:
            if (source.casefold(), target.casefold()) not in actual_pairs:
                missing_pairs.append(f"{edge.name}: {source} -> {target}")

    for vertex_name in ("ZendeskTicket", "ZendeskUser", "ZendeskComment"):
        for container in ("Document", "DocumentChunk"):
            if not existing.has_edge_pair("CONTAINS_ENTITY", container, vertex_name):
                missing_pairs.append(
                    f"CONTAINS_ENTITY: {container} -> {vertex_name}"
                )

    missing = {
        "vertices": missing_vertices,
        "edges": missing_edges,
        "pairs": missing_pairs,
    }
    if conflicts:
        status = "conflict"
    elif not any(missing.values()):
        status = "installed"
    elif any(existing.has_vertex(vertex.name) for vertex in proposal.vertices):
        status = "incomplete"
    else:
        status = "not_installed"
    return {"status": status, "missing": missing, "conflicts": conflicts}


def drop_reused_vertices(
    proposal: SchemaProposal,
    existing: dict[str, dict[str, str]],
) -> list[str]:
    """Remove proposed vertices that safely match an installed type."""
    reused: list[str] = []
    kept = []
    for vertex in proposal.vertices:
        attributes = {attribute.name: attribute.type for attribute in vertex.attributes}
        match = None
        for name, existing_attributes in existing.items():
            if can_reuse_vertex(
                vertex.name,
                attributes,
                name,
                existing_attributes,
            ):
                match = name
                break
        if match is None:
            kept.append(vertex)
        else:
            reused.append(vertex.name)
    proposal.vertices = kept
    return reused
