"""Map Zendesk ticket payloads to graph records and searchable text."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from common.chunkers.structured import StructuredChunker
from common.config import get_graphrag_config
from connectors.records import EdgeRecord, VertexRecord

from .config import ZendeskDataSource
from .schema import ASSIGNED, AUTHORED, CHANGED, MEMBER, REQUESTED

_CHANGE_FIELDS = {
    "status": "status",
    "priority": "priority",
    "assignee_id": "assignee",
}


def _user_name_email(value) -> tuple[str, str, str]:
    if isinstance(value, dict):
        return (
            str(value.get("name") or ""),
            str(value.get("email") or "").strip().lower(),
            str(value.get("organization_id") or ""),
        )
    return str(value or ""), "", ""


def _user_role(value) -> str:
    if isinstance(value, dict):
        return str(value.get("role") or "")
    return ""


def _source_ticket_id(ticket: dict) -> str:
    via = ticket.get("via")
    if not isinstance(via, dict):
        return ""
    source = via.get("source")
    if not isinstance(source, dict):
        return ""
    origin = source.get("from")
    if not isinstance(origin, dict) or not origin.get("ticket_id"):
        return ""
    return str(origin.get("ticket_id"))


def _followup_ids(ticket: dict) -> str:
    return ", ".join(
        str(item) for item in (ticket.get("followup_ids") or []) if item
    )


@dataclass
class MappedChunk:
    chunk_id: str
    index: int
    text: str


@dataclass
class MappedComment:
    comment_id: str
    vertex_id: str
    content_hash: str
    chunks: list[MappedChunk] = field(default_factory=list)


@dataclass
class MappedTicket:
    ticket_id: str
    vertex_id: str
    content_hash: str
    vertices: list[VertexRecord]
    edges: list[EdgeRecord]
    ticket_chunks: list[MappedChunk]
    comments: list[MappedComment]


def ticket_vertex_id(subdomain: str, ticket_id: str) -> str:
    return f"zendesk:{subdomain}:ticket:{ticket_id}"


def person_vertex_id(subdomain: str, user_id: str, email: str) -> str:
    if email:
        return f"person:{email}"
    return f"zendesk:{subdomain}:user:{user_id}"


def comment_vertex_id(subdomain: str, comment_id: str) -> str:
    return f"zendesk:{subdomain}:comment:{comment_id}"


def organization_vertex_id(subdomain: str, organization_id: str, domain: str) -> str:
    if domain:
        return f"organization:{domain.lower()}"
    return f"zendesk:{subdomain}:org:{organization_id}"


def jira_issue_vertex_id(issue_key: str) -> str:
    return f"jira:{issue_key.lower()}:issue"


def _content_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _custom_fields(ticket: dict) -> str:
    parts: list[str] = []
    for row in ticket.get("custom_fields") or []:
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        if value in (None, "", []):
            continue
        name = row.get("name") or row.get("id")
        parts.append(f"{name}: {value}")
    return ", ".join(parts)


class ZendeskMapper:
    def __init__(self, source: ZendeskDataSource, graphname: str | None = None):
        self.source = source
        self.subdomain = source.connection.subdomain
        chunker_config = get_graphrag_config(graphname).get("chunker_config", {})
        self.chunker = StructuredChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )

    def map_ticket(
        self,
        ticket: dict,
        comments: list[dict],
        users_by_id: dict[str, str | dict],
        organizations_by_id: dict[str, str | dict] | None = None,
        groups_by_id: dict[str, str] | None = None,
        audits: list[dict] | None = None,
    ) -> MappedTicket:
        organizations_by_id = organizations_by_id or {}
        groups_by_id = groups_by_id or {}
        ticket_id = str(ticket.get("id"))
        vertex_id = ticket_vertex_id(self.subdomain, ticket_id)
        subject = str(ticket.get("subject") or "")
        description = str(ticket.get("description") or "")
        status = str(ticket.get("status") or "")
        priority = str(ticket.get("priority") or "")
        ticket_type = str(ticket.get("type") or "")
        tags = ", ".join(str(tag) for tag in (ticket.get("tags") or []) if tag)
        problem_id = str(ticket.get("problem_id") or "")
        followup_ids = _followup_ids(ticket)
        source_ticket_id = _source_ticket_id(ticket)
        group_id = ticket.get("group_id")
        group_name = (
            groups_by_id.get(str(group_id), "") if group_id is not None else ""
        )
        url = str(ticket.get("url") or "") or (
            f"https://{self.subdomain}.zendesk.com/agent/tickets/{ticket_id}"
        )
        created = str(ticket.get("created_at") or "")
        updated = str(ticket.get("updated_at") or "")
        vertices = [
            VertexRecord(
                "ZendeskTicket",
                vertex_id,
                {
                    "ticket_id": ticket_id,
                    "ticket_type": ticket_type,
                    "subject": subject,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "tags": tags,
                    "custom_fields": _custom_fields(ticket),
                    "problem_id": problem_id,
                    "followup_ids": followup_ids,
                    "source_ticket_id": source_ticket_id,
                    "group_name": group_name,
                    "url": url,
                    "created": created,
                    "updated": updated,
                },
            )
        ]
        edges: list[EdgeRecord] = []
        self._add_requester(
            vertices,
            edges,
            ticket,
            users_by_id,
            organizations_by_id,
            vertex_id,
        )
        self._add_assignee(vertices, edges, ticket, users_by_id, vertex_id)

        mapped_comments: list[MappedComment] = []
        comment_bodies: list[str] = []
        for comment in comments:
            body = str(comment.get("body") or "").strip()
            comment_id = comment.get("id")
            if not body or comment_id is None:
                continue
            comment_key = str(comment_id)
            comment_bodies.append(body)
            comment_vertex = comment_vertex_id(self.subdomain, comment_key)
            visibility = "public" if comment.get("public", True) else "internal"
            author_id = comment.get("author_id")
            author_name = ""
            author_role = ""
            created = str(comment.get("created_at") or "")
            if author_id is not None and str(author_id).strip():
                author = str(author_id)
                author_name, _email, _org = _user_name_email(users_by_id.get(author))
                author_role = _user_role(users_by_id.get(author))
                person_id = self._add_person(vertices, author, users_by_id)
                edges.append(
                    EdgeRecord(
                        "ZendeskComment",
                        comment_vertex,
                        AUTHORED,
                        "Person",
                        person_id,
                    )
                )
            comment_text = "\n".join(
                part for part in (ticket_id, author_name, body) if part
            )
            comment_hash = _content_hash(
                {"ticket_id": ticket_id, "body": body, "visibility": visibility}
            )
            vertices.append(
                VertexRecord(
                    "ZendeskComment",
                    comment_vertex,
                    {
                        "comment_id": comment_key,
                        "ticket_id": ticket_id,
                        "body": body,
                        "visibility": visibility,
                        "author_role": author_role,
                        "created": created,
                        "content_hash": comment_hash,
                    },
                )
            )
            mapped_comments.append(
                MappedComment(
                    comment_id=comment_key,
                    vertex_id=comment_vertex,
                    content_hash=comment_hash,
                    chunks=self._chunks(comment_vertex, comment_text),
                )
            )
            self._add_attachments(vertices, comment, ticket_id)

        self._add_changes(vertices, edges, ticket_id, audits or [], users_by_id)

        ticket_hash = _content_hash(
            {
                "subject": subject,
                "description": description,
                "status": status,
                "priority": priority,
                "ticket_type": ticket_type,
                "tags": tags,
                "comments": comment_bodies,
            }
        )
        ticket_vertex = vertices[0]
        ticket_attributes = dict(ticket_vertex.attributes)
        ticket_attributes["content_hash"] = ticket_hash
        vertices[0] = VertexRecord(
            ticket_vertex.vertex_type,
            ticket_vertex.vertex_id,
            ticket_attributes,
        )
        ticket_text = "\n".join(
            part for part in (ticket_id, subject, description, url) if part
        )
        return MappedTicket(
            ticket_id=ticket_id,
            vertex_id=vertex_id,
            content_hash=ticket_hash,
            vertices=vertices,
            edges=edges,
            ticket_chunks=self._chunks(vertex_id, ticket_text),
            comments=mapped_comments,
        )

    def _add_person(
        self,
        vertices: list[VertexRecord],
        user_id: str,
        users_by_id: dict,
    ) -> str:
        name, email, _org = _user_name_email(users_by_id.get(user_id))
        person_id = person_vertex_id(self.subdomain, user_id, email)
        attributes = {"display_name": name}
        if email:
            attributes["email"] = email
        vertices.append(VertexRecord("Person", person_id, attributes))
        return person_id

    def _add_requester(
        self,
        vertices,
        edges,
        ticket,
        users_by_id,
        organizations_by_id,
        ticket_vertex_id_value,
    ) -> None:
        requester_id = ticket.get("requester_id")
        if requester_id is None or not str(requester_id).strip():
            return
        requester = str(requester_id)
        person_id = self._add_person(vertices, requester, users_by_id)
        edges.append(
            EdgeRecord(
                "Person",
                person_id,
                REQUESTED,
                "ZendeskTicket",
                ticket_vertex_id_value,
            )
        )
        _name, _email, user_org = _user_name_email(users_by_id.get(requester))
        organization_id = ticket.get("organization_id")
        if not user_org or organization_id is None or user_org != str(organization_id):
            return
        org = organizations_by_id.get(str(organization_id), {})
        if isinstance(org, str):
            org = {"name": org, "domain": ""}
        domain = str(org.get("domain") or "").strip().lower()
        org_vertex = organization_vertex_id(
            self.subdomain, str(organization_id), domain
        )
        vertices.append(
            VertexRecord(
                "Organization",
                org_vertex,
                {"name": str(org.get("name") or ""), "domain": domain},
            )
        )
        edges.append(
            EdgeRecord("Organization", org_vertex, MEMBER, "Person", person_id)
        )

    def _add_assignee(
        self,
        vertices,
        edges,
        ticket,
        users_by_id,
        ticket_vertex_id_value,
    ) -> None:
        assignee_id = ticket.get("assignee_id")
        if assignee_id is None or not str(assignee_id).strip():
            return
        assignee = str(assignee_id)
        person_id = self._add_person(vertices, assignee, users_by_id)
        edges.append(
            EdgeRecord(
                "Person",
                person_id,
                ASSIGNED,
                "ZendeskTicket",
                ticket_vertex_id_value,
            )
        )

    def _add_attachments(self, vertices, comment: dict, ticket_id: str) -> None:
        for attachment in comment.get("attachments") or []:
            if not isinstance(attachment, dict) or attachment.get("id") is None:
                continue
            vertices.append(
                VertexRecord(
                    "Attachment",
                    f"zendesk:{self.subdomain}:attachment:{attachment['id']}",
                    {
                        "file_name": str(attachment.get("file_name") or ""),
                        "content_url": str(attachment.get("content_url") or ""),
                        "parent_id": ticket_id,
                    },
                )
            )

    def _add_changes(self, vertices, edges, ticket_id, audits, users_by_id) -> None:
        for audit in audits:
            audit_id = audit.get("id")
            if audit_id is None:
                continue
            author_id = audit.get("author_id")
            created = str(audit.get("created_at") or "")
            for event in audit.get("events") or []:
                if not isinstance(event, dict):
                    continue
                field_name = _CHANGE_FIELDS.get(str(event.get("field_name") or ""))
                if field_name is None:
                    continue
                change_id = f"{audit_id}:{field_name}"
                change_vertex = (
                    f"zendesk:{self.subdomain}:change:{audit_id}:{field_name}"
                )
                vertices.append(
                    VertexRecord(
                        "ZendeskChange",
                        change_vertex,
                        {
                            "change_id": change_id,
                            "ticket_id": ticket_id,
                            "field": field_name,
                            "from_value": str(event.get("previous_value") or ""),
                            "to_value": str(event.get("value") or ""),
                            "created": created,
                        },
                    )
                )
                if author_id is not None and str(author_id).strip():
                    person_id = self._add_person(
                        vertices, str(author_id), users_by_id
                    )
                    edges.append(
                        EdgeRecord(
                            "ZendeskChange",
                            change_vertex,
                            CHANGED,
                            "Person",
                            person_id,
                        )
                    )

    def _chunks(self, vertex_id: str, text: str) -> list[MappedChunk]:
        chunks: list[MappedChunk] = []
        for index, piece in enumerate(self.chunker.chunk(text)):
            chunk_text = str(piece).strip()
            if not chunk_text:
                continue
            digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:12]
            chunks.append(
                MappedChunk(
                    chunk_id=f"{vertex_id}:chunk:{index}:{digest}",
                    index=index,
                    text=chunk_text,
                )
            )
        return chunks
