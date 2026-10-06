"""Predefined Jira domain schema."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema


JIRA_PROJECT_EDGE = "JIRA_BELONGS_TO"
JIRA_ASSIGNEE_EDGE = "JIRA_ASSIGNED_TO"
JIRA_REPORTER_EDGE = "JIRA_REPORTED_BY"
JIRA_PARENT_EDGE = "JIRA_HAS_PARENT"
JIRA_LINK_EDGE = "JIRA_LINKS_TO"
JIRA_COMMENT_ISSUE_EDGE = "JIRA_COMMENT_ON"
JIRA_COMMENT_AUTHOR_EDGE = "JIRA_COMMENTED_BY"
JIRA_COMMENT_REPLY_EDGE = "JIRA_COMMENT_REPLIES_TO"
JIRA_COMMENT_AFTER_EDGE = "JIRA_COMMENT_AFTER"
JIRA_CHANGE_EDGE = "JIRA_HAS_CHANGE"
JIRA_CHANGE_AUTHOR_EDGE = "JIRA_CHANGE_BY"


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
    """Return the bounded Jira schema consumed by GraphRAG."""
    proposal = SchemaProposal(domain_label="Jira Cloud")
    proposal.add_vertex(
        "JiraProject",
        (
            "A Jira project, modeled as a POLE+O Object subtype and work "
            "container. project_key is the short key, such as PAY."
        ),
        [
            ("project_key", "STRING"),
            ("name", "STRING"),
            ("url", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraIssue",
        (
            "A Jira work item modeled as a POLE+O Object subtype. Filter using "
            "issue_key, status, status_category (new, indeterminate, or done), "
            "priority, issue_type, resolution, labels, components, "
            "fix_versions, created, updated, or due. Status, assignee, "
            "priority, and resolution changes are JiraChange events."
        ),
        [
            ("issue_key", "STRING"),
            ("summary", "STRING"),
            ("issue_type", "STRING"),
            ("status", "STRING"),
            ("status_category", "STRING"),
            ("priority", "STRING"),
            ("resolution", "STRING"),
            ("labels", "STRING"),
            ("components", "STRING"),
            ("fix_versions", "STRING"),
            ("created", "DATETIME"),
            ("updated", "DATETIME"),
            ("due", "DATETIME"),
            ("url", "STRING"),
            ("story_points", "DOUBLE"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraUser",
        (
            "An Atlassian account modeled as a POLE+O Person subtype. "
            "account_id is the stable identity and display_name is the "
            "human-readable name."
        ),
        [
            ("account_id", "STRING"),
            ("display_name", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraComment",
        (
            "A Jira comment modeled as a POLE+O Event subtype. body is the "
            "comment text. Searchable text is also stored as document chunks. "
            "Use graph edges for issue, author, ordering, and explicit replies."
        ),
        [
            ("comment_id", "STRING"),
            ("body", "STRING"),
            ("created", "DATETIME"),
            ("updated", "DATETIME"),
            ("visibility", "STRING"),
            ("is_public", "BOOL"),
            ("ontology_class", "STRING"),
            ("content_hash", "STRING"),
        ],
    )
    proposal.add_vertex(
        "JiraChange",
        (
            "One status, assignee, priority, or resolution change, modeled as "
            "a POLE+O Event subtype. field is the changed field, from_value "
            "and to_value are the previous and new values, and created is "
            "when the change happened. The author is JIRA_CHANGE_BY."
        ),
        [
            ("change_id", "STRING"),
            ("field", "STRING"),
            ("from_value", "STRING"),
            ("to_value", "STRING"),
            ("created", "DATETIME"),
            ("ontology_class", "STRING"),
        ],
    )

    proposal.add_edge_pair(
        JIRA_PROJECT_EDGE,
        "JiraIssue",
        "JiraProject",
        "The Jira issue's current project.",
    )
    proposal.add_edge_pair(
        JIRA_ASSIGNEE_EDGE,
        "JiraIssue",
        "JiraUser",
        "The Jira issue's current assignee.",
    )
    proposal.add_edge_pair(
        JIRA_REPORTER_EDGE,
        "JiraIssue",
        "JiraUser",
        "The Atlassian account that reported the Jira issue.",
    )
    proposal.add_edge_pair(
        JIRA_PARENT_EDGE,
        "JiraIssue",
        "JiraIssue",
        "The parent Jira issue, including epic and subtask parents.",
    )
    proposal.add_edge_pair(
        JIRA_LINK_EDGE,
        "JiraIssue",
        "JiraIssue",
        (
            "A directed Jira issue link. link_type is the phrase from the "
            "source issue toward the target, such as blocks or is blocked by."
        ),
        [("link_type", "STRING")],
    )
    proposal.add_edge_pair(
        JIRA_COMMENT_ISSUE_EDGE,
        "JiraComment",
        "JiraIssue",
        "The Jira issue on which the comment was posted.",
    )
    proposal.add_edge_pair(
        JIRA_COMMENT_AUTHOR_EDGE,
        "JiraComment",
        "JiraUser",
        "The Atlassian account that authored the comment.",
    )
    proposal.add_edge_pair(
        JIRA_COMMENT_REPLY_EDGE,
        "JiraComment",
        "JiraComment",
        "An explicit source-provided parent comment; never inferred.",
    )
    proposal.add_edge_pair(
        JIRA_COMMENT_AFTER_EDGE,
        "JiraComment",
        "JiraComment",
        "Chronological order between adjacent comments on one issue.",
    )
    proposal.add_edge_pair(
        JIRA_CHANGE_EDGE,
        "JiraIssue",
        "JiraChange",
        "A status, assignee, priority, or resolution change on this issue.",
    )
    proposal.add_edge_pair(
        JIRA_CHANGE_AUTHOR_EDGE,
        "JiraChange",
        "JiraUser",
        "The Atlassian account that made the change.",
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

    required_links = (
        ("CONTAINS_ENTITY", "Document", "JiraIssue"),
        ("CONTAINS_ENTITY", "DocumentChunk", "JiraIssue"),
        ("CONTAINS_ENTITY", "Document", "JiraComment"),
        ("CONTAINS_ENTITY", "DocumentChunk", "JiraComment"),
        ("CONTAINS_ENTITY", "Document", "JiraChange"),
        ("CONTAINS_ENTITY", "DocumentChunk", "JiraChange"),
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
