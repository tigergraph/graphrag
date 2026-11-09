"""Zendesk synchronization into graph vertices, chunks, and embeddings."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from common.config import get_embedding_store
from connectors.records import EdgeRecord, VertexRecord
from connectors.upsert import upsert_payload

from .client import COMMENT_FETCH_WORKERS, ZendeskAPIError, ZendeskClient
from .config import ZendeskDataSource
from .linking import LinkReconciliationResult, reconcile_links
from .mapper import MappedChunk, MappedTicket, ZendeskMapper
from .schema import zendesk_schema_status
from .state import ZendeskSourceStore

logger = logging.getLogger(__name__)

UPSERT_BATCH_SIZE = 500
EMBEDDING_BATCH_SIZE = 32


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

    def run(self) -> dict[str, Any]:
        try:
            if not self.source.enabled:
                raise ValueError("Cannot sync a disabled data source")
            schema_result = zendesk_schema_status(self.conn)
            if schema_result["status"] != "installed":
                raise RuntimeError(
                    "Zendesk schema is not installed for this graph. "
                    "Install it from Data Sources before synchronization."
                )

            started = datetime.now(timezone.utc)
            self.source.sync.last_started_at = started
            self.source.sync.last_error = None
            self.store.update_runtime_state(self.graphname, self.source)
            mapper = ZendeskMapper(self.source, graphname=self.graphname)
            try:
                tickets_upserted, comments_upserted = self._retry_failed_tickets(
                    mapper
                )
                retried_tickets, retried_comments = self._retry_pending_comments(
                    mapper
                )
                tickets_upserted += retried_tickets
                comments_upserted += retried_comments
                page_tickets, page_comments = self._sync_pages(mapper)
                tickets_upserted += page_tickets
                comments_upserted += page_comments
                link_result = self._reconcile_links()
                self.source.sync.last_ticket_count = tickets_upserted
                self.source.sync.last_completed_at = datetime.now(timezone.utc)
                if self.source.sync.failed_ticket_ids:
                    self.source.sync.last_error = (
                        self.source.sync.last_error or "some tickets failed"
                    )
                else:
                    self.source.sync.last_error = None
                self.store.update_runtime_state(self.graphname, self.source)
                return {
                    "tickets_upserted": tickets_upserted,
                    "comments_upserted": comments_upserted,
                    "links_written": link_result.links_written,
                    "links_skipped": link_result.links_skipped,
                    "pending_comment_ticket_ids": list(
                        self.source.sync.pending_comment_ticket_ids
                    ),
                    "last_link_error": self.source.sync.last_link_error,
                }
            except Exception as exc:
                self.source.sync.last_error = str(exc)[:1000]
                self.store.update_runtime_state(self.graphname, self.source)
                raise
        finally:
            if self._owns_client:
                self.client.close()

    def _retry_failed_tickets(self, mapper: ZendeskMapper) -> tuple[int, int]:
        pending = list(self.source.sync.failed_ticket_ids)
        still_failed: list[str] = []
        tickets_upserted = 0
        comments_upserted = 0
        for ticket_id in pending:
            try:
                ticket = self.client.ticket(ticket_id)
                comments, failed = self._comments_for([ticket])
                if failed:
                    still_failed.append(str(ticket_id))
                    continue
                mapped = self._map_tickets(mapper, [ticket], comments)
                comments_upserted += self._write_mapped(mapped)
                tickets_upserted += len(mapped)
            except Exception:
                logger.warning(
                    "Zendesk retry failed for ticket %s", ticket_id, exc_info=True
                )
                still_failed.append(str(ticket_id))
        self.source.sync.failed_ticket_ids = still_failed
        self.store.update_runtime_state(self.graphname, self.source)
        return tickets_upserted, comments_upserted

    def _retry_pending_comments(self, mapper: ZendeskMapper) -> tuple[int, int]:
        pending = list(self.source.sync.pending_comment_ticket_ids)
        still_pending: list[str] = []
        tickets_upserted = 0
        comments_upserted = 0
        for ticket_id in pending:
            try:
                ticket = self.client.ticket(ticket_id)
            except ZendeskAPIError as exc:
                if exc.status_code == 404:
                    continue
                still_pending.append(str(ticket_id))
                continue
            if not self._keep(ticket):
                continue
            comments, failed = self._comments_for([ticket])
            if failed:
                still_pending.append(str(ticket_id))
            mapped = self._map_tickets(mapper, [ticket], comments)
            written_comments = self._write_mapped(mapped)
            tickets_upserted += len(mapped)
            comments_upserted += written_comments
        self.source.sync.pending_comment_ticket_ids = still_pending
        self.store.update_runtime_state(self.graphname, self.source)
        return tickets_upserted, comments_upserted

    def _sync_pages(self, mapper: ZendeskMapper) -> tuple[int, int]:
        tickets_upserted = 0
        comments_upserted = 0
        for tickets, _end_time, _end_of_stream, _after_cursor in self.client.iter_ticket_pages():
            kept = [ticket for ticket in tickets if self._keep(ticket)]
            kept.sort(key=lambda ticket: (_ticket_sort_key(ticket), int(ticket.get("id") or 0)))
            comments, failed = self._comments_for(kept)
            pending = list(self.source.sync.pending_comment_ticket_ids)
            for ticket_id in failed:
                if ticket_id not in pending:
                    pending.append(ticket_id)
            self.source.sync.pending_comment_ticket_ids = pending
            mapped = {
                item.ticket_id: item
                for item in self._map_tickets(mapper, kept, comments)
            }
            failed_ids = list(self.source.sync.failed_ticket_ids)
            for ticket in kept:
                ticket_id = str(ticket.get("id"))
                item = mapped.get(ticket_id)
                if item is None:
                    continue
                try:
                    comments_upserted += self._write_mapped([item])
                except Exception:
                    logger.warning(
                        "Zendesk write failed for ticket %s", ticket_id, exc_info=True
                    )
                    if ticket_id not in failed_ids:
                        failed_ids.append(ticket_id)
                    self.source.sync.failed_ticket_ids = failed_ids
                    self.source.sync.last_error = f"ticket {ticket_id} failed"
                    self.store.update_runtime_state(self.graphname, self.source)
                    continue
                tickets_upserted += 1
                updated_at = ticket.get("updated_at")
                if updated_at:
                    self.source.sync.checkpoint = _parse_time(str(updated_at))
                if str(ticket_id).isdigit():
                    self.source.sync.last_ticket_id = int(ticket_id)
                failed_ids = [
                    failed_id for failed_id in failed_ids if failed_id != ticket_id
                ]
                self.source.sync.failed_ticket_ids = failed_ids
                self.store.update_runtime_state(self.graphname, self.source)
        return tickets_upserted, comments_upserted

    def _reconcile_links(self):
        try:
            result = reconcile_links(
                self.conn,
                self.source.connection.subdomain,
                self.client.iter_jira_link_pages(),
                list(self.source.sync.linked_ticket_ids),
            )
        except Exception as exc:
            logger.warning(
                "Zendesk link inventory failed for graph=%s source=%s: %s",
                self.graphname,
                self.source.id,
                exc,
            )
            self.source.sync.last_link_error = str(exc)[:1000]
            self.store.update_runtime_state(self.graphname, self.source)
            return LinkReconciliationResult()
        self.source.sync.last_link_error = None
        self.source.sync.linked_ticket_ids = list(result.linked_ticket_ids)
        self.store.update_runtime_state(self.graphname, self.source)
        return result

    def _keep(self, ticket: dict[str, Any]) -> bool:
        statuses = {status.casefold() for status in self.source.scope.statuses}
        if statuses and str(ticket.get("status") or "").casefold() not in statuses:
            return False
        updated_after = self.source.scope.updated_after
        if updated_after is None:
            return True
        raw = ticket.get("updated_at")
        if not raw:
            return False
        updated = _parse_time(str(raw))
        cutoff = updated_after
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        return updated >= cutoff

    def _comments_for(
        self,
        tickets: list[dict[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        comments: dict[str, list[dict[str, Any]]] = {}
        failed: list[str] = []
        if not tickets:
            return comments, failed
        workers = min(len(tickets), COMMENT_FETCH_WORKERS)

        def load(ticket: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
            ticket_id = str(ticket.get("id"))
            return ticket_id, self.client.comments(ticket_id)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(load, ticket): ticket for ticket in tickets}
            for future in as_completed(futures):
                ticket = futures[future]
                ticket_id = str(ticket.get("id"))
                try:
                    loaded_id, loaded_comments = future.result()
                    comments[loaded_id] = loaded_comments
                except Exception:
                    logger.warning(
                        "Zendesk comments failed for ticket %s",
                        ticket_id,
                        exc_info=True,
                    )
                    comments[ticket_id] = []
                    failed.append(ticket_id)
        return comments, failed

    def _map_tickets(
        self,
        mapper: ZendeskMapper,
        tickets: list[dict[str, Any]],
        comments: dict[str, list[dict[str, Any]]],
    ) -> list[MappedTicket]:
        audits = self._audits_for(tickets)
        user_ids: list[str] = []
        for ticket in tickets:
            requester = ticket.get("requester_id")
            if requester is not None:
                user_ids.append(str(requester))
            for comment in comments.get(str(ticket.get("id")), []):
                author = comment.get("author_id")
                if author is not None:
                    user_ids.append(str(author))
            for audit in audits.get(str(ticket.get("id")), []):
                author = audit.get("author_id")
                if author is not None:
                    user_ids.append(str(author))
        users_by_id = {
            str(user.get("id")): {
                "name": str(user.get("name") or ""),
                "email": str(user.get("email") or ""),
                "organization_id": str(user.get("organization_id") or ""),
            }
            for user in self.client.users(user_ids)
            if user.get("id") is not None
        }
        organization_ids = [
            str(ticket.get("organization_id"))
            for ticket in tickets
            if ticket.get("organization_id") is not None
        ]
        group_ids = [
            str(ticket.get("group_id"))
            for ticket in tickets
            if ticket.get("group_id") is not None
        ]
        organizations_by_id = {}
        for row in self.client.organizations(organization_ids):
            if row.get("id") is None:
                continue
            domains = row.get("domain_names") or []
            organizations_by_id[str(row.get("id"))] = {
                "name": str(row.get("name") or ""),
                "domain": str(domains[0] or "") if domains else "",
            }
        groups_by_id = {
            str(row.get("id")): str(row.get("name") or "")
            for row in self.client.groups(group_ids)
            if row.get("id") is not None
        }
        return [
            mapper.map_ticket(
                ticket,
                comments.get(str(ticket.get("id")), []),
                users_by_id,
                organizations_by_id,
                groups_by_id,
                audits.get(str(ticket.get("id")), []),
            )
            for ticket in tickets
        ]

    def _audits_for(self, tickets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        found: dict[str, list[dict[str, Any]]] = {}
        missing: list[str] = []
        for ticket in tickets:
            ticket_id = str(ticket.get("id"))
            try:
                found[ticket_id] = self.client.audits(ticket_id)
            except ZendeskAPIError as exc:
                if exc.status_code == 404:
                    found[ticket_id] = []
                    missing.append(ticket_id)
                    continue
                if exc.status_code in (401, 403):
                    raise ZendeskAPIError(
                        exc.status_code,
                        "Zendesk rejected the API token while reading ticket history.",
                    ) from exc
                raise
        if missing:
            logger.info(
                "Zendesk has no audit history for %s tickets. "
                "Those tickets are stored without change rows.",
                len(missing),
            )
        return found

    def _write_mapped(self, mapped: list[MappedTicket]) -> int:
        ticket_hashes = self._stored_hashes(
            "ZendeskTicket",
            [item.vertex_id for item in mapped],
        )
        comment_hashes = self._stored_hashes(
            "ZendeskComment",
            [comment.vertex_id for item in mapped for comment in item.comments],
        )
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        changed_tickets: list[MappedTicket] = []
        changed_comments: list[tuple[MappedTicket, Any]] = []
        for item in mapped:
            edges.extend(item.edges)
            ticket_changed = ticket_hashes.get(item.vertex_id, "") != item.content_hash
            if ticket_changed and ticket_hashes.get(item.vertex_id, ""):
                self._delete_chunks("ZendeskTicket", item.vertex_id)
            if ticket_changed:
                changed_tickets.append(item)
            for vertex in item.vertices:
                if vertex.vertex_type == "ZendeskComment":
                    continue
                if vertex.vertex_type == "ZendeskTicket" and ticket_changed:
                    vertices.append(_without_hash(vertex))
                else:
                    vertices.append(vertex)
            for comment in item.comments:
                current = comment_hashes.get(comment.vertex_id, "")
                changed = current != comment.content_hash
                record = next(
                    vertex
                    for vertex in item.vertices
                    if vertex.vertex_id == comment.vertex_id
                )
                if changed and current:
                    self._delete_chunks("ZendeskComment", comment.vertex_id)
                if changed:
                    changed_comments.append((item, comment))
                    vertices.append(_without_hash(record))
                else:
                    vertices.append(record)
        self._upsert_records(vertices, edges)
        self._upsert_chunks(changed_tickets, changed_comments)
        chunks = [chunk for item in changed_tickets for chunk in item.ticket_chunks]
        chunks.extend(
            chunk
            for _item, comment in changed_comments
            for chunk in comment.chunks
        )
        self._embed_chunks(chunks)
        for item in changed_tickets:
            self.conn.upsertVertex(
                "ZendeskTicket",
                item.vertex_id,
                attributes={"content_hash": item.content_hash},
            )
        for _item, comment in changed_comments:
            self.conn.upsertVertex(
                "ZendeskComment",
                comment.vertex_id,
                attributes={"content_hash": comment.content_hash},
            )
        return sum(len(item.comments) for item in mapped)

    def _stored_hashes(self, vertex_type: str, vertex_ids: list[str]) -> dict[str, str]:
        if not vertex_ids:
            return {}
        try:
            rows = self.conn.getVerticesById(vertex_type, vertex_ids) or []
        except Exception as exc:
            message = str(exc).lower()
            if "not a valid vertex" in message or "does not exist" in message:
                return {}
            raise
        hashes: dict[str, str] = {}
        for row in rows:
            if isinstance(row, str):
                hashes[row] = ""
                continue
            vertex_id = str(row.get("v_id") or row.get("id") or "")
            attributes = row.get("attributes") or {}
            value = attributes.get("content_hash")
            if isinstance(value, dict):
                value = value.get("value")
            hashes[vertex_id] = str(value or "")
        return hashes

    def _delete_chunks(self, vertex_type: str, vertex_id: str) -> None:
        chunk_ids = self._chunk_ids(vertex_type, vertex_id)
        if not chunk_ids:
            return
        unique = list(dict.fromkeys(chunk_ids))
        get_embedding_store(graphname=self.graphname).remove_embeddings(ids=unique)
        self.conn.delVerticesById("DocumentChunk", unique)
        self.conn.delVerticesById("Content", unique)

    def _chunk_ids(self, vertex_type: str, vertex_id: str) -> list[str]:
        try:
            edges = self.conn.getEdges(
                vertex_type,
                vertex_id,
                "reverse_CONTAINS_ENTITY",
            ) or []
        except Exception as exc:
            if "is not a valid vertex id" in str(exc):
                return []
            raise
        return [
            str(edge["to_id"])
            for edge in edges
            if edge.get("to_type") == "DocumentChunk" and edge.get("to_id") is not None
        ]

    def _upsert_chunks(
        self,
        tickets: list[MappedTicket],
        comments: list[tuple[MappedTicket, Any]],
    ) -> None:
        epoch_added = int(time.time())
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        for item in tickets:
            previous: str | None = None
            for chunk in item.ticket_chunks:
                vertices.extend(self._chunk_vertices(chunk, epoch_added, "zendesk_ticket"))
                edges.extend(
                    self._chunk_edges(chunk.chunk_id, item.vertex_id, (), previous)
                )
                previous = chunk.chunk_id
        for item, comment in comments:
            previous = None
            for chunk in comment.chunks:
                vertices.extend(
                    self._chunk_vertices(chunk, epoch_added, "zendesk_comment")
                )
                edges.extend(
                    self._chunk_edges(
                        chunk.chunk_id,
                        item.vertex_id,
                        (("ZendeskComment", comment.vertex_id),),
                        previous,
                    )
                )
                previous = chunk.chunk_id
        self._upsert_records(vertices, edges)

    def _chunk_vertices(
        self,
        chunk: MappedChunk,
        epoch_added: int,
        ctype: str,
    ) -> list[VertexRecord]:
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
                {
                    "ctype": ctype,
                    "text": chunk.text,
                    "epoch_added": epoch_added,
                },
            ),
        ]

    def _chunk_edges(
        self,
        chunk_id: str,
        ticket_vertex_id: str,
        entities: tuple[tuple[str, str], ...],
        previous_chunk_id: str | None,
    ) -> list[EdgeRecord]:
        edges = [
            EdgeRecord(
                "DocumentChunk",
                chunk_id,
                "HAS_CONTENT",
                "Content",
                chunk_id,
            ),
            EdgeRecord(
                "DocumentChunk",
                chunk_id,
                "CONTAINS_ENTITY",
                "ZendeskTicket",
                ticket_vertex_id,
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

    def _upsert_records(
        self,
        vertices: list[VertexRecord],
        edges: list[EdgeRecord],
    ) -> None:
        for records, are_edges in ((vertices, False), (edges, True)):
            for start in range(0, len(records), UPSERT_BATCH_SIZE):
                batch = records[start : start + UPSERT_BATCH_SIZE]
                payload = upsert_payload(
                    [] if are_edges else batch,
                    batch if are_edges else [],
                )
                result = self.conn.upsertData(json.dumps(payload))
                if isinstance(result, dict) and (
                    result.get("skipped_vertices") or result.get("skipped_edges")
                ):
                    raise RuntimeError(
                        "TigerGraph rejected part of the Zendesk graph upsert"
                    )

    def _embed_chunks(self, chunks: list[MappedChunk]) -> None:
        if not chunks:
            return
        store = get_embedding_store(graphname=self.graphname)

        async def embed_batches() -> None:
            sem = asyncio.Semaphore(5)

            async def _run_batch(batch: list[MappedChunk]) -> None:
                async with sem:
                    await store.aadd_embeddings(
                        [(chunk.text, []) for chunk in batch],
                        [
                            {"vertex_id": (chunk.chunk_id, "DocumentChunk")}
                            for chunk in batch
                        ],
                    )

            tasks = [
                _run_batch(chunks[start : start + EMBEDDING_BATCH_SIZE])
                for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE)
            ]
            await asyncio.gather(*tasks)

        asyncio.run(embed_batches())
        processed_at = int(time.time())
        for chunk in chunks:
            self.conn.upsertVertex(
                "DocumentChunk",
                chunk.chunk_id,
                attributes={"epoch_processed": processed_at},
            )


def _without_hash(vertex: VertexRecord) -> VertexRecord:
    attributes = dict(vertex.attributes)
    attributes.pop("content_hash", None)
    return VertexRecord(vertex.vertex_type, vertex.vertex_id, attributes)


def _ticket_sort_key(ticket: dict[str, Any]) -> datetime:
    raw = ticket.get("updated_at")
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return _parse_time(str(raw))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
