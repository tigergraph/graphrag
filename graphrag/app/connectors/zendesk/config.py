"""Validated configuration for the Zendesk connector."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SUBDOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TICKET_STATUSES = ("new", "open", "pending", "hold", "solved", "closed")


class ZendeskConnection(BaseModel):
    subdomain: str
    email: str = Field(min_length=3)
    api_token: str = ""

    @field_validator("subdomain")
    @classmethod
    def validate_subdomain(cls, value: str) -> str:
        raw = value.strip().lower().rstrip("/")
        if "://" in raw or "/" in raw:
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
            if parsed.path not in ("", "/"):
                raise ValueError(
                    "subdomain must be the Zendesk account name, such as acme"
                )
            host = (parsed.hostname or "").lower()
        else:
            host = raw
        if host.endswith(".zendesk.com"):
            host = host[: -len(".zendesk.com")]
        if not SUBDOMAIN_RE.fullmatch(host):
            raise ValueError(
                "subdomain must be the Zendesk account name, such as acme"
            )
        return host

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("email must be the Zendesk account email")
        return value


class ZendeskScope(BaseModel):
    updated_after: date | None = None
    statuses: list[Literal["new", "open", "pending", "hold", "solved", "closed"]] = (
        Field(default_factory=list)
    )
    include_comments: bool = True

    @field_validator("statuses")
    @classmethod
    def deduplicate_statuses(
        cls,
        values: list[Literal["new", "open", "pending", "hold", "solved", "closed"]],
    ) -> list[Literal["new", "open", "pending", "hold", "solved", "closed"]]:
        return list(dict.fromkeys(values))


class ZendeskSyncState(BaseModel):
    overlap_seconds: int = Field(default=120, ge=0, le=3600)
    checkpoint: datetime | None = None
    last_tested_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_ticket_count: int = Field(default=0, ge=0)


class ZendeskDataSource(BaseModel):
    id: str
    type: Literal["zendesk"] = "zendesk"
    enabled: bool = True
    display_name: str = Field(min_length=1, max_length=100)
    connection: ZendeskConnection
    scope: ZendeskScope = Field(default_factory=ZendeskScope)
    sync: ZendeskSyncState = Field(default_factory=ZendeskSyncState)

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


class ZendeskSourceFile(BaseModel):
    sources: list[ZendeskDataSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_source_ids(self) -> "ZendeskSourceFile":
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("data-source ids must be unique")
        return self
