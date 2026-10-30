"""Predefined Jira domain schema."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal
from connectors.schema_status import evaluate_proposal


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


_CONTAINS_ENTITY_PAIRS = (
    ("CONTAINS_ENTITY", "Document", "JiraIssue"),
    ("CONTAINS_ENTITY", "DocumentChunk", "JiraIssue"),
    ("CONTAINS_ENTITY", "Document", "JiraComment"),
    ("CONTAINS_ENTITY", "DocumentChunk", "JiraComment"),
    ("CONTAINS_ENTITY", "Document", "JiraChange"),
    ("CONTAINS_ENTITY", "DocumentChunk", "JiraChange"),
)


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
            ("email", "STRING"),
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
    return evaluate_proposal(
        conn,
        jira_schema_proposal(),
        _CONTAINS_ENTITY_PAIRS,
    )
