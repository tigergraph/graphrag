"""Validated configuration for the Confluence connector."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
ContentType = Literal["page", "blogpost"]


class ConfluenceConnection(BaseModel):
    site: str
    email: str = Field(min_length=3)
    api_token: str = ""

    @field_validator("site")
    @classmethod
    def validate_site(cls, value: str) -> str:
        raw = value.strip().rstrip("/")
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        if parsed.scheme != "https" or not host:
            raise ValueError(
                "site must be a Confluence host, for example acme.atlassian.net"
            )
        if path not in ("", "/wiki") or parsed.query or parsed.fragment:
            raise ValueError("site must be the host, without a page path")
        if not HOST_RE.fullmatch(host):
            raise ValueError(
                "site must be a Confluence host, for example acme.atlassian.net"
            )
        return host

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("email must be the Atlassian account email")
        return value


class ConfluenceScope(BaseModel):
    space_keys: list[str] = Field(default_factory=list)
    content_types: list[ContentType] = Field(
        default_factory=lambda: ["page", "blogpost"]
    )
    updated_after: datetime | None = None

    @field_validator("space_keys")
    @classmethod
    def normalize_space_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            key = raw.strip().upper()
            if not SPACE_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid Confluence space key: {raw!r}")
            if key not in seen:
                normalized.append(key)
                seen.add(key)
        return normalized

    @field_validator("content_types")
    @classmethod
    def deduplicate_content_types(
        cls, values: list[ContentType]
    ) -> list[ContentType]:
        deduped = list(dict.fromkeys(values))
        if not deduped:
            raise ValueError("content_types must include page, blogpost, or both")
        return deduped


class ConfluenceSyncState(BaseModel):
    overlap_seconds: int = Field(default=120, ge=0, le=3600)
    checkpoint: datetime | None = None
    last_content_id: str | None = None
    failed_content_ids: list[str] = Field(default_factory=list)
    pending_comment_content_ids: list[str] = Field(default_factory=list)
    last_tested_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_link_error: str | None = None
    last_page_count: int = Field(default=0, ge=0)
    progress_checked: int = Field(default=0, ge=0)
    progress_written: int = Field(default=0, ge=0)
    progress_phase: str | None = None


class ConfluenceDataSource(BaseModel):
    id: str
    type: Literal["confluence"] = "confluence"
    enabled: bool = True
    display_name: str = Field(min_length=1, max_length=100)
    connection: ConfluenceConnection
    scope: ConfluenceScope = Field(default_factory=ConfluenceScope)
    sync: ConfluenceSyncState = Field(default_factory=ConfluenceSyncState)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not SOURCE_ID_RE.fullmatch(value):
            raise ValueError(
                "id must be 1-64 characters using letters, numbers, '_' or '-'"
            )
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return value.strip()


class ConfluenceSourceFile(BaseModel):
    confluence_sources: list[ConfluenceDataSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_source_ids(self) -> "ConfluenceSourceFile":
        ids = [source.id for source in self.confluence_sources]
        if len(ids) != len(set(ids)):
            raise ValueError("data-source ids must be unique")
        return self
