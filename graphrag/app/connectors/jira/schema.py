"""Predefined Jira domain schema."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema


ASSIGNED_TO_EDGE = "assigned_to"
REPORTED_EDGE = "reported"
AUTHORED_EDGE = "authored"
CHANGED_EDGE = "changed"
PART_OF_EDGE = "part_of"
RELATES_TO_EDGE = "relates_to"


def _attribute_types(metadata: dict[str, Any]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    primary_id = (metadata.get("PrimaryId") or {}).get("AttributeName")
    for attribute in metadata.get("Attributes") or []:
        name = attribute.get("AttributeName")
        if not name or name == primary_id:
            continue
        attribute_type = (
            (attribute.get("AttributeType") or {}).get("Name") or "STRING"
        )
        attributes[str(name).casefold()] = str(attribute_type).upper()
    return attributes


def _edge_pairs(metadata: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    source = metadata.get("FromVertexTypeName")
    target = metadata.get("ToVertexTypeName")
    if source and target and source != "*" and target != "*":
        pairs.add((str(source).casefold(), str(target).casefold()))
    for pair in metadata.get("EdgePairs") or []:
        source = pair.get("From")
        target = pair.get("To")
        if source and target:
            pairs.add((str(source).casefold(), str(target).casefold()))
    return pairs


def jira_schema_proposal() -> SchemaProposal:
    """Return the Jira schema from the connector design."""
    proposal = SchemaProposal(domain_label="Jira Cloud")
    proposal.add_vertex(
        "JiraIssue",
        (
            "One Jira issue. status is the current status. summary and "
            "description answer a summary. due is the due date. fix_versions "
            "is the fix version, and a parenthetical release date on that "
            "value is when that version is scheduled. project_key is the "
            "prefix of issue_key. project_name is the Jira project name."
        ),
        [
            ("issue_key", "STRING"),
            ("summary", "STRING"),
            ("description", "STRING"),
            ("project_key", "STRING"),
            ("project_name", "STRING"),
            ("issue_type", "STRING"),
            ("status", "STRING"),
            ("status_category", "STRING"),
            ("priority", "STRING"),
            ("resolution", "STRING"),
            ("labels", "STRING"),
            ("components", "STRING"),
            ("fix_versions", "STRING"),
            ("custom_fields", "STRING"),
            ("created", "DATETIME"),
            ("updated", "DATETIME"),
            ("due", "DATETIME"),
            ("url", "STRING"),
            ("story_points", "DOUBLE"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "Person",
        (
            "A person shared by connectors. email is stored when the source "
            "returns an email address. display_name is the name to show."
        ),
        [
            ("email", "STRING"),
            ("display_name", "STRING"),
            ("email", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraComment",
        (
            "One comment. issue_key is the issue it belongs to. body is the "
            "comment text. Order comments by created."
        ),
        [
            ("comment_id", "STRING"),
            ("issue_key", "STRING"),
            ("body", "STRING"),
            ("created", "DATETIME"),
            ("updated", "DATETIME"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraChange",
        (
            "One status, assignee, priority, or resolution change. issue_key "
            "is the issue. field, from_value, to_value, and created are the "
            "change. The person is the changed edge."
        ),
        [
            ("change_id", "STRING"),
            ("issue_key", "STRING"),
            ("field", "STRING"),
            ("from_value", "STRING"),
            ("to_value", "STRING"),
            ("created", "DATETIME"),
        ],
    )
    proposal.add_vertex(
        "Attachment",
        (
            "A file attached to a record. parent_id is the issue key. "
            "file_name and content_url identify the file. The bytes are not "
            "stored."
        ),
        [
            ("file_name", "STRING"),
            ("content_url", "STRING"),
            ("parent_id", "STRING"),
        ],
    )

    proposal.add_edge_pair(
        ASSIGNED_TO_EDGE,
        "JiraIssue",
        "Person",
        "The current assignee. This is Jira's assignee field.",
    )
    proposal.add_edge_pair(
        REPORTED_EDGE,
        "JiraIssue",
        "Person",
        "The reporter. This is Jira's reporter field.",
    )
    proposal.add_edge_pair(
        AUTHORED_EDGE,
        "JiraComment",
        "Person",
        "The person who wrote the comment.",
    )
    proposal.add_edge_pair(
        CHANGED_EDGE,
        "JiraChange",
        "Person",
        "The person who made the change.",
    )
    proposal.add_edge_pair(
        PART_OF_EDGE,
        "JiraIssue",
        "JiraIssue",
        "The parent epic or parent issue.",
    )
    proposal.add_edge_pair(
        RELATES_TO_EDGE,
        "JiraIssue",
        "JiraIssue",
        (
            "Another issue. link_type is the Jira phrase, such as blocks or "
            "relates to."
        ),
        [("link_type", "STRING")],
    )
    return proposal


def jira_schema_status(conn) -> dict[str, Any]:
    """Inspect whether the one current Jira schema is fully installed."""
    proposal = jira_schema_proposal()
    existing = read_existing_schema(conn)
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
                conflicts.append(
                    f"{vertex.name}.{attribute.name} is missing"
                )
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

    required_links = tuple(
        (edge, source, vertex.name)
        for vertex in proposal.vertices
        for edge, source in (
            ("CONTAINS_ENTITY", "Document"),
            ("CONTAINS_ENTITY", "DocumentChunk"),
        )
    )
    for edge, source, target in required_links:
        if not existing.has_edge_pair(edge, source, target):
            missing_pairs.append(f"{edge}: {source} -> {target}")

    missing = {
        "vertices": missing_vertices,
        "edges": missing_edges,
        "pairs": missing_pairs,
    }
    if conflicts:
        status = "conflict"
    elif not any(missing.values()):
        status = "installed"
    elif any(
        existing.has_vertex(vertex.name) for vertex in proposal.vertices
    ):
        status = "incomplete"
    else:
        status = "not_installed"

    return {
        "status": status,
        "missing": missing,
        "conflicts": conflicts,
    }
