"""Atomic persistence and secret redaction for Zendesk data sources."""

from __future__ import annotations

from pathlib import Path

from common.config import validate_graphname
from connectors.source_file import (
    ensure_unique_source_id,
    load_document,
    replace_key,
)

from .config import ZendeskDataSource, ZendeskSourceFile, ZendeskSyncState

MASKED_SECRET = "********"
FILE_KEY = "zendesk_sources"


class ZendeskSourceStore:
    def __init__(self, config_root: str = "configs/graph_configs"):
        self.config_root = Path(config_root)

    def path(self, graphname: str) -> Path:
        validate_graphname(graphname)
        return self.config_root / graphname / "data_sources.json"

    def load(self, graphname: str) -> ZendeskSourceFile:
        path = self.path(graphname)
        if not path.exists():
            return ZendeskSourceFile()
        document = load_document(path)
        return ZendeskSourceFile.model_validate(
            {FILE_KEY: document.get(FILE_KEY) or []}
        )

    def save(self, graphname: str, config: ZendeskSourceFile) -> None:
        payload = config.model_dump(mode="json")
        for source in payload[FILE_KEY]:
            if not source["connection"].get("api_token"):
                source["connection"].pop("api_token", None)
        replace_key(self.path(graphname), FILE_KEY, payload[FILE_KEY])

    def list(self, graphname: str, *, redact: bool = True) -> list[dict]:
        sources = self.load(graphname).zendesk_sources
        return [
            self.redact(source) if redact else source.model_dump(mode="json")
            for source in sources
        ]

    def get(self, graphname: str, source_id: str) -> ZendeskDataSource:
        for source in self.load(graphname).zendesk_sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def upsert(self, graphname: str, submitted: ZendeskDataSource) -> ZendeskDataSource:
        path = self.path(graphname)
        ensure_unique_source_id(
            load_document(path) if path.exists() else {},
            submitted.id,
            FILE_KEY,
        )
        config = self.load(graphname)
        sources = list(config.zendesk_sources)
        existing = next((source for source in sources if source.id == submitted.id), None)
        overlap_seconds = submitted.sync.overlap_seconds
        submitted.connection.api_token = (
            existing.connection.api_token
            if existing is not None
            and existing.connection.api_token != MASKED_SECRET
            else ""
        )
        if existing is not None:
            credentials_changed = (
                submitted.connection.email != existing.connection.email
                or submitted.connection.subdomain != existing.connection.subdomain
            )
            if submitted.connection.subdomain != existing.connection.subdomain:
                unused_draft = (
                    existing.sync.last_tested_at is None
                    and existing.sync.checkpoint is None
                    and existing.sync.last_ticket_count == 0
                )
                if not unused_draft:
                    raise ValueError(
                        "subdomain cannot be changed after a source has been used; "
                        "create a new source so existing Zendesk data stays separate"
                    )
            submitted.sync = existing.sync.model_copy(deep=True)
            submitted.sync.overlap_seconds = overlap_seconds
            if credentials_changed:
                submitted.sync.last_tested_at = None
            if (
                submitted.scope.statuses != existing.scope.statuses
                or submitted.scope.updated_after != existing.scope.updated_after
            ):
                submitted.sync.checkpoint = None
                submitted.sync.export_cursor = None
                submitted.sync.pending_comment_ticket_ids = []
        else:
            submitted.sync = ZendeskSyncState(overlap_seconds=overlap_seconds)
        sources = [source for source in sources if source.id != submitted.id]
        sources.append(submitted)
        sources.sort(key=lambda source: source.id)
        config.zendesk_sources = sources
        self.save(graphname, config)
        return submitted

    def delete(self, graphname: str, source_id: str) -> None:
        config = self.load(graphname)
        remaining = [
            source for source in config.zendesk_sources if source.id != source_id
        ]
        if len(remaining) == len(config.zendesk_sources):
            raise KeyError(source_id)
        config.zendesk_sources = remaining
        self.save(graphname, config)

    def update_runtime_state(self, graphname: str, source: ZendeskDataSource) -> None:
        config = self.load(graphname)
        sources = list(config.zendesk_sources)
        for index, stored in enumerate(sources):
            if stored.id != source.id:
                continue
            stored.sync = source.sync
            sources[index] = stored
            config.zendesk_sources = sources
            self.save(graphname, config)
            return
        raise KeyError(source.id)

    @staticmethod
    def redact(source: ZendeskDataSource) -> dict:
        payload = source.model_dump(mode="json")
        payload["connection"]["api_token"] = ""
        return payload
