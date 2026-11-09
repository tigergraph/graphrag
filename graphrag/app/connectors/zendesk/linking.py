"""Write links_issue from Zendesk's Jira integration inventory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from connectors.records import EdgeRecord, VertexRecord
from connectors.upsert import upsert_payload

from .mapper import jira_issue_vertex_id, ticket_vertex_id
from .schema import LINKS_ISSUE

_JIRA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]+-\d+$")
_TICKET_ID = re.compile(r"^\d+$")


@dataclass
class LinkReconciliationResult:
    links_seen: int = 0
    links_written: int = 0
    links_skipped: int = 0
    linked_ticket_ids: list[str] = field(default_factory=list)


def normalized_link(link: dict[str, Any]) -> tuple[str, str] | None:
    """Return (issue_key, ticket_id) or None when the row is not a relation."""
    issue_key = str(link.get("issue_key") or "").strip()
    ticket_id = str(link.get("ticket_id") or "").strip()
    if not _JIRA_KEY.fullmatch(issue_key) or not _TICKET_ID.fullmatch(ticket_id):
        return None
    return issue_key, ticket_id


def _vertex_exists(conn, vertex_type: str, vertex_id: str) -> bool:
    try:
        found = conn.getVerticesById(vertex_type, [vertex_id])
    except Exception as exc:
        message = str(exc).lower()
        if "not a valid vertex" in message or "does not exist" in message:
            return False
        raise
    if isinstance(found, dict):
        return bool(found)
    return bool(found)


def _has_type(conn, method: str, name: str) -> bool:
    try:
        names = getattr(conn, method)() or []
    except Exception:
        return False
    folded = name.casefold()
    return any(str(item).casefold() == folded for item in names)


def _edge_targets(conn, ticket_vertex_id_value: str) -> set[str]:
    try:
        edges = conn.getEdges(
            "ZendeskTicket",
            ticket_vertex_id_value,
            LINKS_ISSUE,
        ) or []
    except Exception as exc:
        if "is not a valid vertex id" in str(exc):
            return set()
        raise
    return {
        str(edge.get("to_id"))
        for edge in edges
        if edge.get("to_id") is not None
    }


def _replace_edges(conn, ticket_vertex: str, issue_vertices: set[str]) -> None:
    current = _edge_targets(conn, ticket_vertex)
    if current == issue_vertices:
        return
    if current:
        try:
            conn.delEdges("ZendeskTicket", ticket_vertex, LINKS_ISSUE)
        except Exception as exc:
            if "is not a valid vertex id" not in str(exc):
                raise
    if not issue_vertices:
        return
    edges = [
        EdgeRecord(
            "ZendeskTicket",
            ticket_vertex,
            LINKS_ISSUE,
            "JiraIssue",
            issue_vertex,
        )
        for issue_vertex in sorted(issue_vertices)
    ]
    result = conn.upsertData(json.dumps(upsert_payload([], edges)))
    if isinstance(result, dict) and result.get("skipped_edges"):
        raise RuntimeError("TigerGraph rejected a Zendesk link edge")


def _ticket_attributes(conn, ticket_vertex: str) -> dict[str, Any]:
    try:
        found = conn.getVerticesById("ZendeskTicket", [ticket_vertex]) or []
    except Exception:
        return {}
    for row in found:
        if isinstance(row, dict):
            attributes = row.get("attributes") or {}
            if str(row.get("v_id") or row.get("id") or "") == ticket_vertex or attributes:
                return attributes
    return {}


def _public_comment_text(conn, ticket_id: str) -> str:
    try:
        rows = conn.getVertices("ZendeskComment", where=f'ticket_id="{ticket_id}"') or []
    except Exception:
        return ""
    bodies: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attributes = row.get("attributes") or {}
        if str(attributes.get("visibility") or "public") != "public":
            continue
        body = str(attributes.get("body") or "").strip()
        if body:
            bodies.append(body)
    return "\n".join(bodies)


def _ensure_issue_vertex(conn, issue_key: str, ticket_vertex: str, ticket_id: str) -> str:
    """Return the Jira vertex id, creating it when it is missing.

    An existing vertex is left unchanged. A new vertex gets issue_key, summary
    from the ticket subject, and description from the ticket description plus
    the public comments.
    """
    issue_vertex = jira_issue_vertex_id(issue_key)
    if _vertex_exists(conn, "JiraIssue", issue_vertex):
        return issue_vertex
    ticket_attributes = _ticket_attributes(conn, ticket_vertex)
    description = str(ticket_attributes.get("description") or "").strip()
    public_comments = _public_comment_text(conn, ticket_id)
    if public_comments:
        description = "\n".join(part for part in (description, public_comments) if part)
    conn.upsertData(
        json.dumps(
            upsert_payload(
                [
                    VertexRecord(
                        "JiraIssue",
                        issue_vertex,
                        {
                            "issue_key": issue_key,
                            "summary": str(ticket_attributes.get("subject") or ""),
                            "description": description,
                        },
                    )
                ],
                [],
            )
        )
    )
    return issue_vertex


def reconcile_links(
    conn,
    subdomain: str,
    pages,
    previously_linked: list[str],
) -> LinkReconciliationResult:
    """Write a link for every inventory row whose Zendesk ticket is in the graph.

    A missing Jira issue is created with issue_key only, and the edge is
    written in the same pass. Inventory rows for one ticket replace that
    ticket's link edges. A ticket that was linked on the previous run and is
    absent from this inventory has its link edges removed. The Jira vertex
    is not deleted.
    """
    if not _has_type(conn, "getVertexTypes", "JiraIssue"):
        return LinkReconciliationResult()
    if not _has_type(conn, "getEdgeTypes", LINKS_ISSUE):
        raise RuntimeError(
            "links_issue is not installed. Install the Zendesk schema again "
            "after the Jira schema."
        )

    by_ticket: dict[str, set[str]] = {}
    result = LinkReconciliationResult()
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for raw_link in page:
            normalized = normalized_link(raw_link)
            if normalized is None or normalized in seen:
                result.links_skipped += 1
                continue
            seen.add(normalized)
            result.links_seen += 1
            issue_key, ticket_id = normalized
            by_ticket.setdefault(ticket_id, set()).add(issue_key)

    touched = set(by_ticket) | {
        ticket_id
        for ticket_id in previously_linked
        if _TICKET_ID.fullmatch(str(ticket_id) or "")
    }
    linked: list[str] = []
    for ticket_id in sorted(touched):
        ticket_vertex = ticket_vertex_id(subdomain, ticket_id)
        if not _vertex_exists(conn, "ZendeskTicket", ticket_vertex):
            result.links_skipped += len(by_ticket.get(ticket_id, ()))
            continue
        issue_vertices: set[str] = set()
        for issue_key in by_ticket.get(ticket_id, ()):
            issue_vertices.add(
                _ensure_issue_vertex(conn, issue_key, ticket_vertex, ticket_id)
            )
            result.links_written += 1
        _replace_edges(conn, ticket_vertex, issue_vertices)
        if issue_vertices:
            linked.append(ticket_id)
    result.linked_ticket_ids = linked
    return result
