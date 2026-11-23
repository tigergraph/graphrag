"""Map one Confluence page payload to graph records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from connectors.records import EdgeRecord, VertexRecord

from .config import ConfluenceDataSource
from .html import storage_to_markdown
from .links import issue_keys, zendesk_tickets
from .schema import AUTHORED, EDITED, OWNED

CHUNK_SIZE = 1200


@dataclass
class MappedChunk:
    chunk_id: str
    index: int
    text: str


@dataclass
class MappedComment:
    vertex_id: str
    content_hash: str
    body: str
    chunks: list[MappedChunk] = field(default_factory=list)
    vertex: VertexRecord | None = None
    people: list[VertexRecord] = field(default_factory=list)
    edges: list[EdgeRecord] = field(default_factory=list)


@dataclass
class MappedPage:
    vertex_id: str
    page_id: str
    content_type: str
    updated: str
    content_hash: str
    vertices: list[VertexRecord]
    edges: list[EdgeRecord]
    page_chunks: list[MappedChunk]
    comments: list[MappedComment]
    issue_keys: list[str]
    zendesk_tickets: list[tuple[str, str]]


def page_vertex_id(site: str, content_type: str, page_id: str) -> str:
    kind = "blogpost" if content_type == "blogpost" else "page"
    return f"confluence:{site}:{kind}:{page_id}"


def person_vertex_id(site: str, email: str | None, account_id: str | None) -> str | None:
    if email:
        return f"person:{email.strip().lower()}"
    if account_id:
        return f"confluence:{site}:user:{account_id}"
    return None


def jira_issue_vertex_id(issue_key: str) -> str:
    return f"jira:{issue_key.lower()}:issue"


def zendesk_ticket_vertex_id(subdomain: str, ticket_id: str) -> str:
    return f"zendesk:{subdomain}:ticket:{ticket_id}"


def _storage(body: Any) -> str:
    if isinstance(body, dict):
        storage = body.get("storage")
        if isinstance(storage, dict):
            return str(storage.get("value") or "")
        if body.get("value"):
            return str(body.get("value") or "")
    return str(body or "")


def _chunks(site: str, prefix: str, text: str) -> list[MappedChunk]:
    cleaned = text.strip()
    if not cleaned:
        return []
    pieces: list[str] = []
    current = ""
    for paragraph in cleaned.split("\n"):
        if current and len(current) + len(paragraph) + 1 > CHUNK_SIZE:
            pieces.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}".strip()
    if current:
        pieces.append(current)
    return [
        MappedChunk(f"confluence:{site}:chunk:{prefix}:{index}", index, piece)
        for index, piece in enumerate(pieces)
    ]


def _hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class ConfluenceMapper:
    def __init__(self, source: ConfluenceDataSource):
        self.site = source.connection.site

    def map_page(
        self,
        content: dict[str, Any],
        *,
        content_type: str,
        comments: list[dict[str, Any]],
        labels: list[str],
        attachments: list[dict[str, Any]],
        users: dict[str, dict[str, Any]],
        space_key: str,
        space_name: str,
    ) -> MappedPage:
        page_id = str(content.get("id") or "")
        title = str(content.get("title") or "")
        body = storage_to_markdown(_storage(content.get("body")))
        status = str(content.get("status") or "")
        version = content.get("version") or {}
        created = str(content.get("createdAt") or "")
        updated = str(version.get("createdAt") or "")
        label_text = ", ".join(label for label in labels if label)
        vertex_id = page_vertex_id(self.site, content_type, page_id)
        mapped_comments = [
            self._comment(page_id, comment, users) for comment in comments
        ]
        mapped_comments = [comment for comment in mapped_comments if comment.body.strip()]
        content_hash = _hash(
            title,
            body,
            status,
            label_text,
            *[comment.body for comment in mapped_comments],
        )
        vertices = [
            VertexRecord(
                "ConfluencePage",
                vertex_id,
                {
                    "page_id": page_id,
                    "content_type": content_type,
                    "title": title,
                    "body": body,
                    "status": status,
                    "space_key": space_key,
                    "space_name": space_name,
                    "parent_id": str(content.get("parentId") or ""),
                    "labels": label_text,
                    "version_number": str(version.get("number") or ""),
                    "url": self._url(space_key, content_type, page_id),
                    "created": created,
                    "updated": updated,
                    "content_hash": content_hash,
                },
            )
        ]
        edges: list[EdgeRecord] = []
        self._add_person(
            vertices, edges, "ConfluencePage", vertex_id,
            str(content.get("authorId") or ""), users, AUTHORED,
        )
        self._add_person(
            vertices, edges, "ConfluencePage", vertex_id,
            str(version.get("authorId") or ""), users, EDITED,
        )
        self._add_person(
            vertices, edges, "ConfluencePage", vertex_id,
            str(content.get("ownerId") or ""), users, OWNED,
        )
        for comment in mapped_comments:
            if comment.vertex is not None:
                vertices.append(comment.vertex)
            vertices.extend(comment.people)
            edges.extend(comment.edges)
        for attachment in attachments:
            attachment_id = str(attachment.get("id") or "")
            if not attachment_id:
                continue
            vertices.append(
                VertexRecord(
                    "Attachment",
                    f"confluence:{self.site}:attachment:{attachment_id}",
                    {
                        "file_name": str(attachment.get("title") or ""),
                        "content_url": self._attachment_url(attachment),
                        "parent_id": page_id,
                    },
                )
            )
        texts = [title, body, *[comment.body for comment in mapped_comments]]
        page_text = "\n".join(
            part for part in (f"Title: {title}", f"Body:\n{body}" if body else "") if part
        )
        return MappedPage(
            vertex_id=vertex_id,
            page_id=page_id,
            content_type=content_type,
            updated=updated,
            content_hash=content_hash,
            vertices=vertices,
            edges=edges,
            page_chunks=_chunks(self.site, f"{content_type}:{page_id}", page_text),
            comments=mapped_comments,
            issue_keys=issue_keys(*texts),
            zendesk_tickets=zendesk_tickets(*texts),
        )

    def _comment(
        self,
        page_id: str,
        comment: dict[str, Any],
        users: dict[str, dict[str, Any]],
    ) -> MappedComment:
        body = storage_to_markdown(_storage(comment.get("body")))
        comment_id = str(comment.get("id") or "")
        version = comment.get("version") or {}
        vertex_id = f"confluence:{self.site}:comment:{comment_id}"
        record = VertexRecord(
            "ConfluenceComment",
            vertex_id,
            {
                "comment_id": comment_id,
                "page_id": page_id,
                "body": body,
                "comment_type": str(comment.get("comment_type") or "footer"),
                "resolution": str(comment.get("resolutionStatus") or ""),
                "created": str(version.get("createdAt") or ""),
                "content_hash": _hash(body),
            },
        )
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        self._add_person(
            vertices,
            edges,
            "ConfluenceComment",
            vertex_id,
            str(version.get("authorId") or ""),
            users,
            AUTHORED,
        )
        return MappedComment(
            vertex_id=vertex_id,
            content_hash=_hash(body),
            body=body,
            chunks=_chunks(self.site, f"comment:{comment_id}", body),
            vertex=record,
            people=vertices,
            edges=edges,
        )

    def _add_person(
        self,
        vertices: list[VertexRecord],
        edges: list[EdgeRecord],
        source_type: str,
        source_id: str,
        account_id: str,
        users: dict[str, dict[str, Any]],
        edge_type: str,
    ) -> None:
        if not account_id:
            return
        user = users.get(account_id) or {}
        email = str(user.get("email") or "").strip().lower()
        vertex_id = person_vertex_id(self.site, email or None, account_id)
        if vertex_id is None:
            return
        attributes: dict[str, str] = {
            "display_name": str(user.get("displayName") or "")
        }
        if email:
            attributes["email"] = email
        vertices.append(VertexRecord("Person", vertex_id, attributes))
        edges.append(EdgeRecord(source_type, source_id, edge_type, "Person", vertex_id))

    def _url(self, space_key: str, content_type: str, page_id: str) -> str:
        kind = "blog" if content_type == "blogpost" else "pages"
        return f"https://{self.site}/wiki/spaces/{space_key}/{kind}/{page_id}"

    def _attachment_url(self, attachment: dict[str, Any]) -> str:
        link = str(attachment.get("downloadLink") or "")
        links = attachment.get("_links") or {}
        if not link and isinstance(links, dict):
            link = str(links.get("download") or "")
        if link.startswith("/"):
            return f"https://{self.site}/wiki{link}"
        return link
