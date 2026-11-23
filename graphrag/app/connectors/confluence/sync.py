"""Confluence synchronization into graph vertices, chunks, and embeddings."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from common.config import get_embedding_store
from connectors.records import EdgeRecord, VertexRecord
from connectors.upsert import upsert_payload

from .client import (
    ConfluenceAPIError,
    ConfluenceClient,
    content_cql,
    search_since,
)
from .config import ConfluenceDataSource
from .mapper import (
    MappedChunk,
    MappedPage,
    ConfluenceMapper,
    jira_issue_vertex_id,
    person_vertex_id,
    zendesk_ticket_vertex_id,
)
from .schema import (
    AUTHORED,
    EDITED,
    LINKS_ISSUE,
    LINKS_TICKET,
    MEMBER,
    OWNED,
    confluence_schema_status,
)
from .state import ConfluenceSourceStore

logger = logging.getLogger(__name__)

UPSERT_BATCH_SIZE = 500
EMBEDDING_BATCH_SIZE = 32


class ConfluenceSyncService:
    def __init__(
        self,
        graphname: str,
        source: ConfluenceDataSource,
        conn,
        *,
        store: ConfluenceSourceStore | None = None,
        client: ConfluenceClient | None = None,
    ):
        self.graphname = graphname
        self.source = source
        self.conn = conn
        self.store = store or ConfluenceSourceStore()
        self.client = client
        self.mapper = ConfluenceMapper(source)
        self._spaces: dict[str, tuple[str, str]] = {}
        self._owns_client = client is None

    def run(self) -> dict[str, Any]:
        if not self.source.enabled:
            raise RuntimeError("Confluence data source is disabled.")
        status = confluence_schema_status(self.conn)
        if status["status"] != "installed":
            raise RuntimeError(
                "Confluence schema is not installed for this graph."
            )
        self.source.sync.last_started_at = datetime.now(timezone.utc)
        self.source.sync.last_error = None
        self.source.sync.progress_phase = "pages"
        written = 0
        client = self.client or ConfluenceClient(self.source)
        try:
            self.client = client
            for content_id in list(self.source.sync.failed_content_ids):
                if self._ingest_id(content_id, move_checkpoint=False):
                    self._drop(self.source.sync.failed_content_ids, content_id)
                    written += 1
            for content_id in list(self.source.sync.pending_comment_content_ids):
                if self._ingest_id(content_id, move_checkpoint=False):
                    self._drop(self.source.sync.pending_comment_content_ids, content_id)
            since = search_since(self.source)
            for item in client.search(content_cql(self.source, since)):
                content_id = str(item.get("id") or "")
                content_type = str(item.get("type") or "page")
                if content_type not in self.source.scope.content_types:
                    continue
                if not self._ingest_item(item, move_checkpoint=True):
                    continue
                written += 1
            self.source.sync.last_completed_at = datetime.now(timezone.utc)
            self.source.sync.last_page_count = written
            self.source.sync.progress_phase = None
        except Exception as exc:
            self.source.sync.last_error = str(exc)[:1000]
            raise
        finally:
            if self._owns_client and self.client is not None:
                self.client.close()
            self.store.update_runtime_state(self.graphname, self.source)
        return {
            "pages_written": written,
            "checkpoint": (
                self.source.sync.checkpoint.isoformat()
                if self.source.sync.checkpoint
                else None
            ),
            "last_content_id": self.source.sync.last_content_id,
            "last_link_error": self.source.sync.last_link_error,
        }

    def _ingest_id(self, content_id: str, *, move_checkpoint: bool) -> bool:
        content_type = "page"
        try:
            item = self.client.content(content_id, content_type)
        except ConfluenceAPIError:
            item = self.client.content(content_id, "blogpost")
            content_type = "blogpost"
        item["type"] = content_type
        return self._ingest_item(item, move_checkpoint=move_checkpoint)

    def _ingest_item(self, item: dict[str, Any], *, move_checkpoint: bool) -> bool:
        content_id = str(item.get("id") or "")
        content_type = str(item.get("type") or "page")
        if content_type not in ("page", "blogpost"):
            content_type = "page"
        try:
            content = item if item.get("body") else self.client.content(content_id, content_type)
            comments, comments_failed = self._comments(content_id, content_type)
            labels = [
                str(row.get("name") or "")
                for row in self.client.labels(content_id, content_type)
            ]
            attachments = self.client.attachments(content_id, content_type)
            space_key, space_name = self._space(str(content.get("spaceId") or ""))
            users = self._users(content, comments)
            mapped = self.mapper.map_page(
                content,
                content_type=content_type,
                comments=comments,
                labels=labels,
                attachments=attachments,
                users=users,
                space_key=space_key,
                space_name=space_name,
            )
        except ConfluenceAPIError as exc:
            if exc.status_code in (401, 403):
                raise RuntimeError("Confluence rejected the API token.") from exc
            self._remember(self.source.sync.failed_content_ids, content_id)
            self.store.update_runtime_state(self.graphname, self.source)
            return False
        except Exception:
            self._remember(self.source.sync.failed_content_ids, content_id)
            self.store.update_runtime_state(self.graphname, self.source)
            raise
        self._use_existing_jira_person(mapped)
        stored = self._stored_hash(mapped.vertex_id)
        if stored != mapped.content_hash:
            self._write_page(mapped)
        self._write_links(mapped)
        self._write_memberships(mapped)
        if comments_failed:
            self._remember(self.source.sync.pending_comment_content_ids, content_id)
        else:
            self._drop(self.source.sync.pending_comment_content_ids, content_id)
        if move_checkpoint and mapped.updated:
            self.source.sync.checkpoint = _parse_time(mapped.updated)
            self.source.sync.last_content_id = content_id
            self._drop(self.source.sync.failed_content_ids, content_id)
        self.source.sync.progress_written += 1
        self.store.update_runtime_state(self.graphname, self.source)
        return True

    def _comments(
        self, content_id: str, content_type: str
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            footer = self.client.footer_comments(content_id, content_type)
            inline = self.client.inline_comments(content_id, content_type)
        except ConfluenceAPIError as exc:
            if exc.status_code in (401, 403):
                raise
            return [], True
        for row in footer:
            row["comment_type"] = "footer"
        for row in inline:
            row["comment_type"] = "inline"
        return footer + inline, False

    def _space(self, space_id: str) -> tuple[str, str]:
        if not space_id:
            return "", ""
        if space_id in self._spaces:
            return self._spaces[space_id]
        payload = self.client.space(space_id)
        pair = (str(payload.get("key") or ""), str(payload.get("name") or ""))
        self._spaces[space_id] = pair
        return pair

    def _users(self, content: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        account_ids = [
            str(content.get("authorId") or ""),
            str(content.get("ownerId") or ""),
            str((content.get("version") or {}).get("authorId") or ""),
        ]
        for comment in comments:
            account_ids.append(str((comment.get("version") or {}).get("authorId") or ""))
        found = {}
        for user in self.client.users([item for item in account_ids if item]):
            account_id = str(user.get("accountId") or "")
            if account_id:
                found[account_id] = user
        return found

    def _write_page(self, mapped: MappedPage) -> None:
        self._replace_edges(mapped.vertex_id, "ConfluencePage", [AUTHORED, EDITED, OWNED])
        self._upsert(mapped.vertices, mapped.edges)
        self._upsert_chunks(mapped)
        chunks = list(mapped.page_chunks)
        for comment in mapped.comments:
            chunks.extend(comment.chunks)
        self._embed(chunks)

    def _write_links(self, mapped: MappedPage) -> None:
        try:
            self._replace_link(
                mapped,
                LINKS_ISSUE,
                "JiraIssue",
                [
                    (jira_issue_vertex_id(key), {"issue_key": key})
                    for key in mapped.issue_keys
                ],
            )
            self._replace_link(
                mapped,
                LINKS_TICKET,
                "ZendeskTicket",
                [
                    (
                        zendesk_ticket_vertex_id(subdomain, ticket_id),
                        {"ticket_id": ticket_id},
                    )
                    for subdomain, ticket_id in mapped.zendesk_tickets
                ],
            )
            self.source.sync.last_link_error = None
        except Exception as exc:
            self.source.sync.last_link_error = str(exc)[:1000]

    def _replace_link(
        self,
        mapped: MappedPage,
        edge_type: str,
        target_type: str,
        targets: list[tuple[str, dict[str, str]]],
    ) -> None:
        if not _has_type(self.conn, target_type):
            return
        self._replace_edges(mapped.vertex_id, "ConfluencePage", [edge_type])
        vertices = []
        edges = []
        for vertex_id, attributes in targets:
            if not _vertex_exists(self.conn, target_type, vertex_id):
                vertices.append(VertexRecord(target_type, vertex_id, attributes))
            edges.append(
                EdgeRecord(
                    "ConfluencePage",
                    mapped.vertex_id,
                    edge_type,
                    target_type,
                    vertex_id,
                )
            )
        self._upsert(vertices, edges)

    def _write_memberships(self, mapped: MappedPage) -> None:
        if not _has_type(self.conn, "Organization"):
            return
        for vertex in mapped.vertices:
            if vertex.vertex_type != "Person":
                continue
            email = str(vertex.attributes.get("email") or "")
            domain = email.split("@", 1)[1].lower() if "@" in email else ""
            if not domain:
                continue
            organization_id = _organization_id(self.conn, domain)
            if not organization_id:
                continue
            self._upsert(
                [],
                [
                    EdgeRecord(
                        "Organization",
                        organization_id,
                        MEMBER,
                        "Person",
                        vertex.vertex_id,
                    )
                ],
            )

    def _use_existing_jira_person(self, mapped: MappedPage) -> None:
        prefix = f"confluence:{self.source.connection.site}:user:"
        for vertex in list(mapped.vertices):
            if vertex.vertex_type != "Person" or vertex.attributes.get("email"):
                continue
            if not vertex.vertex_id.startswith(prefix):
                continue
            account_id = vertex.vertex_id[len(prefix):]
            jira_id = f"jira:user:{account_id}"
            if not _vertex_exists(self.conn, "Person", jira_id):
                continue
            mapped.vertices = [
                item for item in mapped.vertices if item.vertex_id != vertex.vertex_id
            ]
            mapped.edges = [
                EdgeRecord(
                    edge.source_type,
                    edge.source_id,
                    edge.edge_type,
                    edge.target_type,
                    jira_id if edge.target_id == vertex.vertex_id else edge.target_id,
                    edge.attributes,
                )
                if edge.target_id == vertex.vertex_id
                else edge
                for edge in mapped.edges
            ]

    def _stored_hash(self, vertex_id: str) -> str:
        try:
            rows = self.conn.getVerticesById("ConfluencePage", [vertex_id]) or []
        except Exception:
            return ""
        for row in rows:
            if isinstance(row, dict):
                value = (row.get("attributes") or {}).get("content_hash")
                if isinstance(value, dict):
                    value = value.get("value")
                return str(value or "")
        return ""

    def _replace_edges(self, vertex_id: str, vertex_type: str, edge_types: list[str]) -> None:
        for edge_type in edge_types:
            try:
                self.conn.delEdges(vertex_type, vertex_id, edge_type)
            except Exception as exc:
                if "is not a valid vertex id" not in str(exc):
                    raise

    def _upsert(self, vertices: list[VertexRecord], edges: list[EdgeRecord]) -> None:
        for records, are_edges in ((vertices, False), (edges, True)):
            for start in range(0, len(records), UPSERT_BATCH_SIZE):
                batch = records[start : start + UPSERT_BATCH_SIZE]
                if not batch:
                    continue
                payload = upsert_payload(
                    [] if are_edges else batch,
                    batch if are_edges else [],
                )
                result = self.conn.upsertData(json.dumps(payload))
                if isinstance(result, dict) and (
                    result.get("skipped_vertices") or result.get("skipped_edges")
                ):
                    raise RuntimeError(
                        "TigerGraph rejected part of the Confluence graph upsert"
                    )

    def _upsert_chunks(self, mapped: MappedPage) -> None:
        epoch_added = int(time.time())
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        previous = None
        for chunk in mapped.page_chunks:
            vertices.extend(_chunk_vertices(chunk, epoch_added, "confluence_page"))
            edges.extend(
                _chunk_edges(chunk.chunk_id, mapped.vertex_id, (), previous)
            )
            previous = chunk.chunk_id
        for comment in mapped.comments:
            previous = None
            for chunk in comment.chunks:
                vertices.extend(_chunk_vertices(chunk, epoch_added, "confluence_comment"))
                edges.extend(
                    _chunk_edges(
                        chunk.chunk_id,
                        mapped.vertex_id,
                        (("ConfluenceComment", comment.vertex_id),),
                        previous,
                    )
                )
                previous = chunk.chunk_id
        self._upsert(vertices, edges)

    def _embed(self, chunks: list[MappedChunk]) -> None:
        if not chunks:
            return
        store = get_embedding_store(graphname=self.graphname)

        async def embed_batches() -> None:
            await store.aadd_embeddings(
                [(chunk.text, []) for chunk in chunks],
                [{"vertex_id": (chunk.chunk_id, "DocumentChunk")} for chunk in chunks],
            )

        asyncio.run(embed_batches())

    @staticmethod
    def _remember(values: list[str], content_id: str) -> None:
        if content_id and content_id not in values:
            values.append(content_id)

    @staticmethod
    def _drop(values: list[str], content_id: str) -> None:
        if content_id in values:
            values.remove(content_id)


def _chunk_vertices(chunk: MappedChunk, epoch_added: int, ctype: str) -> list[VertexRecord]:
    return [
        VertexRecord(
            "DocumentChunk",
            chunk.chunk_id,
            {
                "idx": chunk.index,
                "epoch_added": epoch_added,
                "epoch_processing": 0,
                "epoch_processed": 0,
            },
        ),
        VertexRecord(
            "Content",
            chunk.chunk_id,
            {"ctype": ctype, "text": chunk.text, "epoch_added": epoch_added},
        ),
    ]


def _chunk_edges(
    chunk_id: str,
    page_vertex_id: str,
    entities: tuple[tuple[str, str], ...],
    previous_chunk_id: str | None,
) -> list[EdgeRecord]:
    edges = [
        EdgeRecord("DocumentChunk", chunk_id, "HAS_CONTENT", "Content", chunk_id),
        EdgeRecord(
            "DocumentChunk",
            chunk_id,
            "CONTAINS_ENTITY",
            "ConfluencePage",
            page_vertex_id,
        ),
    ]
    for vertex_type, vertex_id in entities:
        edges.append(
            EdgeRecord(
                "DocumentChunk",
                chunk_id,
                "CONTAINS_ENTITY",
                vertex_type,
                vertex_id,
            )
        )
    if previous_chunk_id:
        edges.append(
            EdgeRecord(
                "DocumentChunk",
                chunk_id,
                "IS_AFTER",
                "DocumentChunk",
                previous_chunk_id,
            )
        )
    return edges


def _has_type(conn, name: str) -> bool:
    try:
        names = conn.getVertexTypes() or []
    except Exception:
        return False
    folded = name.casefold()
    return any(str(item).casefold() == folded for item in names)


def _vertex_exists(conn, vertex_type: str, vertex_id: str) -> bool:
    try:
        found = conn.getVerticesById(vertex_type, [vertex_id]) or []
    except Exception as exc:
        if "not a valid vertex" in str(exc).lower():
            return False
        raise
    return bool(found)


def _organization_id(conn, domain: str) -> str | None:
    vertex_id = f"organization:{domain}"
    if _vertex_exists(conn, "Organization", vertex_id):
        return vertex_id
    return None


def _parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
