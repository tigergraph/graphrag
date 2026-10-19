"""Ingest Zendesk tickets into the graph and resolve cross-source links."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from common.chunkers.structured import StructuredChunker
from common.config import get_embedding_store, get_graphrag_config

from connectors.cross_source import TigerGraphLinkGraph, link_text, resolve_target
from connectors.jira.mapper import EdgeRecord, VertexRecord
from connectors.jira.sync import _upsert_payload

from .client import ZendeskClient
from .config import ZendeskDataSource
from .schema import zendesk_schema_status
from .state import ZendeskSourceStore

logger = logging.getLogger(__name__)


class ZendeskSyncService:
    def __init__(
        self,
        graphname: str,
        source: ZendeskDataSource,
        conn,
        *,
        store: ZendeskSourceStore | None = None,
        client: ZendeskClient | None = None,
    ):
        self.graphname = graphname
        self.source = source
        self.conn = conn
        self.store = store or ZendeskSourceStore()
        self.client = client or ZendeskClient(source)
        self._owns_client = client is None
        chunker_config = get_graphrag_config(graphname).get("chunker_config", {})
        self.chunker = StructuredChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )

    def run(self) -> dict[str, Any]:
        if not self.source.enabled:
            raise ValueError("Cannot sync a disabled data source")
        started = datetime.now(timezone.utc)
        self.source.sync.last_started_at = started
        self.source.sync.last_error = None
        self.store.update_runtime_state(self.graphname, self.source)
        try:
            schema_result = zendesk_schema_status(self.conn)
            if schema_result["status"] != "installed":
                raise RuntimeError(
                    "Zendesk schema is not installed for this graph. "
                    "Install it from Data Sources before synchronization."
                )
            written = 0
            for tickets, end_time, _done in self.client.iter_ticket_pages(
                self._start_time()
            ):
                selected = [ticket for ticket in tickets if self._in_scope(ticket)]
                if selected:
                    self._write_tickets(selected)
                    written += len(selected)
                if end_time:
                    self.source.sync.checkpoint = datetime.fromtimestamp(
                        int(end_time),
                        timezone.utc,
                    )
                    self.store.update_runtime_state(self.graphname, self.source)
            self.source.sync.last_ticket_count = written
            self.source.sync.last_completed_at = datetime.now(timezone.utc)
            self.source.sync.last_error = None
            self.store.update_runtime_state(self.graphname, self.source)
            return {"tickets": written}
        except Exception as exc:
            self.source.sync.last_error = str(exc)[:1000]
            self.store.update_runtime_state(self.graphname, self.source)
            raise
        finally:
            if self._owns_client:
                self.client.close()

    def _start_time(self) -> int:
        checkpoint = self.source.sync.checkpoint
        if checkpoint is not None:
            if checkpoint.tzinfo is None:
                checkpoint = checkpoint.replace(tzinfo=timezone.utc)
            start = checkpoint - timedelta(seconds=self.source.sync.overlap_seconds)
            return max(int(start.timestamp()), 1)
        updated_after = self.source.scope.updated_after
        if updated_after is not None:
            start = datetime(
                updated_after.year,
                updated_after.month,
                updated_after.day,
                tzinfo=timezone.utc,
            )
            return max(int(start.timestamp()), 1)
        return 1

    def _in_scope(self, ticket: dict[str, Any]) -> bool:
        status = str(ticket.get("status") or "").lower()
        if self.source.scope.statuses and status not in self.source.scope.statuses:
            return False
        updated_after = self.source.scope.updated_after
        if updated_after is None:
            return True
        updated_at = str(ticket.get("updated_at") or "")
        if not updated_at:
            return True
        return updated_at[:10] >= updated_after.isoformat()

    def _write_tickets(self, tickets: list[dict[str, Any]]) -> None:
        user_ids = [
            str(ticket.get("requester_id"))
            for ticket in tickets
            if ticket.get("requester_id") is not None
        ]
        comments_by_ticket: dict[str, list[dict[str, Any]]] = {}
        if self.source.scope.include_comments:
            for ticket in tickets:
                ticket_id = str(ticket["id"])
                comments = self.client.comments(ticket_id)
                comments_by_ticket[ticket_id] = comments
                user_ids.extend(
                    str(comment.get("author_id"))
                    for comment in comments
                    if comment.get("author_id") is not None
                )
        users = self.client.users(user_ids)
        subdomain = self.source.connection.subdomain
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        chunks: list[tuple[str, int, str, str, str]] = []
        for ticket in tickets:
            ticket_id = str(ticket["id"])
            vertex_id = f"zendesk:{subdomain}:ticket:{ticket_id}"
            url = str(
                ticket.get("url")
                or f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"
            )
            vertices.append(
                VertexRecord(
                    "ZendeskTicket",
                    vertex_id,
                    {
                        "ticket_id": ticket_id,
                        "subject": str(ticket.get("subject") or ""),
                        "status": str(ticket.get("status") or ""),
                        "url": url,
                    },
                )
            )
            requester_id = ticket.get("requester_id")
            if requester_id is not None:
                user_vertex_id = f"zendesk:{subdomain}:user:{requester_id}"
                user = users.get(str(requester_id)) or {}
                vertices.append(
                    VertexRecord(
                        "ZendeskUser",
                        user_vertex_id,
                        {
                            "user_id": str(requester_id),
                            "display_name": str(user.get("name") or ""),
                        },
                    )
                )
                edges.append(
                    EdgeRecord(
                        "ZendeskTicket",
                        vertex_id,
                        "ZENDESK_REQUESTED_BY",
                        "ZendeskUser",
                        user_vertex_id,
                    )
                )
            description = "\n".join(
                part
                for part in (
                    f"Ticket {ticket_id}",
                    str(ticket.get("subject") or ""),
                    str(ticket.get("description") or ""),
                    url,
                )
                if part
            )
            chunks.extend(
                self._chunk_rows(vertex_id, "zendesk_ticket", description, vertex_id)
            )
            for comment in comments_by_ticket.get(ticket_id, []):
                body = str(comment.get("body") or "").strip()
                if not body:
                    continue
                comment_id = str(comment["id"])
                comment_vertex_id = f"zendesk:{subdomain}:comment:{comment_id}"
                author_id = comment.get("author_id")
                author = users.get(str(author_id)) if author_id is not None else None
                author_name = str((author or {}).get("name") or "")
                vertices.append(
                    VertexRecord(
                        "ZendeskComment",
                        comment_vertex_id,
                        {"comment_id": comment_id, "body": body},
                    )
                )
                edges.append(
                    EdgeRecord(
                        "ZendeskComment",
                        comment_vertex_id,
                        "ZENDESK_COMMENT_ON",
                        "ZendeskTicket",
                        vertex_id,
                    )
                )
                comment_text = "\n".join(
                    part
                    for part in (
                        f"Ticket {ticket_id}",
                        f"Comment by {author_name}" if author_name else "",
                        body,
                    )
                    if part
                )
                chunks.extend(
                    self._chunk_rows(
                        comment_vertex_id,
                        "zendesk_comment",
                        comment_text,
                        vertex_id,
                        ("ZendeskComment", comment_vertex_id),
                    )
                )
        self.conn.upsertData(json.dumps(_upsert_payload(vertices, edges)))
        self._write_chunks(chunks)
        graph = TigerGraphLinkGraph(self.conn)
        for ticket in tickets:
            ticket_id = str(ticket["id"])
            vertex_id = f"zendesk:{subdomain}:ticket:{ticket_id}"
            resolve_target(graph, "ZendeskTicket", vertex_id)
            texts = [
                str(ticket.get("subject") or ""),
                str(ticket.get("description") or ""),
                str(ticket.get("url") or ""),
            ]
            texts.extend(
                str(comment.get("body") or "")
                for comment in comments_by_ticket.get(ticket_id, [])
            )
            link_text(graph, "ZendeskTicket", vertex_id, "\n".join(texts))

    def _chunk_rows(
        self,
        owner_id: str,
        ctype: str,
        text: str,
        ticket_vertex_id: str,
        extra_entity: tuple[str, str] | None = None,
    ) -> list[tuple[str, int, str, str, str]]:
        pieces = [str(piece) for piece in self.chunker.chunk(text) if str(piece).strip()]
        if not pieces and text.strip():
            pieces = [text.strip()]
        rows = []
        for index, piece in enumerate(pieces):
            chunk_id = f"{owner_id}:chunk:{index}"
            rows.append((chunk_id, index, piece, ctype, ticket_vertex_id))
            if extra_entity is not None:
                rows.append(
                    (
                        chunk_id,
                        index,
                        piece,
                        ctype,
                        f"{extra_entity[0]}|{extra_entity[1]}",
                    )
                )
        return rows

    def _write_chunks(self, rows: list[tuple[str, int, str, str, str]]) -> None:
        if not rows:
            return
        epoch_added = int(time.time())
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        seen_chunks: set[str] = set()
        ordered_chunks: list[tuple[str, str]] = []
        previous_by_owner: dict[str, str] = {}
        for chunk_id, index, text, ctype, target in rows:
            if chunk_id not in seen_chunks:
                seen_chunks.add(chunk_id)
                ordered_chunks.append((chunk_id, text))
                vertices.extend(
                    [
                        VertexRecord(
                            "DocumentChunk",
                            chunk_id,
                            {
                                "idx": index,
                                "epoch_added": epoch_added,
                                "epoch_processing": 0,
                                "epoch_processed": 0,
                            },
                        ),
                        VertexRecord(
                            "Content",
                            chunk_id,
                            {
                                "ctype": ctype,
                                "text": text,
                                "epoch_added": epoch_added,
                            },
                        ),
                    ]
                )
                edges.append(
                    EdgeRecord(
                        "DocumentChunk",
                        chunk_id,
                        "HAS_CONTENT",
                        "Content",
                        chunk_id,
                    )
                )
            if "|" in target and not target.startswith("zendesk:"):
                entity_type, entity_id = target.split("|", 1)
            else:
                entity_type, entity_id = "ZendeskTicket", target
            edges.append(
                EdgeRecord(
                    "DocumentChunk",
                    chunk_id,
                    "CONTAINS_ENTITY",
                    entity_type,
                    entity_id,
                )
            )
            owner = chunk_id.rsplit(":chunk:", 1)[0]
            previous = previous_by_owner.get(owner)
            if previous and previous != chunk_id:
                edges.append(
                    EdgeRecord(
                        "DocumentChunk",
                        chunk_id,
                        "IS_AFTER",
                        "DocumentChunk",
                        previous,
                    )
                )
            previous_by_owner[owner] = chunk_id
        self.conn.upsertData(json.dumps(_upsert_payload(vertices, edges)))
        self._embed_chunks(ordered_chunks)

    def _embed_chunks(self, chunks: list[tuple[str, str]]) -> None:
        if not chunks:
            return
        store = get_embedding_store(graphname=self.graphname)

        async def embed_batches() -> None:
            for start in range(0, len(chunks), 32):
                batch = chunks[start : start + 32]
                await store.aadd_embeddings(
                    [(text, []) for _chunk_id, text in batch],
                    [
                        {"vertex_id": (chunk_id, "DocumentChunk")}
                        for chunk_id, _text in batch
                    ],
                )

        asyncio.run(embed_batches())
        processed_at = int(time.time())
        for chunk_id, _text in chunks:
            self.conn.upsertVertex(
                "DocumentChunk",
                chunk_id,
                attributes={"epoch_processed": processed_at},
            )
