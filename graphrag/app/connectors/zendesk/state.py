"""Persistence for Zendesk sources in the shared data_sources.json file."""

from __future__ import annotations

import json
from pathlib import Path

from common.config import _config_file_lock, validate_graphname

from connectors.source_file import replace_sources, source_ids

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
        with _config_file_lock:
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        sources = payload.get(FILE_KEY) if isinstance(payload, dict) else None
        return ZendeskSourceFile.model_validate({"sources": sources or []})

    def save(self, graphname: str, config: ZendeskSourceFile) -> None:
        payload = config.model_dump(mode="json")
        for source in payload["sources"]:
            if not source["connection"].get("api_token"):
                source["connection"].pop("api_token", None)
        replace_sources(self.path(graphname), FILE_KEY, payload["sources"])

    def list(self, graphname: str, *, redact: bool = True) -> list[dict]:
        sources = self.load(graphname).sources
        return [
            self.redact(source) if redact else source.model_dump(mode="json")
            for source in sources
        ]

    def get(self, graphname: str, source_id: str) -> ZendeskDataSource:
        for source in self.load(graphname).sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def upsert(self, graphname: str, submitted: ZendeskDataSource) -> ZendeskDataSource:
        if submitted.id in source_ids(self.path(graphname), "sources"):
            raise ValueError("id is already used by a Jira data source")
        config = self.load(graphname)
        sources = list(config.sources)
        existing = next((item for item in sources if item.id == submitted.id), None)
        overlap_seconds = submitted.sync.overlap_seconds
        submitted.connection.api_token = (
            existing.connection.api_token
            if existing is not None
            and existing.connection.api_token != MASKED_SECRET
            else ""
        )
        if existing is not None:
            credentials_changed = submitted.connection.email != existing.connection.email
            if submitted.connection.subdomain != existing.connection.subdomain:
                unused_draft = (
                    existing.sync.last_tested_at is None
                    and existing.sync.checkpoint is None
                    and existing.sync.last_ticket_count == 0
                )
                if not unused_draft:
                    raise ValueError(
                        "subdomain cannot be changed after a source has been used; "
                        "create a new source so existing Zendesk data can be reconciled"
                    )
                credentials_changed = True
            submitted.sync = existing.sync.model_copy(deep=True)
            submitted.sync.overlap_seconds = overlap_seconds
            if credentials_changed:
                submitted.sync.last_tested_at = None
            if submitted.scope != existing.scope:
                submitted.sync.checkpoint = None
        else:
            submitted.sync = ZendeskSyncState(overlap_seconds=overlap_seconds)
        sources = [source for source in sources if source.id != submitted.id]
        sources.append(submitted)
        sources.sort(key=lambda source: source.id)
        config.sources = sources
        self.save(graphname, config)
        return submitted

    def delete(self, graphname: str, source_id: str) -> None:
        config = self.load(graphname)
        remaining = [source for source in config.sources if source.id != source_id]
        if len(remaining) == len(config.sources):
            raise KeyError(source_id)
        config.sources = remaining
        self.save(graphname, config)

    def update_runtime_state(self, graphname: str, source: ZendeskDataSource) -> None:
        config = self.load(graphname)
        sources = list(config.sources)
        for index, stored in enumerate(sources):
            if stored.id != source.id:
                continue
            stored.sync = source.sync
            sources[index] = stored
            config.sources = sources
            self.save(graphname, config)
            return
        raise KeyError(source.id)

    @staticmethod
    def redact(source: ZendeskDataSource) -> dict:
        payload = source.model_dump(mode="json")
        payload["connection"]["api_token"] = ""
        return payload
