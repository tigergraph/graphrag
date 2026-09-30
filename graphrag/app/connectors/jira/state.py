"""Atomic persistence and secret redaction for Jira data sources."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from common.config import _config_file_lock, validate_graphname

from .config import JiraDataSource, JiraSourceFile, JiraSyncState


MASKED_SECRET = "********"
NON_TENANT_ATLASSIAN_HOSTS = {
    "api.atlassian.com",
    "atlassian.net",
    "graphql.atlassian.net",
    "id.atlassian.com",
}


class JiraSourceStore:
    def __init__(self, config_root: str = "configs/graph_configs"):
        self.config_root = Path(config_root)

    def path(self, graphname: str) -> Path:
        validate_graphname(graphname)
        return self.config_root / graphname / "data_sources.json"

    def load(self, graphname: str) -> JiraSourceFile:
        path = self.path(graphname)
        if not path.exists():
            return JiraSourceFile()
        with _config_file_lock:
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        return JiraSourceFile.model_validate(payload)

    def save(self, graphname: str, config: JiraSourceFile) -> None:
        path = self.path(graphname)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = config.model_dump(mode="json")
        for source in payload["sources"]:
            if not source["connection"].get("api_token"):
                source["connection"].pop("api_token", None)
        with _config_file_lock:
            try:
                with tmp.open("w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()

    def list(self, graphname: str, *, redact: bool = True) -> list[dict]:
        sources = self.load(graphname).sources
        return [self.redact(source) if redact else source.model_dump(mode="json") for source in sources]

    def get(self, graphname: str, source_id: str) -> JiraDataSource:
        for source in self.load(graphname).sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def upsert(self, graphname: str, submitted: JiraDataSource) -> JiraDataSource:
        hostname = urlparse(submitted.connection.site_url).hostname
        if hostname and hostname.lower() in NON_TENANT_ATLASSIAN_HOSTS:
            raise ValueError(
                "site_url must be your Jira tenant URL, for example "
                "https://your-company.atlassian.net"
            )
        config = self.load(graphname)
        sources = list(config.sources)
        existing = next((s for s in sources if s.id == submitted.id), None)
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
            )
            if submitted.connection.site_url != existing.connection.site_url:
                unused_draft = (
                    existing.sync.last_tested_at is None
                    and existing.sync.checkpoint is None
                    and existing.sync.last_issue_count == 0
                )
                if not unused_draft:
                    raise ValueError(
                        "site_url cannot be changed after a source has been used; "
                        "create a new source so existing Jira data can be reconciled"
                    )
                credentials_changed = True
            else:
                pass
            submitted.sync = existing.sync.model_copy(deep=True)
            submitted.sync.overlap_seconds = overlap_seconds
            if credentials_changed:
                submitted.sync.last_tested_at = None
            if submitted.scope != existing.scope:
                submitted.sync.checkpoint = None
        else:
            submitted.sync = JiraSyncState(overlap_seconds=overlap_seconds)
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

    def update_runtime_state(
        self,
        graphname: str,
        source: JiraDataSource,
    ) -> None:
        """Persist connector-owned sync fields without losing edits."""
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
    def redact(source: JiraDataSource) -> dict:
        payload = source.model_dump(mode="json")
        payload["connection"]["api_token"] = ""
        return payload
