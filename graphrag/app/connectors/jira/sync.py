"""Jira Cloud synchronization into the existing GraphRAG pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterator

from common.config import get_embedding_store
from common.db.health import embedding_coverage

from .client import JiraCloudClient
from .config import JiraDataSource
from .mapper import (
    EdgeRecord,
    JiraIssueMapper,
    MappedChunk,
    MappedComment,
    MappedIssue,
    VertexRecord,
)
from .schema import (
    JIRA_ASSIGNEE_EDGE,
    JIRA_CHANGE_AUTHOR_EDGE,
    JIRA_CHANGE_EDGE,
    JIRA_COMMENT_AFTER_EDGE,
    JIRA_COMMENT_AUTHOR_EDGE,
    JIRA_COMMENT_ISSUE_EDGE,
    JIRA_COMMENT_REPLY_EDGE,
    JIRA_LINK_EDGE,
    JIRA_PARENT_EDGE,
    JIRA_PROJECT_EDGE,
    JIRA_REPORTER_EDGE,
    jira_schema_status,
)
from .state import JiraSourceStore

logger = logging.getLogger(__name__)

CURRENT_STATE_EDGES = (
    JIRA_PROJECT_EDGE,
    JIRA_ASSIGNEE_EDGE,
    JIRA_REPORTER_EDGE,
    JIRA_PARENT_EDGE,
    JIRA_LINK_EDGE,
    JIRA_CHANGE_EDGE,
)
COMMENT_STATE_EDGES = (
    JIRA_COMMENT_ISSUE_EDGE,
    JIRA_COMMENT_AUTHOR_EDGE,
    JIRA_COMMENT_REPLY_EDGE,
    JIRA_COMMENT_AFTER_EDGE,
)
UPSERT_BATCH_SIZE = 500
EMBEDDING_BATCH_SIZE = 32
# Flush accumulated documents and comment-chunks to TigerGraph every this
# many Jira pages.  A higher value means fewer supportai.ingest() round-trips
# (the dominant overhead for large initial loads) at the cost of a larger
# in-memory accumulation.  10 pages × 100 issues = 1 000 issues per flush.
DOC_LOAD_BATCH_PAGES = 10


def _attrs(attributes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {"value": value}
        for key, value in attributes.items()
        if value is not None
    }


def _upsert_payload(
    vertices: list[VertexRecord],
    edges: list[EdgeRecord],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vertices": defaultdict(dict),
        "edges": defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(lambda: defaultdict(dict))
            )
        ),
    }
    for vertex in vertices:
        payload["vertices"][vertex.vertex_type][vertex.vertex_id] = _attrs(
            vertex.attributes
        )
    for edge in edges:
        payload["edges"][edge.source_type][edge.source_id][edge.edge_type][
            edge.target_type
        ][edge.target_id] = _attrs(edge.attributes)
    return payload


class JiraSyncService:
    def __init__(
        self,
        graphname: str,
        source: JiraDataSource,
        conn,
        *,
        store: JiraSourceStore | None = None,
        client: JiraCloudClient | None = None,
    ):
        self.graphname = graphname
        self.source = source
        self.conn = conn
        self.store = store or JiraSourceStore()
        self.client = client or JiraCloudClient(source)
        self._owns_client = client is None

    def run(self) -> dict[str, Any]:
        if not self.source.enabled:
            raise ValueError("Cannot sync a disabled data source")

        started = datetime.now(timezone.utc)
        self.source.sync.last_started_at = started
        self.source.sync.last_error = None
        self.store.update_runtime_state(self.graphname, self.source)

        try:
            schema_result = jira_schema_status(self.conn)
            if schema_result["status"] != "installed":
                raise RuntimeError(
                    "Jira schema is not installed for this graph. "
                    "Install it from Data Sources before synchronization."
                )
            self._prepare_legacy_comment_migration()

            mapper = JiraIssueMapper(
                self.source,
                graphname=self.graphname,
            )
            existing_hashes = self._all_existing_hashes()
            # If the graph has no JiraIssue vertices but a checkpoint exists,
            # the graph was likely cleared or recreated. Reset the checkpoint
            # so the next iteration performs a full sync instead of a no-op.
            if not existing_hashes and self.source.sync.checkpoint is not None:
                logger.warning(
                    "Graph appears empty but checkpoint is set — resetting "
                    "checkpoint for graph=%s source=%s to trigger a full sync",
                    self.graphname,
                    self.source.id,
                )
                self.source.sync.checkpoint = None
                self.store.update_runtime_state(self.graphname, self.source)
            existing_comment_hashes = self._all_existing_comment_hashes()
            issues_upserted = 0
            documents_loaded = 0
            comments_deleted = 0

            # Pending batches — accumulated across DOC_LOAD_BATCH_PAGES pages
            # before being flushed to TigerGraph in one supportai.ingest call.
            pending_docs: list[MappedIssue] = []
            pending_comments: list[MappedComment] = []
            pages_since_flush = 0
            # Track the highest "updated" timestamp seen so far.  We only
            # advance the checkpoint AFTER a successful batch flush so that a
            # crash before the flush leaves the checkpoint behind the lost
            # pages, ensuring they are re-fetched on the next sync run.
            pending_checkpoint = self.source.sync.checkpoint

            # ── Recovery phase ────────────────────────────────────────────────
            # Re-embed ingested issues whose content hash was cleared before
            # embedding finished. This does not change the checkpoint and does
            # not replace the project scan below.
            recovery_keys = self._find_recovery_issue_keys()
            if recovery_keys:
                recovery_docs: list[MappedIssue] = []
                recovery_comments: list[MappedComment] = []
                for issues in self.client.iter_issues_by_keys(recovery_keys):
                    mapped = [mapper.map(issue) for issue in issues]
                    changed_docs, changed_comms, deleted = self._upsert_issue_records(
                        mapped, existing_hashes, existing_comment_hashes
                    )
                    recovery_docs.extend(changed_docs)
                    recovery_comments.extend(changed_comms)
                    documents_loaded += len(changed_docs)
                    comments_deleted += deleted
                    issues_upserted += len(mapped)

                if recovery_docs or recovery_comments:
                    self._finalize_batch(
                        recovery_docs,
                        recovery_comments,
                        existing_hashes,
                        existing_comment_hashes,
                    )
                # Do not move the checkpoint here. A cleared checkpoint means a
                # new project must be scanned from the start. Link stubs are
                # not failed embeddings, and their update times must not become
                # the checkpoint for that scan.
                logger.info(
                    "Recovery complete: %d doc(s), %d comment(s) re-embedded "
                    "for graph=%s source=%s. The project scan continues from "
                    "the saved checkpoint.",
                    len(recovery_docs), len(recovery_comments),
                    self.graphname, self.source.id,
                )
            # ── End recovery phase ────────────────────────────────────────────

            for issues in self._iter_pages_pipelined():
                mapped = [mapper.map(issue) for issue in issues]
                changed_docs, changed_comms, deleted_count = (
                    self._upsert_issue_records(
                        mapped,
                        existing_hashes,
                        existing_comment_hashes,
                    )
                )
                pending_docs.extend(changed_docs)
                pending_comments.extend(changed_comms)
                documents_loaded += len(changed_docs)
                comments_deleted += deleted_count
                issues_upserted += len(mapped)
                pages_since_flush += 1

                page_max = max(
                    (item.updated for item in mapped if item.updated is not None),
                    default=None,
                )
                if page_max is not None and (
                    pending_checkpoint is None or page_max > pending_checkpoint
                ):
                    pending_checkpoint = page_max

                if pages_since_flush >= DOC_LOAD_BATCH_PAGES:
                    self._finalize_batch(
                        pending_docs,
                        pending_comments,
                        existing_hashes,
                        existing_comment_hashes,
                    )
                    pending_docs = []
                    pending_comments = []
                    pages_since_flush = 0
                    if pending_checkpoint != self.source.sync.checkpoint:
                        self.source.sync.checkpoint = pending_checkpoint
                        self.store.update_runtime_state(
                            self.graphname, self.source
                        )

            # Final flush for any pages that did not fill a complete batch.
            if pending_docs or pending_comments:
                self._finalize_batch(
                    pending_docs,
                    pending_comments,
                    existing_hashes,
                    existing_comment_hashes,
                )
            if pending_checkpoint != self.source.sync.checkpoint:
                self.source.sync.checkpoint = pending_checkpoint
                self.store.update_runtime_state(self.graphname, self.source)

            chunk_coverage = embedding_coverage(
                self.conn,
                "DocumentChunk",
            )
            logger.info(
                "embedding_coverage graph=%s: %s",
                self.graphname,
                chunk_coverage,
            )
            missing_chunk_embeddings = (
                int(chunk_coverage["missing"])
                if chunk_coverage is not None
                else 0
            )

            # Recovery check: if this sync loaded nothing new but there are
            # DocumentChunks still at epoch_processed=0, a previous ECC rebuild
            # must have failed partway through.  Trigger a fresh rebuild so
            # those chunks get embedded without the user having to intervene.
            if not documents_loaded and not comments_deleted and not missing_chunk_embeddings:
                if self._has_unprocessed_chunks():
                    logger.info(
                        "Detected unprocessed DocumentChunks from a prior failed "
                        "ECC rebuild for graph=%s source=%s — triggering recovery rebuild",
                        self.graphname,
                        self.source.id,
                    )
                    missing_chunk_embeddings = 1
            self.source.sync.last_completed_at = datetime.now(timezone.utc)
            self.source.sync.last_issue_count = issues_upserted
            self.source.sync.last_error = None
            self.source.sync.migrating_legacy_comments = False
            self.store.update_runtime_state(self.graphname, self.source)
            return {
                "status": "completed",
                "issues_upserted": issues_upserted,
                "issues_deleted": 0,
                "comments_deleted": comments_deleted,
                "documents_loaded": documents_loaded,
                "checkpoint": (
                    self.source.sync.checkpoint.isoformat()
                    if self.source.sync.checkpoint is not None
                    else None
                ),
                "schema": schema_result["status"],
                "missing_chunk_embeddings": missing_chunk_embeddings,
                "rebuild_required": bool(
                    documents_loaded
                    or comments_deleted
                    or missing_chunk_embeddings
                ),
            }
        except Exception as exc:
            logger.exception(
                "Jira sync failed for graph=%s source=%s",
                self.graphname,
                self.source.id,
            )
            self.source.sync.last_error = str(exc)[:1000]
            self.store.update_runtime_state(self.graphname, self.source)
            raise
        finally:
            if self._owns_client:
                self.client.close()

    def _find_recovery_issue_keys(self) -> set[str]:
        """Return issue keys whose own ingest was written but not embedded.

        Link and parent placeholders also have an empty content_hash. They
        were never ingested, so they are not failed embeddings. Only a full
        issue (status or updated set) or a real comment (body or created set)
        is recovered. This does not replace the project scan.
        """
        keys: set[str] = set()

        try:
            issue_verts = self.conn.getVertices(
                "JiraIssue",
                where='content_hash=""',
                select="content_hash,status,updated",
                limit=5000,
            ) or []
            for vertex in issue_verts:
                if not self._issue_embed_incomplete(vertex):
                    continue
                key = self._issue_key_from_vertex_id(str(vertex.get("v_id", "")))
                if key:
                    keys.add(key)
        except Exception as exc:
            logger.warning(
                "Recovery: could not query empty-hash JiraIssue vertices "
                "graph=%s: %s", self.graphname, exc,
            )

        try:
            comment_verts = self.conn.getVertices(
                "JiraComment",
                where='content_hash=""',
                select="content_hash,body,created",
                limit=5000,
            ) or []
            for vertex in comment_verts:
                if not self._comment_embed_incomplete(vertex):
                    continue
                comment_v_id = str(vertex.get("v_id", ""))
                try:
                    edges = self.conn.getEdges(
                        "JiraComment",
                        comment_v_id,
                        JIRA_COMMENT_ISSUE_EDGE,
                    ) or []
                except Exception:
                    continue
                for edge in edges:
                    key = self._issue_key_from_vertex_id(str(edge.get("to_id", "")))
                    if key:
                        keys.add(key)
        except Exception as exc:
            logger.warning(
                "Recovery: could not query empty-hash JiraComment vertices "
                "graph=%s: %s", self.graphname, exc,
            )

        if keys:
            logger.info(
                "Recovery: found %d ingested issue(s) with missing embeddings "
                "in graph=%s source=%s. Re-embedding them without moving the "
                "project checkpoint.",
                len(keys), self.graphname, self.source.id,
            )
        return keys

    @staticmethod
    def _vertex_attr(vertex: dict, name: str) -> str:
        attributes = vertex.get("attributes") or {}
        value = attributes.get(name)
        if isinstance(value, dict):
            value = value.get("value")
        return str(value or "").strip()

    @classmethod
    def _issue_embed_incomplete(cls, vertex: dict) -> bool:
        """True for an ingested issue whose content hash was not saved.

        A link placeholder has no status and no real updated time.
        """
        status = cls._vertex_attr(vertex, "status")
        updated = cls._vertex_attr(vertex, "updated")
        return bool(status) or cls._is_real_timestamp(updated)

    @classmethod
    def _comment_embed_incomplete(cls, vertex: dict) -> bool:
        """True for a stored comment whose content hash was not saved.

        A parent-comment placeholder has no body and no created time.
        """
        body = cls._vertex_attr(vertex, "body")
        created = cls._vertex_attr(vertex, "created")
        return bool(body) or cls._is_real_timestamp(created)

    @staticmethod
    def _is_real_timestamp(value: str) -> bool:
        return bool(value) and not value.startswith("1970-01-01")

    @staticmethod
    def _issue_key_from_vertex_id(vertex_id: str) -> str:
        if not vertex_id.startswith("jira:") or not vertex_id.endswith(":issue"):
            return ""
        return vertex_id.removeprefix("jira:").removesuffix(":issue").upper()

    def _all_content_hashes(self, vertex_type: str) -> dict[str, str]:
        # pyTigerGraph's getVerticesById raises error 601 as soon as any
        # requested STRING ID does not exist, which is the normal state during
        # an initial or incremental ingestion. Read the lightweight hash
        # projection once and filter it in memory.
        #
        # getVertices() in this version of pyTigerGraph does not support an
        # offset parameter, and an unbounded call hits TigerGraph's 4 MB REST
        # limit (REST-4000) on large vertex sets.  Use a GSQL interpreted query
        # with LIMIT/OFFSET for proper pagination without that constraint.
        _PAGE = 10_000
        result: dict[str, str] = {}
        offset = 0
        while True:
            query = (
                f"INTERPRET QUERY() FOR GRAPH {self.graphname} {{\n"
                f"  verts = {{{vertex_type}.*}};\n"
                f"  res = SELECT v FROM verts:v\n"
                f"        ORDER BY v.content_hash ASC\n"
                f"        LIMIT {_PAGE} OFFSET {offset};\n"
                f"  PRINT res[res.content_hash];\n"
                f"}}"
            )
            response = self.conn.runInterpretedQuery(query) or []
            page = response[0].get("res", []) if response else []
            for vertex in page:
                # TG prefixes the attribute with the result-set alias:
                # "res.content_hash" rather than plain "content_hash".
                result[str(vertex.get("v_id", ""))] = str(
                    (vertex.get("attributes") or {}).get("res.content_hash") or ""
                )
            if len(page) < _PAGE:
                break
            offset += _PAGE
        return result

    def _all_existing_hashes(self) -> dict[str, str]:
        return self._all_content_hashes("JiraIssue")

    def _all_existing_comment_hashes(self) -> dict[str, str]:
        return self._all_content_hashes("JiraComment")

    def _has_unprocessed_chunks(self) -> bool:
        """True when at least one DocumentChunk has epoch_processed=0.

        Called only when this sync loaded no new content (documents_loaded=0,
        comments_deleted=0).  In that case any epoch_processed=0 chunk is a
        survivor from a prior ECC rebuild that failed before it could embed
        everything.  We signal rebuild_required so the next ECC run can
        complete the job without the user having to force a re-ingest.
        """
        try:
            hits = self.conn.getVertices(
                "DocumentChunk",
                where="epoch_processed=0",
                select="epoch_processed",
                limit=1,
            ) or []
            logger.info(
                "_has_unprocessed_chunks graph=%s: found %d chunk(s) with epoch_processed=0",
                self.graphname,
                len(hits),
            )
            return len(hits) > 0
        except Exception as exc:
            logger.warning("_has_unprocessed_chunks check failed: %s", exc)
            return False

    def _prepare_legacy_comment_migration(self) -> None:
        if self.source.sync.migrating_legacy_comments:
            return
        documents = self.conn.getVertices("Document", select="id") or []
        has_legacy_comments = any(
            ":comment-doc:" in str(document.get("v_id") or "")
            for document in documents
        )
        if not has_legacy_comments:
            return
        self.source.sync.migrating_legacy_comments = True
        self.source.sync.checkpoint = None
        self.store.update_runtime_state(self.graphname, self.source)

    def _existing_hashes(self, issue_ids: list[str]) -> dict[str, str]:
        if not issue_ids:
            return {}
        requested = set(issue_ids)
        return {
            issue_id: content_hash
            for issue_id, content_hash in self._all_existing_hashes().items()
            if issue_id in requested
        }

    def _iter_pages_pipelined(self) -> Iterator[list[dict[str, Any]]]:
        """Yield Jira issue pages pre-fetched by a background thread.

        The background thread requests the next page from the Jira API while
        the main thread is writing the current page to TigerGraph, overlapping
        network I/O with graph I/O.  A queue capacity of 2 bounds memory: at
        most two extra pages are held in RAM at any moment.
        """
        page_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
        error_holder: list[Exception] = []

        def _fetch() -> None:
            try:
                for page in self.client.iter_issue_pages():
                    page_queue.put(("page", page))
            except Exception as exc:  # noqa: BLE001
                error_holder.append(exc)
            finally:
                page_queue.put(("done", None))

        thread = threading.Thread(
            target=_fetch, daemon=True, name="jira-page-fetcher"
        )
        thread.start()
        try:
            while True:
                kind, value = page_queue.get()
                if kind == "done":
                    if error_holder:
                        raise error_holder[0]
                    break
                yield value
        finally:
            thread.join(timeout=60)

    def _upsert_issue_records(
        self,
        mapped: list[MappedIssue],
        existing_hashes: dict[str, str],
        existing_comment_hashes: dict[str, str],
    ) -> tuple[list[MappedIssue], list[MappedComment], int]:
        """Write graph vertices/edges for one page; return items to embed later.

        Fact chunks and comment chunks are written by the caller in batches
        via _finalize_batch().

        Returns (changed_documents, changed_comments, comments_deleted).
        """
        changed_documents = self._changed_documents(mapped, existing_hashes)
        mapped_comments = [
            comment
            for item in mapped
            for comment in item.comments
        ]
        changed_comments = self._changed_comments(
            mapped_comments,
            existing_comment_hashes,
        )
        comments_deleted = 0
        for item in mapped:
            comments_deleted += self._reconcile_issue_comments(
                item,
                existing_comment_hashes,
            )
            self._reconcile_issue_changes(item)

        all_vertices: dict[tuple[str, str], VertexRecord] = {}
        all_edges: list[EdgeRecord] = []
        for item in mapped:
            for vertex in item.vertices:
                key = (vertex.vertex_type, vertex.vertex_id)
                previous = all_vertices.get(key)
                if (
                    previous
                    and previous.attributes.keys() - vertex.attributes.keys()
                ):
                    # Keep richer attributes if a linked-issue placeholder
                    # arrives after a full issue record in the same page.
                    continue
                all_vertices[key] = vertex
            all_edges.extend(item.edges)
            if item.issue_vertex_id in existing_hashes:
                self._delete_current_edges(item.issue_vertex_id)
                for change_vertex_id in item.change_vertex_ids:
                    self._delete_change_author_edges(change_vertex_id)
            for comment in item.comments:
                if comment.comment_vertex_id in existing_comment_hashes:
                    self._delete_current_comment_edges(
                        comment.comment_vertex_id
                    )

        # Do not advance a changed item's content hash until its document load
        # succeeds. A failed batch is therefore safe to replay after restart.
        def strip_content_hash(vertex_type: str, vertex_id: str) -> None:
            key = (vertex_type, vertex_id)
            vertex = all_vertices.get(key)
            if vertex is not None:
                attributes = dict(vertex.attributes)
                attributes.pop("content_hash", None)
                all_vertices[key] = VertexRecord(
                    vertex.vertex_type,
                    vertex.vertex_id,
                    attributes,
                )

        for item in changed_documents:
            strip_content_hash("JiraIssue", item.issue_vertex_id)
        for comment in changed_comments:
            strip_content_hash("JiraComment", comment.comment_vertex_id)

        self._upsert_records(list(all_vertices.values()), all_edges)
        return changed_documents, changed_comments, comments_deleted

    def _finalize_batch(
        self,
        changed_documents: list[MappedIssue],
        changed_comments: list[MappedComment],
        existing_hashes: dict[str, str],
        existing_comment_hashes: dict[str, str],
    ) -> None:
        """Write issue fact chunks and embed comment chunks for a batch.

        Called after every DOC_LOAD_BATCH_PAGES pages (and once at the end of
        the sync). Facts are written by the connector, one short chunk per
        record, description piece, and change, instead of one document blob.
        """
        if changed_documents:
            self._write_issue_facts(changed_documents, existing_hashes)
        if changed_comments:
            self._upsert_comment_chunks(changed_comments)
            self._embed_chunks(
                [
                    chunk
                    for comment in changed_comments
                    for chunk in comment.chunks
                ]
            )
            for comment in changed_comments:
                self.conn.upsertVertex(
                    "JiraComment",
                    comment.comment_vertex_id,
                    attributes={"content_hash": comment.content_hash},
                )
                existing_comment_hashes[
                    comment.comment_vertex_id
                ] = comment.content_hash

    def _upsert_issue_page(
        self,
        mapped: list[MappedIssue],
        existing_hashes: dict[str, str],
        existing_comment_hashes: dict[str, str],
    ) -> tuple[int, int]:
        changed_documents = self._changed_documents(mapped, existing_hashes)
        mapped_comments = [
            comment
            for item in mapped
            for comment in item.comments
        ]
        changed_comments = self._changed_comments(
            mapped_comments,
            existing_comment_hashes,
        )
        comments_deleted = 0
        for item in mapped:
            comments_deleted += self._reconcile_issue_comments(
                item,
                existing_comment_hashes,
            )
            self._reconcile_issue_changes(item)

        all_vertices: dict[tuple[str, str], VertexRecord] = {}
        all_edges: list[EdgeRecord] = []
        for item in mapped:
            for vertex in item.vertices:
                key = (vertex.vertex_type, vertex.vertex_id)
                previous = all_vertices.get(key)
                if (
                    previous
                    and previous.attributes.keys() - vertex.attributes.keys()
                ):
                    # Keep richer attributes if a linked-issue placeholder
                    # arrives after a full issue record in the same page.
                    continue
                all_vertices[key] = vertex
            all_edges.extend(item.edges)
            if item.issue_vertex_id in existing_hashes:
                self._delete_current_edges(item.issue_vertex_id)
                for change_vertex_id in item.change_vertex_ids:
                    self._delete_change_author_edges(change_vertex_id)
            for comment in item.comments:
                if comment.comment_vertex_id in existing_comment_hashes:
                    self._delete_current_comment_edges(
                        comment.comment_vertex_id
                    )

        # Do not advance a changed issue's content hash until its document load
        # succeeds. A failed page is therefore safe to replay after restart.
        def strip_content_hash(vertex_type: str, vertex_id: str) -> None:
            key = (vertex_type, vertex_id)
            vertex = all_vertices.get(key)
            if vertex is not None:
                attributes = dict(vertex.attributes)
                attributes.pop("content_hash", None)
                all_vertices[key] = VertexRecord(
                    vertex.vertex_type,
                    vertex.vertex_id,
                    attributes,
                )

        for item in changed_documents:
            strip_content_hash("JiraIssue", item.issue_vertex_id)
        for comment in changed_comments:
            strip_content_hash("JiraComment", comment.comment_vertex_id)

        self._upsert_records(list(all_vertices.values()), all_edges)

        documents_to_load: list[MappedIssue | MappedComment] = [
            *changed_documents,
            *changed_comments,
        ]
        if documents_to_load:
            if changed_documents:
                self._write_issue_facts(changed_documents, existing_hashes)
            if changed_comments:
                self._upsert_comment_chunks(changed_comments)
                self._embed_chunks(
                    [
                        chunk
                        for comment in changed_comments
                        for chunk in comment.chunks
                    ]
                )
            for comment in changed_comments:
                self.conn.upsertVertex(
                    "JiraComment",
                    comment.comment_vertex_id,
                    attributes={"content_hash": comment.content_hash},
                )
                existing_comment_hashes[
                    comment.comment_vertex_id
                ] = comment.content_hash

        return len(documents_to_load), comments_deleted

    def _upsert_records(
        self,
        vertices: list[VertexRecord],
        edges: list[EdgeRecord],
    ) -> None:
        # Vertices must exist before their edge batches are applied. Keeping
        # payloads bounded avoids REST request-size failures on large projects.
        for records, are_edges in ((vertices, False), (edges, True)):
            for start in range(0, len(records), UPSERT_BATCH_SIZE):
                batch = records[start : start + UPSERT_BATCH_SIZE]
                payload = _upsert_payload(
                    [] if are_edges else batch,
                    batch if are_edges else [],
                )
                result = self.conn.upsertData(json.dumps(payload))
                if isinstance(result, dict) and (
                    result.get("skipped_vertices") or result.get("skipped_edges")
                ):
                    raise RuntimeError(
                        "TigerGraph rejected part of the Jira graph upsert"
                    )

    def _changed_documents(
        self,
        mapped: list[MappedIssue],
        existing: dict[str, str],
    ) -> list[MappedIssue]:
        changed: list[MappedIssue] = []
        for item in mapped:
            if existing.get(item.issue_vertex_id) == item.content_hash:
                continue
            if item.issue_vertex_id in existing:
                self._delete_issue_search_content(item)
            changed.append(item)
        return changed

    def _changed_comments(
        self,
        comments: list[MappedComment],
        existing: dict[str, str],
    ) -> list[MappedComment]:
        changed: list[MappedComment] = []
        for comment in comments:
            if existing.get(comment.comment_vertex_id) == comment.content_hash:
                continue
            if comment.comment_vertex_id in existing:
                self._delete_comment_content(
                    comment.issue_vertex_id,
                    comment.comment_vertex_id,
                )
            changed.append(comment)
        return changed

    def _issue_comment_ids(self, issue_vertex_id: str) -> set[str]:
        try:
            edges = self.conn.getEdges(
                "JiraIssue",
                issue_vertex_id,
                f"reverse_{JIRA_COMMENT_ISSUE_EDGE}",
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

    def _reconcile_issue_comments(
        self,
        issue: MappedIssue,
        existing_hashes: dict[str, str],
    ) -> int:
        current_ids = {
            comment.comment_vertex_id for comment in issue.comments
        }
        stale_ids = self._issue_comment_ids(issue.issue_vertex_id) - current_ids
        for comment_vertex_id in stale_ids:
            self._delete_comment(
                issue.issue_vertex_id,
                comment_vertex_id,
            )
            existing_hashes.pop(comment_vertex_id, None)
        return len(stale_ids)

    def _reconcile_issue_changes(self, issue: MappedIssue) -> None:
        current_ids = set(issue.change_vertex_ids)
        for change_vertex_id in (
            self._issue_change_ids(issue.issue_vertex_id) - current_ids
        ):
            self.conn.delVerticesById("JiraChange", [change_vertex_id])

    def _issue_change_ids(self, issue_vertex_id: str) -> set[str]:
        try:
            edges = self.conn.getEdges(
                "JiraIssue",
                issue_vertex_id,
                JIRA_CHANGE_EDGE,
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

    def _delete_change_author_edges(self, change_vertex_id: str) -> None:
        try:
            self.conn.delEdges(
                "JiraChange",
                change_vertex_id,
                JIRA_CHANGE_AUTHOR_EDGE,
            )
        except Exception as exc:
            if "is not a valid vertex id" in str(exc):
                return
            raise

    def _delete_current_edges(self, issue_vertex_id: str) -> None:
        for edge_type in CURRENT_STATE_EDGES:
            self.conn.delEdges("JiraIssue", issue_vertex_id, edge_type)

    def _delete_current_comment_edges(
        self,
        comment_vertex_id: str,
    ) -> None:
        for edge_type in COMMENT_STATE_EDGES:
            self.conn.delEdges("JiraComment", comment_vertex_id, edge_type)

    def _delete_comment(
        self,
        issue_vertex_id: str,
        comment_vertex_id: str,
    ) -> None:
        self._delete_comment_content(issue_vertex_id, comment_vertex_id)
        self.conn.delVerticesById("JiraComment", [comment_vertex_id])

    def _comment_chunk_ids(self, comment_vertex_id: str) -> list[str]:
        try:
            edges = self.conn.getEdges(
                "JiraComment",
                comment_vertex_id,
                "reverse_CONTAINS_ENTITY",
            ) or []
        except Exception as exc:
            if "is not a valid vertex id" in str(exc):
                return []
            raise
        return [
            str(edge["to_id"])
            for edge in edges
            if edge.get("to_type") == "DocumentChunk"
            and edge.get("to_id") is not None
        ]

    def _delete_comment_content(
        self,
        issue_vertex_id: str,
        comment_vertex_id: str,
    ) -> None:
        direct_chunk_ids = self._comment_chunk_ids(comment_vertex_id)
        if direct_chunk_ids:
            get_embedding_store(
                graphname=self.graphname
            ).remove_embeddings(ids=direct_chunk_ids)
            self.conn.delVerticesById("DocumentChunk", direct_chunk_ids)
            self.conn.delVerticesById("Content", direct_chunk_ids)

        # Remove records created by the former Document -> ECC chunking path.
        comment_id = comment_vertex_id.rsplit(":comment:", 1)[-1]
        legacy_document_id = f"{issue_vertex_id}:comment-doc:{comment_id}"
        self._delete_document_chunks(legacy_document_id)
        self.conn.delVerticesById("Document", [legacy_document_id])
        self.conn.delVerticesById("Content", [legacy_document_id])

    def _delete_document_chunks(self, document_id: str) -> None:
        try:
            edges = self.conn.getEdges("Document", document_id, "HAS_CHILD") or []
        except Exception as exc:
            if "is not a valid vertex id" in str(exc):
                return
            raise
        chunk_ids = [
            str(edge.get("to_id"))
            for edge in edges
            if edge.get("to_id") is not None
        ]
        if not chunk_ids:
            return
        get_embedding_store(graphname=self.graphname).remove_embeddings(ids=chunk_ids)
        self.conn.delVerticesById("DocumentChunk", chunk_ids)
        self.conn.delVerticesById("Content", chunk_ids)

    def _write_issue_facts(
        self,
        issues: list[MappedIssue],
        existing_hashes: dict[str, str],
    ) -> None:
        self._upsert_issue_facts(issues)
        self._embed_chunks(
            [fact.chunk for issue in issues for fact in issue.facts]
        )
        for issue in issues:
            self.conn.upsertVertex(
                "JiraIssue",
                issue.issue_vertex_id,
                attributes={"content_hash": issue.content_hash},
            )
            existing_hashes[issue.issue_vertex_id] = issue.content_hash

    def _delete_issue_search_content(self, issue: MappedIssue) -> None:
        chunk_ids = self._issue_fact_chunk_ids(issue.issue_vertex_id)
        if chunk_ids:
            get_embedding_store(
                graphname=self.graphname
            ).remove_embeddings(ids=chunk_ids)
            self.conn.delVerticesById("DocumentChunk", chunk_ids)
            self.conn.delVerticesById("Content", chunk_ids)
        document_id = issue.legacy_document_id.lower()
        self._delete_document_chunks(document_id)
        self.conn.delVerticesById("Document", [document_id])
        self.conn.delVerticesById("Content", [document_id])

    def _issue_fact_chunk_ids(self, issue_vertex_id: str) -> list[str]:
        try:
            edges = self.conn.getEdges(
                "JiraIssue",
                issue_vertex_id,
                "reverse_CONTAINS_ENTITY",
            ) or []
        except Exception as exc:
            if "is not a valid vertex id" in str(exc):
                return []
            raise
        return [
            str(edge["to_id"])
            for edge in edges
            if edge.get("to_type") == "DocumentChunk"
            and edge.get("to_id") is not None
            and ":fact:" in str(edge["to_id"])
        ]

    def _upsert_issue_facts(self, issues: list[MappedIssue]) -> None:
        epoch_added = int(time.time())
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        for issue in issues:
            previous_chunk_id: str | None = None
            for fact in issue.facts:
                chunk = fact.chunk
                vertices.extend(
                    self._chunk_vertices(chunk, epoch_added, "jira_issue")
                )
                edges.extend(
                    self._chunk_edges(
                        chunk.chunk_id,
                        issue.issue_vertex_id,
                        fact.entities,
                        previous_chunk_id,
                    )
                )
                previous_chunk_id = chunk.chunk_id
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
        issue_vertex_id: str,
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
                "JiraIssue",
                issue_vertex_id,
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

    def _upsert_comment_chunks(
        self,
        comments: list[MappedComment],
    ) -> None:
        epoch_added = int(time.time())
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        for comment in comments:
            previous_chunk_id: str | None = None
            for chunk in comment.chunks:
                vertices.extend(
                    self._chunk_vertices(chunk, epoch_added, "jira_comment")
                )
                edges.extend(
                    self._chunk_edges(
                        chunk.chunk_id,
                        comment.issue_vertex_id,
                        (("JiraComment", comment.comment_vertex_id),),
                        previous_chunk_id,
                    )
                )
                previous_chunk_id = chunk.chunk_id
        self._upsert_records(vertices, edges)

    def _embed_chunks(self, chunks: list[MappedChunk]) -> None:
        if not chunks:
            return
        store = get_embedding_store(graphname=self.graphname)

        async def embed_batches() -> None:
            # Cap at 5 concurrent aadd_embeddings calls. Each call makes ~32
            # sequential Gemini requests; 20 concurrent was causing traffic
            # spikes that trigger Gemini 500 INTERNAL (server overload).
            # 5 concurrent × ~32 requests = ~160 in-flight, safe for the API.
            sem = asyncio.Semaphore(5)

            async def _run_batch(batch: list) -> None:
                async with sem:
                    await store.aadd_embeddings(
                        [(chunk.text, []) for chunk in batch],
                        [
                            {
                                "vertex_id": (
                                    chunk.chunk_id,
                                    "DocumentChunk",
                                )
                            }
                            for chunk in batch
                        ],
                    )

            tasks = [
                _run_batch(chunks[s : s + EMBEDDING_BATCH_SIZE])
                for s in range(0, len(chunks), EMBEDDING_BATCH_SIZE)
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
