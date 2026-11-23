"""Atomic persistence and secret redaction for Confluence data sources."""

from __future__ import annotations

from pathlib import Path

from common.config import validate_graphname
from connectors.source_file import (
    ensure_unique_source_id,
    load_document,
    replace_key,
)

from .config import ConfluenceDataSource, ConfluenceSourceFile, ConfluenceSyncState

MASKED_SECRET = "********"
FILE_KEY = "confluence_sources"


class ConfluenceSourceStore:
    def __init__(self, config_root: str = "configs/graph_configs"):
        self.config_root = Path(config_root)

    def path(self, graphname: str) -> Path:
        validate_graphname(graphname)
        return self.config_root / graphname / "data_sources.json"

    def load(self, graphname: str) -> ConfluenceSourceFile:
        path = self.path(graphname)
        if not path.exists():
            return ConfluenceSourceFile()
        document = load_document(path)
        return ConfluenceSourceFile.model_validate(
            {FILE_KEY: document.get(FILE_KEY) or []}
        )

    def save(self, graphname: str, config: ConfluenceSourceFile) -> None:
        payload = config.model_dump(mode="json")
        for source in payload[FILE_KEY]:
            source["connection"].pop("api_token", None)
        replace_key(self.path(graphname), FILE_KEY, payload[FILE_KEY])

    def list(self, graphname: str, *, redact: bool = True) -> list[dict]:
        sources = self.load(graphname).confluence_sources
        return [
            self.redact(source) if redact else source.model_dump(mode="json")
            for source in sources
        ]

    def get(self, graphname: str, source_id: str) -> ConfluenceDataSource:
        for source in self.load(graphname).confluence_sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def upsert(self, graphname: str, submitted: ConfluenceDataSource) -> ConfluenceDataSource:
        path = self.path(graphname)
        ensure_unique_source_id(
            load_document(path) if path.exists() else {},
            submitted.id,
            FILE_KEY,
        )
        config = self.load(graphname)
        sources = list(config.confluence_sources)
        existing = next((source for source in sources if source.id == submitted.id), None)
        overlap_seconds = submitted.sync.overlap_seconds
        submitted.connection.api_token = ""
        if existing is not None:
            credentials_changed = (
                submitted.connection.email != existing.connection.email
                or submitted.connection.site != existing.connection.site
            )
            if submitted.connection.site != existing.connection.site:
                unused_draft = (
                    existing.sync.last_tested_at is None
                    and existing.sync.checkpoint is None
                    and existing.sync.last_page_count == 0
                )
                if not unused_draft:
                    raise ValueError(
                        "site cannot be changed after a source has been used; "
                        "create a new source so existing Confluence data stays separate"
                    )
            submitted.sync = existing.sync.model_copy(deep=True)
            submitted.sync.overlap_seconds = overlap_seconds
            if credentials_changed:
                submitted.sync.last_tested_at = None
            if (
                submitted.scope.space_keys != existing.scope.space_keys
                or submitted.scope.content_types != existing.scope.content_types
                or submitted.scope.updated_after != existing.scope.updated_after
            ):
                submitted.sync.checkpoint = None
                submitted.sync.last_content_id = None
                submitted.sync.failed_content_ids = []
                submitted.sync.pending_comment_content_ids = []
        else:
            submitted.sync = ConfluenceSyncState(overlap_seconds=overlap_seconds)
        sources = [source for source in sources if source.id != submitted.id]
        sources.append(submitted)
        sources.sort(key=lambda source: source.id)
        config.confluence_sources = sources
        self.save(graphname, config)
        return submitted

    def delete(self, graphname: str, source_id: str) -> None:
        config = self.load(graphname)
        remaining = [
            source for source in config.confluence_sources if source.id != source_id
        ]
        if len(remaining) == len(config.confluence_sources):
            raise KeyError(source_id)
        config.confluence_sources = remaining
        self.save(graphname, config)

    def update_runtime_state(self, graphname: str, source: ConfluenceDataSource) -> None:
        config = self.load(graphname)
        sources = list(config.confluence_sources)
        for index, stored in enumerate(sources):
            if stored.id != source.id:
                continue
            stored.sync = source.sync
            sources[index] = stored
            config.confluence_sources = sources
            self.save(graphname, config)
            return
        raise KeyError(source.id)

    @staticmethod
    def redact(source: ConfluenceDataSource) -> dict:
        payload = source.model_dump(mode="json")
        payload["connection"].pop("api_token", None)
        return payload
