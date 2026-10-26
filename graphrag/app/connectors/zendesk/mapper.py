"""Map Zendesk ticket payloads to graph records and searchable text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from common.chunkers.structured import StructuredChunker
from common.config import get_graphrag_config
from connectors.records import EdgeRecord, VertexRecord

from .config import ZendeskDataSource
from .schema import ZENDESK_COMMENT_ON, ZENDESK_REQUESTED_BY


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


def user_vertex_id(subdomain: str, user_id: str) -> str:
    return f"zendesk:{subdomain}:user:{user_id}"


def comment_vertex_id(subdomain: str, comment_id: str) -> str:
    return f"zendesk:{subdomain}:comment:{comment_id}"


def jira_issue_vertex_id(issue_key: str) -> str:
    return f"jira:{issue_key.lower()}:issue"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        users_by_id: dict[str, str],
    ) -> MappedTicket:
        ticket_id = str(ticket.get("id"))
        vertex_id = ticket_vertex_id(self.subdomain, ticket_id)
        subject = str(ticket.get("subject") or "")
        description = str(ticket.get("description") or "")
        status = str(ticket.get("status") or "")
        url = str(ticket.get("url") or "") or (
            f"https://{self.subdomain}.zendesk.com/agent/tickets/{ticket_id}"
        )
        vertices = [
            VertexRecord(
                "ZendeskTicket",
                vertex_id,
                {
                    "ticket_id": ticket_id,
                    "subject": subject,
                    "status": status,
                    "url": url,
                },
            )
        ]
        edges: list[EdgeRecord] = []
        requester_id = ticket.get("requester_id")
        if requester_id is not None and str(requester_id).strip():
            requester = str(requester_id)
            user_id = user_vertex_id(self.subdomain, requester)
            vertices.append(
                VertexRecord(
                    "ZendeskUser",
                    user_id,
                    {
                        "user_id": requester,
                        "display_name": users_by_id.get(requester, ""),
                    },
                )
            )
            edges.append(
                EdgeRecord(
                    "ZendeskTicket",
                    vertex_id,
                    ZENDESK_REQUESTED_BY,
                    "ZendeskUser",
                    user_id,
                )
            )

        mapped_comments: list[MappedComment] = []
        for comment in comments:
            body = str(comment.get("body") or "").strip()
            comment_id = comment.get("id")
            if not body or comment_id is None:
                continue
            comment_key = str(comment_id)
            comment_vertex = comment_vertex_id(self.subdomain, comment_key)
            author_id = comment.get("author_id")
            author_name = ""
            if author_id is not None and str(author_id).strip():
                author = str(author_id)
                author_name = users_by_id.get(author, "")
                author_vertex = user_vertex_id(self.subdomain, author)
                vertices.append(
                    VertexRecord(
                        "ZendeskUser",
                        author_vertex,
                        {
                            "user_id": author,
                            "display_name": author_name,
                        },
                    )
                )
            edges.append(
                EdgeRecord(
                    "ZendeskComment",
                    comment_vertex,
                    ZENDESK_COMMENT_ON,
                    "ZendeskTicket",
                    vertex_id,
                )
            )
            comment_text = "\n".join(
                part for part in (ticket_id, author_name, body) if part
            )
            comment_hash = _content_hash(comment_text)
            vertices.append(
                VertexRecord(
                    "ZendeskComment",
                    comment_vertex,
                    {
                        "comment_id": comment_key,
                        "body": body,
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

        ticket_text = "\n".join(
            part for part in (ticket_id, subject, description, url) if part
        )
        ticket_hash = _content_hash(ticket_text)
        ticket_vertex = vertices[0]
        ticket_attributes = dict(ticket_vertex.attributes)
        ticket_attributes["content_hash"] = ticket_hash
        vertices[0] = VertexRecord(
            ticket_vertex.vertex_type,
            ticket_vertex.vertex_id,
            ticket_attributes,
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
