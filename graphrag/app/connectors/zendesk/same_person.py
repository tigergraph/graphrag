"""Link a Zendesk user to a Jira user when their emails match.

Neither connector creates the other user. The edge is written by whichever
sync runs while both vertices already exist. Email is stored in lowercase.
"""

from __future__ import annotations

import logging

from connectors.records import EdgeRecord, VertexRecord

from .schema import ZENDESK_SAME_PERSON

logger = logging.getLogger(__name__)


def edges_for_zendesk_users(
    conn,
    users: list[tuple[str, str]],
) -> list[EdgeRecord]:
    """Return ZENDESK_SAME_PERSON edges for Zendesk users that match a Jira user."""
    return _edges(conn, users, source_is_zendesk=True)


def edges_for_jira_users(conn, vertices: list[VertexRecord]) -> list[EdgeRecord]:
    """Return ZENDESK_SAME_PERSON edges for Jira users that match a Zendesk user."""
    users = [
        (vertex.vertex_id, str(vertex.attributes.get("email") or ""))
        for vertex in vertices
        if vertex.vertex_type == "JiraUser" and vertex.attributes.get("email")
    ]
    return _edges(conn, users, source_is_zendesk=False)


def _edges(
    conn,
    users: list[tuple[str, str]],
    *,
    source_is_zendesk: bool,
) -> list[EdgeRecord]:
    if not users or not _installed(conn):
        return []
    target_type = "JiraUser" if source_is_zendesk else "ZendeskUser"
    edges: list[EdgeRecord] = []
    seen: set[tuple[str, str]] = set()
    for vertex_id, email in users:
        needle = str(email or "").strip().casefold()
        if not needle or '"' in needle or "\\" in needle:
            continue
        try:
            matches = _matching_ids(conn, target_type, needle)
        except Exception:
            logger.warning(
                "Could not match %s email to %s",
                "ZendeskUser" if source_is_zendesk else "JiraUser",
                target_type,
                exc_info=True,
            )
            continue
        for match_id in matches:
            zendesk_id = vertex_id if source_is_zendesk else match_id
            jira_id = match_id if source_is_zendesk else vertex_id
            key = (zendesk_id, jira_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                EdgeRecord(
                    "ZendeskUser",
                    zendesk_id,
                    ZENDESK_SAME_PERSON,
                    "JiraUser",
                    jira_id,
                )
            )
    return edges


def _installed(conn) -> bool:
    try:
        vertices = conn.getVertexTypes() or []
        edges = conn.getEdgeTypes() or []
    except Exception:
        return False
    folded_vertices = {str(name).casefold() for name in vertices}
    folded_edges = {str(name).casefold() for name in edges}
    return (
        "zendeskuser" in folded_vertices
        and "jirauser" in folded_vertices
        and ZENDESK_SAME_PERSON.casefold() in folded_edges
    )


def _matching_ids(conn, vertex_type: str, email: str) -> list[str]:
    rows = conn.getVertices(vertex_type, where=f'email="{email}"') or []
    found: list[str] = []
    for row in rows:
        if isinstance(row, str):
            found.append(row)
            continue
        vertex_id = row.get("v_id") or row.get("id")
        if vertex_id:
            found.append(str(vertex_id))
    return found
