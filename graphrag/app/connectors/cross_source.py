"""Ingestion-time cross-source links and pending references.

A missing target is recorded as one anchor plus one pending vertex per waiting
source. Resolving the target reads that anchor only. This is not a separate
relationship-discovery service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from common.db.schema_utils import SchemaProposal

CROSS_SOURCE_EDGE = "CROSS_SOURCE_LINK"
PENDING_EDGE = "PENDING_REF"
PENDING_REF_TYPE = "PendingRef"
PENDING_LINK_TYPE = "PendingLink"

_ZENDESK_URL = re.compile(
    r"https?://([a-z0-9-]+)\.zendesk\.com/\S*?/tickets/(\d+)",
    re.IGNORECASE,
)
_JIRA_URL = re.compile(
    r"https?://[^/\s]+/browse/([A-Za-z][A-Za-z0-9]+-\d+)",
    re.IGNORECASE,
)


def add_pending_reference_schema(
    proposal: SchemaProposal,
    source_vertex_type: str,
) -> None:
    """Add the shared pending types used by every connector."""
    proposal.add_vertex(
        PENDING_REF_TYPE,
        "Anchor for one missing cross-source target. It stores no source identity.",
    )
    proposal.add_vertex(
        PENDING_LINK_TYPE,
        "One waiting source for one missing target.",
        [("target_key", "STRING")],
    )
    proposal.add_edge_pair(
        PENDING_EDGE,
        PENDING_LINK_TYPE,
        PENDING_REF_TYPE,
        "Connects one waiting source to the missing target anchor.",
    )
    proposal.add_edge_pair(
        PENDING_EDGE,
        source_vertex_type,
        PENDING_REF_TYPE,
        "Waiting source edge. One source and one anchor identify the pending link.",
    )


def canonical_target(url: str) -> tuple[str, str] | None:
    """Return ``(vertex_type, vertex_id)`` for an explicit source URL.

    A Jira issue key is never rewritten into a Zendesk ticket id.
    """
    zendesk = _ZENDESK_URL.search(url or "")
    if zendesk:
        source = zendesk.group(1).lower()
        ticket_id = zendesk.group(2)
        return ("ZendeskTicket", f"zendesk:{source}:ticket:{ticket_id}")
    jira = _JIRA_URL.search(url or "")
    if jira:
        key = jira.group(1).upper()
        return ("JiraIssue", f"jira:{key.lower()}:issue")
    return None


def extract_targets(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _ZENDESK_URL.finditer(text or ""):
        target = (
            "ZendeskTicket",
            f"zendesk:{match.group(1).lower()}:ticket:{match.group(2)}",
        )
        if target not in seen:
            seen.add(target)
            found.append(target)
    for match in _JIRA_URL.finditer(text or ""):
        key = match.group(1).upper()
        target = ("JiraIssue", f"jira:{key.lower()}:issue")
        if target not in seen:
            seen.add(target)
            found.append(target)
    return found


def anchor_id(target_id: str) -> str:
    return f"ref:{target_id}"


def pending_id(source_type: str, source_id: str, target_id: str) -> str:
    return f"pending|{source_type}|{source_id}|{target_id}"


def parse_pending_id(vertex_id: str) -> tuple[str, str, str] | None:
    parts = vertex_id.split("|", 3)
    if len(parts) != 4 or parts[0] != "pending":
        return None
    return parts[1], parts[2], parts[3]


@dataclass
class LinkGraph:
    """Small graph surface used by the link operations."""

    vertices: dict[tuple[str, str], dict] = field(default_factory=dict)
    edges: set[tuple[str, str, str, str, str]] = field(default_factory=set)
    cross_edge_installed: bool = True

    def has_edge_type(self, edge_type: str) -> bool:
        if edge_type == CROSS_SOURCE_EDGE:
            return self.cross_edge_installed
        return True

    def vertex_exists(self, vertex_type: str, vertex_id: str) -> bool:
        return (vertex_type, vertex_id) in self.vertices

    def upsert_vertex(
        self,
        vertex_type: str,
        vertex_id: str,
        attributes: dict | None = None,
        *,
        overwrite: bool = False,
    ) -> None:
        key = (vertex_type, vertex_id)
        if key in self.vertices and not overwrite:
            return
        self.vertices[key] = dict(attributes or {})

    def upsert_edge(
        self,
        source_type: str,
        source_id: str,
        edge_type: str,
        target_type: str,
        target_id: str,
    ) -> None:
        self.edges.add(
            (source_type, source_id, edge_type, target_type, target_id)
        )

    def incoming(self, vertex_type: str, vertex_id: str, edge_type: str) -> list[str]:
        return [
            source_id
            for source_type, source_id, edge, target_type, target_id in self.edges
            if edge == edge_type
            and target_type == vertex_type
            and target_id == vertex_id
            and source_type == PENDING_LINK_TYPE
        ]

    def delete_edge(
        self,
        source_type: str,
        source_id: str,
        edge_type: str,
        target_type: str,
        target_id: str,
    ) -> None:
        self.edges.discard(
            (source_type, source_id, edge_type, target_type, target_id)
        )

    def delete_vertex(self, vertex_type: str, vertex_id: str) -> None:
        self.vertices.pop((vertex_type, vertex_id), None)
        self.edges = {
            edge
            for edge in self.edges
            if (edge[0], edge[1]) != (vertex_type, vertex_id)
            and (edge[3], edge[4]) != (vertex_type, vertex_id)
        }


def _missing_vertex(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not a valid vertex id" in text or "error 601" in text


class TigerGraphLinkGraph:
    """Adapter over the connection methods the Jira sync already uses."""

    def __init__(self, conn):
        self.conn = conn

    def has_vertex_type(self, vertex_type: str) -> bool:
        cached = getattr(self, "_vertex_types", None)
        if cached is None:
            try:
                names = self.conn.getVertexTypes() or []
            except Exception:
                names = []
            cached = {str(name).casefold() for name in names}
            self._vertex_types = cached
        return vertex_type.casefold() in cached

    def has_edge_type(self, edge_type: str) -> bool:
        cached = getattr(self, "_edge_types", None)
        if cached is None:
            try:
                names = self.conn.getEdgeTypes() or []
            except Exception:
                names = []
            cached = {str(name).casefold() for name in names}
            self._edge_types = cached
        return edge_type.casefold() in cached

    def vertex_exists(self, vertex_type: str, vertex_id: str) -> bool:
        # A connector installed by itself will not have the other connector's
        # vertex type. Treat that as a missing target; never issue getEdges
        # with an invalid source vertex type because TigerGraph returns 30000.
        if not self.has_vertex_type(vertex_type):
            return False
        try:
            self.conn.getEdges(vertex_type, vertex_id, PENDING_EDGE)
        except Exception as exc:
            if _missing_vertex(exc):
                return False
            raise
        return True

    def upsert_vertex(
        self,
        vertex_type: str,
        vertex_id: str,
        attributes: dict | None = None,
        *,
        overwrite: bool = False,
    ) -> None:
        if not overwrite and self.vertex_exists(vertex_type, vertex_id):
            return
        self.conn.upsertVertex(vertex_type, vertex_id, attributes or {})

    def upsert_edge(
        self,
        source_type: str,
        source_id: str,
        edge_type: str,
        target_type: str,
        target_id: str,
    ) -> None:
        import json

        from connectors.jira.mapper import EdgeRecord
        from connectors.jira.sync import _upsert_payload

        payload = _upsert_payload(
            [],
            [
                EdgeRecord(
                    source_type,
                    source_id,
                    edge_type,
                    target_type,
                    target_id,
                )
            ],
        )
        self.conn.upsertData(json.dumps(payload))

    def incoming(self, vertex_type: str, vertex_id: str, edge_type: str) -> list[str]:
        try:
            edges = self.conn.getEdges(
                vertex_type,
                vertex_id,
                f"reverse_{edge_type}",
            ) or []
        except Exception as exc:
            if _missing_vertex(exc):
                return []
            raise
        return [
            str(edge.get("to_id"))
            for edge in edges
            if str(edge.get("to_id") or "").startswith("pending|")
        ]

    def delete_edge(
        self,
        source_type: str,
        source_id: str,
        edge_type: str,
        target_type: str,
        target_id: str,
    ) -> None:
        try:
            self.conn.delEdges(
                source_type,
                source_id,
                edge_type,
                target_type,
                target_id,
            )
        except Exception as exc:
            if not _missing_vertex(exc):
                raise

    def delete_vertex(self, vertex_type: str, vertex_id: str) -> None:
        try:
            self.conn.delVerticesById(vertex_type, [vertex_id])
        except Exception as exc:
            if not _missing_vertex(exc):
                raise


def link_reference(
    graph,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
) -> None:
    """Create the cross-source edge, or one pending link if the target is absent.

    A reference inside the same vertex type is not a cross-source link. Jira
    already stores those with its own edges. The cross-source edge is written
    only when that edge type is installed.
    """
    if source_type == target_type:
        return
    if graph.vertex_exists(target_type, target_id):
        _resolve_one(graph, source_type, source_id, target_type, target_id)
        return
    anchor = anchor_id(target_id)
    pending = pending_id(source_type, source_id, target_id)
    graph.upsert_vertex(PENDING_REF_TYPE, anchor, overwrite=False)
    graph.upsert_vertex(
        PENDING_LINK_TYPE,
        pending,
        {"target_key": target_id},
        overwrite=True,
    )
    graph.upsert_edge(
        PENDING_LINK_TYPE,
        pending,
        PENDING_EDGE,
        PENDING_REF_TYPE,
        anchor,
    )
    graph.upsert_edge(
        source_type,
        source_id,
        PENDING_EDGE,
        PENDING_REF_TYPE,
        anchor,
    )
    if graph.vertex_exists(target_type, target_id):
        _resolve_one(graph, source_type, source_id, target_type, target_id)


def resolve_target(graph, target_type: str, target_id: str) -> None:
    """Create edges for every source waiting on this target, then remove them."""
    anchor = anchor_id(target_id)
    if not graph.vertex_exists(PENDING_REF_TYPE, anchor):
        return
    waiting = list(graph.incoming(PENDING_REF_TYPE, anchor, PENDING_EDGE))
    for pending in waiting:
        parsed = parse_pending_id(pending)
        if parsed is None:
            continue
        source_type, source_id, pending_target = parsed
        if pending_target != target_id:
            continue
        _resolve_one(graph, source_type, source_id, target_type, target_id)
    if not graph.incoming(PENDING_REF_TYPE, anchor, PENDING_EDGE):
        graph.delete_vertex(PENDING_REF_TYPE, anchor)


def link_text(graph, source_type: str, source_id: str, text: str) -> None:
    for target_type, target_id in extract_targets(text):
        link_reference(graph, source_type, source_id, target_type, target_id)


def _resolve_one(graph, source_type, source_id, target_type, target_id) -> None:
    if not graph.has_edge_type(CROSS_SOURCE_EDGE):
        return
    graph.upsert_edge(
        source_type,
        source_id,
        CROSS_SOURCE_EDGE,
        target_type,
        target_id,
    )
    pending = pending_id(source_type, source_id, target_id)
    graph.delete_vertex(PENDING_LINK_TYPE, pending)
    graph.delete_edge(
        source_type,
        source_id,
        PENDING_EDGE,
        PENDING_REF_TYPE,
        anchor_id(target_id),
    )
